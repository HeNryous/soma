"""
Telegram-Frontend für Soma.

Single-User-Bot (OWNER_CHAT_ID). Liest Nachricht → ruft core.run() →
sendet final_text zurück. Telegram-Limit 4096 chars, wir kürzen bei 3900
mit Marker. Reasoning-Content / Zwischenstand-Text werden NICHT
gesendet — Telegram bekommt nur die fertige Antwort.

Token kommt aus .env (siehe .env.example). Ein Token kann jeweils nur
EINEN gleichzeitig laufenden Bot bedienen — falls ein anderer Prozess
auf demselben Token pollt, gibt's TelegramConflictError.
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

# .env laden BEVOR irgendwas anderes Env-Vars liest
ENV_PATH = Path(__file__).parent / ".env"
load_env(ENV_PATH)


# core erst nach .env-Load importieren — falls core Env-Vars liest.
from core import run as core_run, MEMORY_PATH  # noqa: E402
from memory import MemoryStore  # noqa: E402
from background import Background  # noqa: E402


logger = logging.getLogger("soma.telegram")

TELEGRAM_MAX = 3900  # Sicherheits-Marge unter Telegram-Limit 4096
# Inbox liegt im workspace/ neben den memories — wird via core.MEMORY_PATH abgeleitet
INBOX = Path(MEMORY_PATH).parent / "inbox"
INBOX.mkdir(parents=True, exist_ok=True)
MAX_FILE_BYTES = 20 * 1024 * 1024  # Telegram-Bot-API-Limit


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
        text = text[:TELEGRAM_MAX] + f"\n…[{len(text)} Zeichen gekürzt]"
    try:
        await message.answer(text)
    except Exception as exc:
        logger.exception("send failed: %s", exc)


def _check_owner(message: Message, owner_id: int) -> bool:
    """Single-User-Check. Andere User → ignorieren mit minimaler Meldung."""
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

    # Background-Kurator: async Task neben dem Polling. Queue-getrieben,
    # kein Polling-Timer. Wird nach jedem foreground turn gefüttert.
    # bot+owner_id geben dem Background proaktive Send-Capability.
    bg_queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    background = Background(bg_queue, bot=bot, owner_id=owner_id)
    bg_task = asyncio.create_task(background.run(), name="soma-background")

    # Message-Debouncing: schnelle Folge-Nachrichten zusammenfassen damit
    # aiogram nicht zwei parallele core.run() startet. Beim Eintreffen
    # einer Message wird ein 3-s-Timer gesetzt; weitere Messages
    # resetten den Timer und werden gebuffert. Erst nach Timeout startet
    # core.run() mit dem zusammengefassten Text.
    DEBOUNCE_SECONDS = 3.0
    pending_buffer: list[str] = []
    pending_task: asyncio.Task | None = None
    buffer_lock = asyncio.Lock()
    # Serialisierungs-Lock: stellt sicher dass IMMER nur EIN core.run()
    # gleichzeitig läuft — auch bei messages die > DEBOUNCE_SECONDS
    # Abstand haben aber innerhalb der Laufzeit des vorherigen Runs
    # eintreffen. FIFO-Order garantiert chronologische Antworten.
    core_lock = asyncio.Lock()

    async def _execute_buffered(trigger_message: Message) -> None:
        nonlocal pending_task
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return  # Neue Nachricht angekommen → wir wurden ersetzt
        async with buffer_lock:
            if not pending_buffer:
                pending_task = None
                return
            buffered = list(pending_buffer)
            pending_buffer.clear()
            pending_task = None
        combined = "\n\n".join(buffered)
        # Serialisierung: nur 1 core.run gleichzeitig, FIFO-Reihenfolge.
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
                await _safe_send(trigger_message, f"Fehler: {exc}")
                return
            await _safe_send(trigger_message,
                              result.get("final_text", "(keine Antwort)"))
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

    # /start als Smoke-Test
    @dp.message(F.text.startswith("/start"))
    async def on_start(message: Message) -> None:
        if not _check_owner(message, owner_id):
            return
        await _safe_send(message, "Soma läuft. Schreib mir etwas.")

    # --- File-Handling ---
    # Datei rein → in inbox speichern → 3-s-Debounce → ALLE im Batch
    # sequenziell durch core.run verarbeitet (sequenziell, nicht parallel,
    # via core_lock). Pro Datei: extrahieren, relevante Fakten als memory
    # mit Tag 'from-file', EINE Zeile zurück. AM ENDE des Batches: GENAU
    # EINE Telegram-Nachricht — Liste aller Befunde + Frage was damit zu
    # tun ist. Kein Spam, kein „Datei liegt unter…"-Ack, keine
    # Zwischen-Meldungen pro Datei.
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
                " Note: binäres/strukturiertes Format — extrahieren im "
                "Container (pypdf/extract_msg/openpyxl), NIE ganzen Inhalt "
                "in den Context laden."
            )
        if caption:
            # Caption-Modus: Caption IST der Task. Volles core.run.
            return (
                f"{caption}\n\n"
                f"Datei dafür: {path} ({size} bytes, {kind}).{binary_hint}"
            )
        # Auto-Modus: extrahieren + relevante Memory schreiben + EINE Zeile
        # zurück. Keine Frage, kein Vorschlag — das macht der Batch-Summary.
        return (
            f"Datei: {path} ({size} bytes, {kind}).\n"
            f"Task:\n"
            f"1) Öffne die Datei per execute.\n"
            f"2) Extrahiere die wichtigsten Fakten.\n"
            f"3) Wenn substanzielle Fakten drin sind (Kunde, Angebots-Nr, "
            f"Produkt, Preise, Personen): schreibe sie als semantic oder "
            f"episodic Memory mit Tag 'from-file' + Domain-Tag — damit "
            f"sie nicht verloren gehen.\n"
            f"4) Antworte mit GENAU EINER Zeile im Format:\n"
            f"   <Typ>: <Kurzbeschreibung mit 2-3 Schlüssel-Fakten>\n"
            f"   z.B. 'Quote — Customer X, GPU-Server (2× GPU-Y, 512GB RAM)'\n"
            f"KEINE Frage, KEIN Vorschlag — beides kommt später im "
            f"Batch-Summary.{binary_hint}"
        )

    async def _process_files_silent(file_info: dict) -> tuple[str, dict]:
        """Eine Datei durch core.run schicken OHNE Telegram-Output.
        Returns (one_line_summary, run_result)."""
        prompt = _build_file_prompt(file_info)
        pre_count = len(MemoryStore(MEMORY_PATH).load())
        try:
            result = await core_run(prompt)
        except Exception as exc:
            logger.exception("core.run failed (file): %s", exc)
            return (f"[Fehler: {exc}]", {})
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
        return (result.get("final_text", "(nichts)").strip(), result)

    async def _execute_files_buffered(trigger_message: Message) -> None:
        """Debounce-Tail. Verarbeitet ALLE gepufferten Files sequenziell
        ohne Zwischen-Spam. Sendet EINE Telegram-Nachricht am Ende."""
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
        # Sequenziell durch core_lock — kein paralleler run, keine race.
        async with core_lock:
            async with ChatActionSender.typing(
                bot=trigger_message.bot,
                chat_id=trigger_message.chat.id,
            ):
                for f in queued:
                    summary, _ = await _process_files_silent(f)
                    results.append((f, summary))

        # Genau EINE Telegram-Nachricht für den ganzen Batch.
        any_caption = any((f.get("caption") or "") for f, _ in results)
        if len(results) == 1:
            f, s = results[0]
            if f.get("caption"):
                # Caption-Modus: Antwort war ein Task-Ergebnis, einfach senden.
                await _safe_send(trigger_message, s or "(keine Antwort)")
            else:
                # Auto-Modus: Summary + offene Frage
                await _safe_send(
                    trigger_message,
                    f"{s}\n\nWas soll ich damit machen?"
                )
            return
        # Mehrere Files: Liste + offene Frage. Captions (falls da) als
        # Task-Antworten ausgewiesen, der Rest mit Index.
        lines = [f"{len(results)} Dateien verarbeitet:"]
        for i, (f, s) in enumerate(results, 1):
            tag = f.get("safe_name", "?")
            lines.append(f"[{i}] {tag} — {s[:300]}")
        if not any_caption:
            lines.append("\nWas soll ich mit ihnen machen?")
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
                                 f"Datei zu groß ({size} bytes, Limit "
                                 f"{MAX_FILE_BYTES}).")
                return
            try:
                tg_file = await message.bot.get_file(obj.file_id)
            except Exception as exc:
                logger.exception("get_file failed: %s", exc)
                await _safe_send(message, f"Download fehlgeschlagen: {exc}")
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
                await _safe_send(message, f"Download fehlgeschlagen: {exc}")
                return
            caption = (message.caption or "").strip()
            container_path = f"/workspace/inbox/{dest.name}"
            logger.info("file received kind=%s size=%d → %s caption=%r",
                        kind, size, container_path, caption[:80])

        # In den File-Buffer einreihen + Debounce-Task (re)starten
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
    logger.info("soma telegram bot @%s gestartet, owner=%s",
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
