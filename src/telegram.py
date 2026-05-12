"""
Telegram frontend for Soma.

Single-user bot (OWNER_CHAT_ID). Reads a message → calls core.run() →
sends final_text back. Telegram limit is 4096 chars; we truncate at 3900
with a marker. Reasoning content / intermediate text are NOT sent —
Telegram receives only the final reply.

The token comes from .env (see .env.example). A token can only feed ONE
running bot at a time — if another process polls the same token you get
TelegramConflictError.
"""
import asyncio
import logging
import os
import re
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from envfile import load_env

# Load .env from the repo root BEFORE anything else reads env vars.
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_env(ENV_PATH)


# Import core only after .env is loaded — in case core reads env vars at import.
from core import run as core_run, MEMORY_PATH  # noqa: E402
from memory import MemoryStore  # noqa: E402
from background import Background  # noqa: E402


logger = logging.getLogger("soma.telegram")

TELEGRAM_MAX = 3900  # safety margin below the Telegram 4096 limit
# Inbox lives next to memories in workspace/ — derived from core.MEMORY_PATH
INBOX = Path(MEMORY_PATH).parent / "inbox"
INBOX.mkdir(parents=True, exist_ok=True)
MAX_FILE_BYTES = 20 * 1024 * 1024  # Telegram Bot API limit


SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str | None, default_ext: str = "") -> str:
    if not name:
        return f"file_{int(time.time())}{default_ext}"
    cleaned = SAFE_NAME_RE.sub("_", name).strip("_") or f"file_{int(time.time())}"
    return cleaned[:120]


async def _safe_send(message: Message, text: str) -> None:
    if not text:
        return
    if len(text) > TELEGRAM_MAX:
        text = text[:TELEGRAM_MAX] + f"\n…[{len(text)} chars truncated]"
    try:
        await message.answer(text)
    except Exception as exc:
        logger.exception("send failed: %s", exc)


def _check_owner(message: Message, owner_id: int) -> bool:
    """Single-user check. Other users → silently ignore."""
    if message.chat.id != owner_id:
        logger.warning("ignored message from non-owner chat_id=%s",
                       message.chat.id)
        return False
    return True


async def amain() -> None:
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    owner_raw = os.environ.get("OWNER_CHAT_ID", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_TOKEN not set in .env")
    if not owner_raw or not owner_raw.lstrip("-").isdigit():
        raise SystemExit("OWNER_CHAT_ID must be a numeric chat ID in .env")
    owner_id = int(owner_raw)

    bot = Bot(token=token)
    dp = Dispatcher()

    # Background curator: async task alongside polling. Queue-driven,
    # no polling timer. Fed after each foreground turn. The bot+owner_id
    # give the background proactive-send capability.
    bg_queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    background = Background(bg_queue, bot=bot, owner_id=owner_id)
    bg_task = asyncio.create_task(background.run(), name="soma-background")

    # Message debouncing: collapse fast follow-up messages so aiogram
    # doesn't start two parallel core.run() calls. On arrival a 3s
    # timer is set; further messages reset the timer and get buffered.
    # core.run() only starts after the timeout, with the combined text.
    DEBOUNCE_SECONDS = 3.0
    pending_buffer: list[str] = []
    pending_task: asyncio.Task | None = None
    buffer_lock = asyncio.Lock()
    # Serialization lock: makes sure only ONE core.run() runs at a time —
    # even for messages > DEBOUNCE_SECONDS apart that arrive during the
    # previous run's lifetime. FIFO order guarantees chronological replies.
    core_lock = asyncio.Lock()

    async def _execute_buffered(trigger_message: Message) -> None:
        nonlocal pending_task
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return  # a newer message arrived → we were replaced
        async with buffer_lock:
            if not pending_buffer:
                pending_task = None
                return
            buffered = list(pending_buffer)
            pending_buffer.clear()
            pending_task = None
        combined = "\n\n".join(buffered)
        # Serialization: at most one core.run at a time, FIFO order.
        async with core_lock:
            logger.info("debounced %d msgs → %d chars (lock acquired)",
                        len(buffered), len(combined))
            pre_count = len(MemoryStore(MEMORY_PATH).load())
            try:
                async with ChatActionSender.typing(
                    bot=trigger_message.bot,
                    chat_id=trigger_message.chat.id,
                ):
                    result = await core_run(combined)
            except Exception as exc:
                logger.exception("debounced core.run failed: %s", exc)
                await _safe_send(trigger_message, f"Error: {exc}")
                return
            await _safe_send(trigger_message,
                              result.get("final_text", "(no reply)"))
            post_count = len(MemoryStore(MEMORY_PATH).load())
            try:
                bg_queue.put_nowait({
                    "user_message": combined,
                    "final_text": result.get("final_text", ""),
                    "blocks_executed": result.get("blocks_executed", 0),
                    "memory_delta": post_count - pre_count,
                })
            except asyncio.QueueFull:
                logger.warning("background queue full — turn dropped")

    @dp.message(F.text)
    async def on_text(message: Message) -> None:
        nonlocal pending_task
        if not _check_owner(message, owner_id):
            return
        user_text = (message.text or "").strip()
        if not user_text:
            return
        logger.info("user → %r", user_text[:100])
        async with buffer_lock:
            pending_buffer.append(user_text)
            if pending_task and not pending_task.done():
                pending_task.cancel()
            pending_task = asyncio.create_task(_execute_buffered(message))

    # /start as a smoke test
    @dp.message(F.text.startswith("/start"))
    async def on_start(message: Message) -> None:
        if not _check_owner(message, owner_id):
            return
        await _safe_send(message, "Soma is running. Send me something.")

    # --- File handling ---
    # File arrives → store in inbox → 3s debounce → ALL files in the
    # batch processed sequentially through core.run (under core_lock —
    # not parallel). Per file: extract, persist relevant facts as a memory
    # tagged 'from-file', return ONE line. AT THE END of the batch:
    # EXACTLY ONE Telegram message — list of findings + question about
    # what to do with them. No spam, no "file is at..." ack, no per-file
    # intermediate messages.
    pending_files: list[dict] = []
    pending_files_task: asyncio.Task | None = None
    files_buffer_lock = asyncio.Lock()

    BINARY_EXTS = {".msg", ".pdf", ".xlsx", ".xls", ".docx", ".doc",
                    ".pptx", ".png", ".jpg", ".jpeg", ".zip", ".gz",
                    ".tar", ".7z", ".webp"}

    def _build_file_prompt(file_info: dict) -> str:
        path = file_info["container_path"]
        size = file_info["size"]
        kind = file_info["kind"]
        caption = file_info.get("caption") or ""
        is_binary = any(path.lower().endswith(e) for e in BINARY_EXTS)
        binary_hint = ""
        if is_binary:
            binary_hint = (
                " Note: binary / structured format — extract inside the "
                "container (pypdf / extract_msg / openpyxl), NEVER load "
                "the whole content into the context."
            )
        if caption:
            # Caption mode: the caption IS the task. Full core.run.
            return (
                f"{caption}\n\n"
                f"File for this: {path} ({size} bytes, {kind}).{binary_hint}"
            )
        # Auto mode: extract + write relevant memory + return ONE line.
        # No question, no suggestion — that comes in the batch summary.
        return (
            f"File: {path} ({size} bytes, {kind}).\n"
            f"Task:\n"
            f"1) Open the file via execute.\n"
            f"2) Extract the most important facts.\n"
            f"3) If substantial facts are inside (customer, quote-no, "
            f"product, prices, people): persist them as a semantic or "
            f"episodic memory with tag 'from-file' + a domain tag — so "
            f"they aren't lost.\n"
            f"4) Reply with EXACTLY ONE line in the format:\n"
            f"   <type>: <short description with 2-3 key facts>\n"
            f"   e.g. 'Quote — Customer X, GPU server (2× GPU-Y, 512GB RAM)'\n"
            f"NO question, NO suggestion — both come later in the "
            f"batch summary.{binary_hint}"
        )

    async def _process_files_silent(file_info: dict) -> tuple[str, dict]:
        """Run one file through core.run WITHOUT Telegram output.
        Returns (one_line_summary, run_result)."""
        prompt = _build_file_prompt(file_info)
        pre_count = len(MemoryStore(MEMORY_PATH).load())
        try:
            result = await core_run(prompt)
        except Exception as exc:
            logger.exception("core.run failed (file): %s", exc)
            return (f"[error: {exc}]", {})
        post_count = len(MemoryStore(MEMORY_PATH).load())
        try:
            bg_queue.put_nowait({
                "user_message": prompt,
                "final_text": result.get("final_text", ""),
                "blocks_executed": result.get("blocks_executed", 0),
                "memory_delta": post_count - pre_count,
            })
        except asyncio.QueueFull:
            logger.warning("background queue full — file turn")
        return (result.get("final_text", "(nothing)").strip(), result)

    async def _execute_files_buffered(trigger_message: Message) -> None:
        """Debounce tail. Process ALL buffered files sequentially with no
        intermediate spam. Sends ONE Telegram message at the end."""
        nonlocal pending_files_task
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        async with files_buffer_lock:
            if not pending_files:
                pending_files_task = None
                return
            queued = list(pending_files)
            pending_files.clear()
            pending_files_task = None

        logger.info("file-batch: %d files sequential", len(queued))
        results: list[tuple[dict, str]] = []
        # Sequential under core_lock — no parallel run, no race.
        async with core_lock:
            async with ChatActionSender.typing(
                bot=trigger_message.bot,
                chat_id=trigger_message.chat.id,
            ):
                for f in queued:
                    summary, _ = await _process_files_silent(f)
                    results.append((f, summary))

        # Exactly ONE Telegram message for the whole batch.
        any_caption = any((f.get("caption") or "") for f, _ in results)
        if len(results) == 1:
            f, s = results[0]
            if f.get("caption"):
                # Caption mode: the reply is the task result — just send it.
                await _safe_send(trigger_message, s or "(no reply)")
            else:
                # Auto mode: summary + open question
                await _safe_send(
                    trigger_message,
                    f"{s}\n\nWhat should I do with it?"
                )
            return
        # Multiple files: list + open question. Captions (if present) are
        # shown as task replies, the rest indexed.
        lines = [f"{len(results)} files processed:"]
        for i, (f, s) in enumerate(results, 1):
            tag = f.get("safe_name", "?")
            lines.append(f"[{i}] {tag} — {s[:300]}")
        if not any_caption:
            lines.append("\nWhat should I do with them?")
        await _safe_send(trigger_message, "\n".join(lines))

    @dp.message(F.document | F.photo | F.voice | F.audio | F.video
                | F.video_note)
    async def on_file(message: Message) -> None:
        nonlocal pending_files_task
        if not _check_owner(message, owner_id):
            return
        async with ChatActionSender.upload_document(
            bot=message.bot, chat_id=message.chat.id
        ):
            obj = None
            kind = "file"
            default_ext = ""
            if message.document:
                obj, kind = message.document, "document"
            elif message.photo:
                obj, kind = message.photo[-1], "photo"
                default_ext = ".jpg"
            elif message.voice:
                obj, kind = message.voice, "voice"
                default_ext = ".ogg"
            elif message.audio:
                obj, kind = message.audio, "audio"
                default_ext = ".mp3"
            elif message.video:
                obj, kind = message.video, "video"
                default_ext = ".mp4"
            elif message.video_note:
                obj, kind = message.video_note, "video_note"
                default_ext = ".mp4"
            if obj is None:
                return
            size = getattr(obj, "file_size", 0) or 0
            if size and size > MAX_FILE_BYTES:
                await _safe_send(message,
                                 f"File too large ({size} bytes, limit "
                                 f"{MAX_FILE_BYTES}).")
                return
            try:
                tg_file = await message.bot.get_file(obj.file_id)
            except Exception as exc:
                logger.exception("get_file failed: %s", exc)
                await _safe_send(message, f"Download failed: {exc}")
                return
            orig_name = getattr(obj, "file_name", None)
            safe = _safe_filename(orig_name, default_ext=default_ext)
            stamp = int(time.time())
            dest = INBOX / f"{stamp}_{safe}"
            try:
                await message.bot.download_file(tg_file.file_path,
                                                destination=dest)
            except Exception as exc:
                logger.exception("download failed: %s", exc)
                await _safe_send(message, f"Download failed: {exc}")
                return
            caption = (message.caption or "").strip()
            container_path = f"/workspace/inbox/{dest.name}"
            logger.info("file received kind=%s size=%d → %s caption=%r",
                        kind, size, container_path, caption[:80])

        # Enqueue into the file buffer + (re)start the debounce task
        file_info = {
            "container_path": container_path,
            "host_path": str(dest),
            "size": size,
            "kind": kind,
            "safe_name": safe,
            "caption": caption,
        }
        async with files_buffer_lock:
            pending_files.append(file_info)
            if pending_files_task and not pending_files_task.done():
                pending_files_task.cancel()
            pending_files_task = asyncio.create_task(
                _execute_files_buffered(message)
            )

    me = await bot.get_me()
    logger.info("soma telegram bot @%s started, owner=%s",
                me.username, owner_id)
    try:
        await dp.start_polling(bot)
    finally:
        bg_task.cancel()
        try:
            await bg_task
        except (asyncio.CancelledError, Exception):
            pass
        await bot.session.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        logger.info("interrupted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
