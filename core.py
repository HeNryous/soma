"""
Soma Core — PTC loop without tool schema.

The model writes Markdown code blocks (```python``` or ```shell```).
Soma extracts them via regex and runs them in the sandbox container.
The result is appended back as a role="user" message. Loop until the
model no longer writes a block.

Model-agnostic: no tool-call format, no model-specific parser.
Any model that can answer in Markdown code blocks works.
"""
import asyncio
import json
import logging
import re
import sys

import httpx

from memory import (MemoryStore, format_for_prompt,
                    format_for_prompt_selected, render_behaviors,
                    read_context_selection)
from events import (EventLog, build_correction_note,
                    render_recent_turns, build_broken_promise_note)
from self_model import summarize as self_summarize


import os as _os
from pathlib import Path as _Path

# Auto-load .env next to this module so CLI, status.py and tests see
# the same env vars as the Telegram bot.
try:
    from envfile import load_env as _load_env
    _env_path = _Path(__file__).parent / ".env"
    if _env_path.exists():
        _load_env(_env_path)
except Exception:
    pass

__version__ = "0.2.1"

VLLM_URL = _os.environ.get("VLLM_BASE_URL",
                           "http://localhost:8000/v1").rstrip("/") \
           + "/chat/completions"
MODEL = _os.environ.get("VLLM_MODEL", "your-model-name")
CONTAINER = _os.environ.get("SOMA_CONTAINER", "soma-sandbox")
_SOMA_ROOT = _os.environ.get("SOMA_ROOT",
                              _os.path.dirname(_os.path.abspath(__file__)))
# Runtime data lives under SOMA_DATA (default: <repo>/data/) so the
# repo checkout itself only contains code, no state.
_SOMA_DATA = _os.environ.get("SOMA_DATA",
                              _os.path.join(_SOMA_ROOT, "data"))
MEMORY_PATH = _os.path.join(_SOMA_DATA, "workspace", "memories.jsonl")
EVENT_PATH = _os.path.join(_SOMA_DATA, "events.jsonl")

MAX_ITERATIONS = 100
EXEC_TIMEOUT = 30
OUTPUT_MAX = 4000

# #8 Context-Compression
COMPRESS_THRESHOLD_MESSAGES = 40
COMPRESS_THRESHOLD_CHARS = 200_000
COMPRESS_HEAD_KEEP = 5      # system + first 2 user/assistant pairs
COMPRESS_TAIL_KEEP = 10
COMPRESS_TOOL_SHORTEN = 500  # tool-result chars > N → 1-line summary
COMPRESS_COOLDOWN_ITERS = 10  # earliest re-trigger after N more iters

# #12 Conflict-Detection
CONFLICT_MIN_TAG_OVERLAP = 2

BASE_PROMPT = """\
You are a helpful companion. Answer in the language the user is
talking to you in.

Always address the user DIRECTLY (you / your). Even when memories
phrase facts in the third person ("Alex works at Acme", "Alex looks
after Beta"), translate them when replying ("you work at Acme",
"you look after Beta"). NEVER talk ABOUT the user in third person —
not even as analysis or recommendation.

If a "Behavior Rules" block appears below: it has absolute priority.
Follow it on EVERY reply, even for pure knowledge questions.

If a task requires an action (create a file, run code, process data),
write a Markdown code block with language `shell` or `python`. EVERY
such code block in your reply is automatically executed in the sandbox
container. You receive stdout and stderr in the next round.

Example — create a file:
```shell
echo 'Hello' > /workspace/test.txt
```

Example — Python:
```python
print(2 ** 100)
```

Rules:
- NEVER claim you did something without writing a code block.
- If no action is needed (e.g. a pure knowledge question), answer as
  text without a code block. That ends the round.
- Workspace is `/workspace/` inside the container — that's where state
  lives.
- Keep replies compact. No markdown bullets, no tables.

Memories
------------
Your long-term memory lives in `/workspace/memories.jsonl`. One JSON
line per memory. Three types:
- `semantic`: facts / preferences ("User prefers metric units")
- `procedural`: learned procedures (short how-to with example)
- `episodic`: things that happened (events, lessons from a day)

When you should remember something, write a shell block that appends
the memory as a JSON line. Write actively — not only when the user
explicitly says "remember this". Triggers:
- user names a role/job/work context → tag "role" or "identity"
- user mentions domain terms / definitions → tag "domain"
- user corrects you or states a preference → tag "preference" or "correction"
- important facts about user, company, projects → tag "fact"
- **extracted from processed files**: customers, product numbers,
  partners, recurring structures, configurations → tag "fact" +
  domain tag (e.g. "manufacturer", "customer", "product"). Next time
  the same object shows up, memory speeds up recognition.

**Correction detection — TWO STAGES (very important):**

If a user message contains negations ("not", "no"), contradictions
("wrong", "actually not", "different"), or clarifications
("actually…", "that has nothing to do with X") — REGARDLESS of length —
handle it in two stages:

**Stage 1 — Fact extraction (BEFORE you reply):**
Read the message AGAIN and list for yourself:
- What was WRONG in my last reply?
- Which NEW facts/entities does the user mention?
- Which ASSIGNMENTS does the user correct (X is not Y, it's Z)?
For EACH new fact: write a separate `semantic` memory via shell-echo
BEFORE you give the textual reply.

**Stage 2 — Reply:**
Only after all memories are written, reply to the user. Filler phrases
like "I'll remember that" without an actual memory write are forbidden —
write the JSONL line(s) FIRST, then give the confirmation.

Example: user: "That has nothing to do with Beta — I look after Acme,
Beta and Cloudly."
Correct reaction = FIRST three shell blocks (each one
`echo '{...}' >> /workspace/memories.jsonl`), THEN text "Noted three
customers: Acme, Beta, Cloudly."

JSON must be on ONE line:

```shell
echo '{"type":"semantic","content":"User prefers metric units","tags":["preference"]}' >> /workspace/memories.jsonl
```

Write memories SPARINGLY but ACTIVELY. Rule of thumb: if a fact would
still be relevant next week, write it. If a fact is already in the
listed memories below: don't write it again.
"""


def build_system_prompt(memory_block: str) -> str:
    if memory_block:
        return BASE_PROMPT + "\n\nKnown memories:\n" + memory_block
    return BASE_PROMPT


CODE_BLOCK_RE = re.compile(
    r"```(python|shell|bash|sh)\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def extract_code_blocks(text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    for m in CODE_BLOCK_RE.finditer(text or ""):
        raw_lang = m.group(1).lower()
        lang = "shell" if raw_lang in ("shell", "bash", "sh") else "python"
        code = m.group(2).strip()
        if code:
            blocks.append((lang, code))
    return blocks


async def execute(language: str, code: str) -> dict:
    if language == "python":
        cmd = ["docker", "exec", "-i", CONTAINER, "python", "-c", code]
    else:
        cmd = ["docker", "exec", "-i", CONTAINER, "sh", "-c", code]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=EXEC_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "out": "", "err": f"timeout {EXEC_TIMEOUT}s"}
    out = stdout.decode("utf-8", "replace").strip()
    err = stderr.decode("utf-8", "replace").strip()
    if len(out) > OUTPUT_MAX:
        out = out[:OUTPUT_MAX] + f"\n…[{len(out)} chars total]"
    if len(err) > OUTPUT_MAX:
        err = err[:OUTPUT_MAX] + "\n…[truncated]"
    return {"ok": proc.returncode == 0, "out": out, "err": err}


BUDGET_SUMMARY_PROMPT = (
    "You have reached the iteration budget. Answer NOW as text — a "
    "compact summary of what you did and found, plus what's still "
    "open. NO further code blocks, just prose. Stick to the Behavior "
    "Rules above."
)

COMPRESS_SYSTEM_PROMPT = (
    "You are a conversation compressor. You receive the middle "
    "section of messages from a running agentic loop. Produce a "
    "STRUCTURED summary in four sections, each 1-3 compact sentences:\n"
    "Resolved: what was completed (files, results, actions)\n"
    "Pending: what's still open / unanswered\n"
    "Decisions: which decisions were made along the way\n"
    "Results: concrete outputs (file names, IDs, values, facts)\n"
    "Answer ONLY in this format. No code blocks, no surrounding "
    "explanation."
)


def _msg_chars(messages: list[dict]) -> int:
    return sum(len(m.get("content") or "") for m in messages)


def _shorten_tool_result(content: str) -> str:
    """Shorten tool-result to a 1-line summary. First line + length info."""
    if len(content) <= COMPRESS_TOOL_SHORTEN:
        return content
    first_line = content.splitlines()[0] if content else ""
    return f"[tool-result {len(content)} chars] {first_line[:120]}…"


def _looks_like_tool_result(msg: dict) -> bool:
    """Heuristic: a user message with the '[CODE-RESULT' prefix is a
    tool result from our pipeline."""
    if msg.get("role") != "user":
        return False
    c = msg.get("content") or ""
    return c.startswith("[CODE-RESULT")


async def compress_middle(client: httpx.AsyncClient,
                          messages: list[dict]) -> list[dict]:
    """Head-Middle-Tail compression. Returns a new message list.

    Strategy:
    - Head (first COMPRESS_HEAD_KEEP) is kept intact.
    - Tail (last COMPRESS_TAIL_KEEP) is kept intact.
    - Middle: all tool-results > COMPRESS_TOOL_SHORTEN get shortened;
      then the entire middle is compressed into ONE system summary.
    """
    if len(messages) <= COMPRESS_HEAD_KEEP + COMPRESS_TAIL_KEEP + 2:
        return messages
    head = messages[:COMPRESS_HEAD_KEEP]
    tail = messages[-COMPRESS_TAIL_KEEP:]
    middle = messages[COMPRESS_HEAD_KEEP:-COMPRESS_TAIL_KEEP]

    # 1) Pre-shorten middle tool-results — saves tokens for the LLM call
    shortened: list[dict] = []
    for m in middle:
        if _looks_like_tool_result(m):
            shortened.append({"role": "user",
                              "content": _shorten_tool_result(
                                  m.get("content") or "")})
        else:
            shortened.append(m)

    # 2) LLM call: middle → structured summary
    middle_text = "\n\n".join(
        f"[{m.get('role')}]: {(m.get('content') or '')[:1500]}"
        for m in shortened
    )
    try:
        summary = await call_model(client, [
            {"role": "system", "content": COMPRESS_SYSTEM_PROMPT},
            {"role": "user", "content": middle_text},
        ])
    except Exception:
        # On error: keep shortened middle instead of summary
        return head + shortened + tail
    if not summary:
        return head + shortened + tail

    summary_msg = {"role": "system",
                   "content": f"[middle-compression]\n{summary}"}
    return head + [summary_msg] + tail


async def call_model(client: httpx.AsyncClient, messages: list[dict],
                     *, max_tokens: int = 4096,
                     temperature: float = 0.7) -> str:
    r = await client.post(
        VLLM_URL,
        json={
            "model": MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=180.0,
    )
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    # Reasoning models can have content=null; reasoning_content is the fallback.
    return (msg.get("content") or msg.get("reasoning_content") or "").strip()


CLASSIFIER_SYSTEM = (
    "You are a strict binary classifier. Reply with exactly one token: "
    "YES or NO. No punctuation, no explanation."
)


async def classify_correction(client: httpx.AsyncClient,
                              user_message: str,
                              prev_bot_text: str) -> str | None:
    """Language-neutral correction detection. Returns "correction" when
    the user message looks like a correction/contradiction/pushback
    against the previous assistant reply, else None.

    Cheap pre-filter: skip when no previous reply or user message is
    long (corrections are short reactions, not new tasks)."""
    if not user_message or not prev_bot_text:
        return None
    if len(user_message) > 200:
        return None
    prompt = (
        "Decide whether the user message is a correction, contradiction, "
        "or pushback against the previous assistant reply.\n\n"
        f"Previous assistant reply: {prev_bot_text[:500]}\n"
        f"User message: {user_message[:300]}\n\n"
        "Answer YES or NO."
    )
    try:
        text = await call_model(
            client,
            [{"role": "system", "content": CLASSIFIER_SYSTEM},
             {"role": "user", "content": prompt}],
            max_tokens=5,
            temperature=0.0,
        )
    except Exception:
        return None
    return "correction" if text.strip().upper().startswith("YES") else None


async def classify_memory_promise(client: httpx.AsyncClient,
                                  bot_text: str,
                                  blocks_executed: int) -> str | None:
    """Language-neutral broken-promise detection. Returns "memory_promise"
    when the bot's reply claims it just remembered / noted / saved
    something WITHOUT actually running a code block, else None.

    Cheap pre-filter: only run when blocks_executed == 0."""
    if not bot_text or blocks_executed != 0:
        return None
    prompt = (
        "Decide whether the assistant reply makes an explicit promise or "
        "claim that it just remembered, noted, saved, recorded, or wrote "
        "down a piece of information.\n\n"
        f"Assistant reply: {bot_text[:500]}\n\n"
        "Answer YES or NO."
    )
    try:
        text = await call_model(
            client,
            [{"role": "system", "content": CLASSIFIER_SYSTEM},
             {"role": "user", "content": prompt}],
            max_tokens=5,
            temperature=0.0,
        )
    except Exception:
        return None
    return "memory_promise" if text.strip().upper().startswith("YES") else None


def format_result(idx: int, total: int, lang: str, result: dict) -> str:
    parts = [f"[CODE-RESULT block {idx}/{total} language={lang}]"]
    if result.get("out"):
        parts.append(f"STDOUT:\n{result['out']}")
    if result.get("err"):
        parts.append(f"STDERR:\n{result['err']}")
    if not result.get("ok"):
        parts.append("(exit != 0)")
    return "\n".join(parts) if len(parts) > 1 else parts[0] + "\n(no output)"


async def run(user_message: str) -> dict:
    """
    Run one agentic loop against vLLM. Returns statistics including
    final_text, iterations and blocks_executed.
    """
    log = logging.getLogger("soma")
    store = MemoryStore(MEMORY_PATH)
    events = EventLog(EVENT_PATH)
    # Auto-prune when memory count > 200 → keep 100 + all style memories
    initial = store.load()
    if len(initial) > 200:
        removed = store.prune(keep=100)
        events.log("memory_pruned", removed=removed,
                   kept_after=len(store.load()))
        log.info("auto-prune: removed %d memories", removed)
    # Build the memory block: if the curator wrote a selection AND
    # there are enough memories → split layout (relevant context + further).
    # Otherwise fall back to all memories grouped by type.
    workspace_dir = _Path(MEMORY_PATH).parent
    all_mems_for_prompt = store.load()
    selection_ids = read_context_selection(workspace_dir)
    if selection_ids and len(all_mems_for_prompt) >= 15:
        memory_block = format_for_prompt_selected(
            all_mems_for_prompt, selection_ids, user_message)
        log.info("loaded %d memories (selection: %d ids)",
                 len(all_mems_for_prompt), len(selection_ids))
    else:
        memory_block = format_for_prompt(all_mems_for_prompt)
        log.info("loaded %d memories (fallback render)",
                 len(all_mems_for_prompt))

    # Read conversation history BEFORE logging the current prompt.
    recent_pairs = events.recent_turns(n=3)
    # #12 pre-existing conflicts: everything since the last prompt_received
    last_prompt = events.last("prompt_received")
    last_prompt_ts = (last_prompt or {}).get("ts", "")
    pending_conflicts = [
        e for e in events.by_type("memory_conflict_detected")
        if e.get("ts", "") > last_prompt_ts
    ]
    # Snapshot memory IDs for conflict detection AFTER run()
    initial_mem_ids = {m.get("id") for m in store.load()}

    events.log("prompt_received", user_message=user_message)

    last_resp_for_promise = events.last("response_sent")
    prev_bot_text = (last_resp_for_promise or {}).get("final_text", "")
    prev_blocks = (last_resp_for_promise or {}).get("blocks_executed", 0)

    blocks_executed = 0
    final_text = ""
    last_compression_iter = 0  # #8 cooldown counter

    async with httpx.AsyncClient() as client:
        # Two language-neutral classifications, in parallel:
        #   1) Is the new user message a correction of the previous reply?
        #   2) Did the previous reply make a memory-write promise without a code block?
        correction_trigger, promise_signal = await asyncio.gather(
            classify_correction(client, user_message, prev_bot_text),
            classify_memory_promise(client, prev_bot_text, prev_blocks),
        )

        broken_promise_note = build_broken_promise_note(
            last_resp_for_promise, promise_signal or "")
        if broken_promise_note:
            events.log("memory_promise_unfulfilled",
                       prior_response=prev_bot_text[:200])
            log.info("broken-promise detected")
            # Auto-capture: persist the PREVIOUS user message as a memory.
            # That was the statement about which the bot claimed to remember.
            prev_user_msg = ""
            for ev in reversed(events.load()[:-1]):
                if ev.get("type") == "prompt_received":
                    prev_user_msg = ev.get("user_message", "")
                    break
            if prev_user_msg and len(prev_user_msg) > 50:
                captured = store.append(
                    "semantic",
                    prev_user_msg[:500],
                    tags=["auto-captured", "from-broken-promise"],
                )
                events.log("memory_auto_captured",
                           reason="broken_promise",
                           content_preview=prev_user_msg[:120],
                           memory_id=captured.get("id"))
                log.info("auto-captured prev user-msg as memory: %s",
                         prev_user_msg[:80])
                # Reload memory_block so the new entry shows up in the prompt
                reloaded = store.load()
                if selection_ids and len(reloaded) >= 15:
                    memory_block = format_for_prompt_selected(
                        reloaded, selection_ids, user_message)
                else:
                    memory_block = format_for_prompt(reloaded)

        correction_note = ""
        if correction_trigger:
            correction_note = build_correction_note(
                last_resp_for_promise, correction_trigger, user_message)
            if correction_note:
                events.log("correction", trigger=correction_trigger,
                           last_response=prev_bot_text[:200])
                log.info("correction detected")

        messages: list[dict] = [
            {"role": "system", "content": build_system_prompt(memory_block)},
        ]
        self_awareness = self_summarize(store, events)
        if self_awareness:
            messages.append({"role": "system", "content": self_awareness})
        # Broken-promise note must come before correction — higher priority.
        if broken_promise_note:
            messages.append({"role": "system", "content": broken_promise_note})
        # #12 conflict reminder: unresolved conflicts from the previous turn
        if pending_conflicts:
            items = "\n".join(
                f"- existing [{c.get('existing_id','?')}]: {c.get('existing_content','')[:100]}\n"
                f"  vs new [{c.get('new_id','?')}]: {c.get('new_content','')[:100]}"
                for c in pending_conflicts[-3:]
            )
            messages.append({"role": "system", "content": (
                "MEMORY CONFLICTS from the previous turn (not blocking, but "
                "check whether an update or clarification is needed):\n" + items
            )})
        if correction_note:
            messages.append({"role": "system", "content": correction_note})
        # Recency bias: repeat style memories directly before the user message.
        behavior_reminder = render_behaviors(store.load())
        if behavior_reminder:
            messages.append({"role": "system", "content": behavior_reminder})
        # Reconstruct multi-turn conversation from the event log.
        for p in recent_pairs:
            if p.get("user"):
                messages.append({"role": "user", "content": p["user"]})
            if p.get("assistant"):
                messages.append({"role": "assistant", "content": p["assistant"]})
        messages.append({"role": "user", "content": user_message})
        for iteration in range(1, MAX_ITERATIONS + 1):
            # #8 compression check: test threshold before every model call
            need_compress = (
                (len(messages) > COMPRESS_THRESHOLD_MESSAGES
                 or _msg_chars(messages) > COMPRESS_THRESHOLD_CHARS)
                and (iteration - last_compression_iter) >= COMPRESS_COOLDOWN_ITERS
            )
            if need_compress:
                before_msgs = len(messages)
                before_chars = _msg_chars(messages)
                messages = await compress_middle(client, messages)
                last_compression_iter = iteration
                events.log("compression_applied", iteration=iteration,
                           msgs_before=before_msgs,
                           msgs_after=len(messages),
                           chars_before=before_chars,
                           chars_after=_msg_chars(messages))
                log.info("compressed: %d→%d msgs, %d→%d chars",
                         before_msgs, len(messages),
                         before_chars, _msg_chars(messages))

            text = await call_model(client, messages)
            blocks = extract_code_blocks(text)
            log.info("iter %d: %d blocks, %d chars", iteration, len(blocks), len(text))
            events.log("model_call", iteration=iteration,
                       blocks=len(blocks), chars=len(text))

            if not blocks:
                final_text = text
                messages.append({"role": "assistant", "content": text})
                events.log("response_sent", iterations=iteration,
                           blocks_executed=blocks_executed,
                           final_text=final_text[:1000])
                _post_run_maintenance(events, store,
                                      initial_mem_ids=initial_mem_ids)
                return {
                    "ok": True,
                    "iterations": iteration,
                    "blocks_executed": blocks_executed,
                    "final_text": final_text,
                    "messages": messages,
                }

            messages.append({"role": "assistant", "content": text})
            results: list[str] = []
            for i, (lang, code) in enumerate(blocks, start=1):
                result = await execute(lang, code)
                blocks_executed += 1
                results.append(format_result(i, len(blocks), lang, result))
                log.info("  exec %d/%d lang=%s ok=%s", i, len(blocks), lang,
                         result.get("ok"))
                events.log("code_executed", iteration=iteration,
                           lang=lang, ok=result.get("ok", False),
                           code_chars=len(code),
                           code_snippet=code[:200])
            messages.append({"role": "user", "content": "\n\n".join(results)})

        # Budget reached — summary call without further code blocks.
        events.log("error", where="run", message="budget_exhausted",
                   iterations=MAX_ITERATIONS,
                   blocks_executed=blocks_executed)
        summary_messages = messages + [
            {"role": "system", "content": BUDGET_SUMMARY_PROMPT}
        ]
        try:
            final_text = await call_model(client, summary_messages)
        except Exception as exc:
            logger.exception("budget summary call failed: %s", exc)
            final_text = (f"(budget reached after {MAX_ITERATIONS} "
                          f"iterations, summary call failed: {exc})")
        if not final_text:
            final_text = (f"(budget reached after {MAX_ITERATIONS} "
                          "iterations — no summary output)")
        events.log("response_sent", iterations=MAX_ITERATIONS,
                   blocks_executed=blocks_executed,
                   final_text=final_text[:1000])

    _post_run_maintenance(events, store, initial_mem_ids=initial_mem_ids)
    return {
        "ok": False,
        "iterations": MAX_ITERATIONS,
        "blocks_executed": blocks_executed,
        "final_text": final_text,
        "messages": messages,
    }


AUTO_CRYSTALLIZE_THRESHOLD = 20


def _post_run_maintenance(events: EventLog, store: MemoryStore,
                          threshold: int = AUTO_CRYSTALLIZE_THRESHOLD,
                          initial_mem_ids: set | None = None) -> None:
    """After every run: auto-fuse, auto-crystallize, conflict-detect.
    initial_mem_ids: memory IDs at the START of the run (for conflict-detect)."""
    log = logging.getLogger("soma")
    # #12 conflict detection: check every memory written during this run
    if initial_mem_ids is not None:
        try:
            current = store.load()
            new_mems = [m for m in current
                        if m.get("id") not in initial_mem_ids]
            for nm in new_mems:
                conflicts = store.find_tag_conflicts(
                    nm, min_tag_overlap=CONFLICT_MIN_TAG_OVERLAP)
                for c in conflicts:
                    shared = (set(t.lower() for t in nm.get("tags", []))
                              & set(t.lower() for t in c.get("tags", [])))
                    events.log("memory_conflict_detected",
                               new_id=nm.get("id"),
                               new_content=nm.get("content", "")[:120],
                               existing_id=c.get("id"),
                               existing_content=c.get("content", "")[:120],
                               shared_tags=sorted(shared))
                    log.info("memory_conflict: new=%s vs existing=%s",
                             nm.get("id"), c.get("id"))
        except Exception as exc:
            log.exception("conflict-detection failed: %s", exc)

    # Auto-fusion: every run — cheap operation
    try:
        merged = store.fuse()
        if merged > 0:
            events.log("memory_fused", count=merged)
            log.info("auto-fuse merged %d memories", merged)
    except Exception as exc:
        log.exception("auto-fuse failed: %s", exc)
        events.log("error", where="auto_fuse", message=str(exc))

    # Auto-Crystallize: only if enough new code_executed since last trigger
    try:
        last = events.last("auto_crystallized")
        since_ts = last["ts"] if last else ""
        new_code_events = [e for e in events.by_type("code_executed")
                           if e.get("ts", "") > since_ts]
        if len(new_code_events) < threshold:
            return
        from crystallize import crystallize as do_crystallize
        written = do_crystallize(events_path=str(events.path),
                                 memory_path=str(store.path),
                                 threshold=3)
        events.log("auto_crystallized",
                   new_skills=len(written),
                   code_events_window=len(new_code_events))
        log.info("auto-crystallize: %d code events → %d new skills",
                 len(new_code_events), len(written))
    except Exception as exc:
        log.exception("auto-crystallize failed: %s", exc)
        events.log("error", where="auto_crystallize", message=str(exc))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if len(sys.argv) < 2:
        print("usage: python core.py <user-message>", file=sys.stderr)
        return 2
    if sys.argv[1] in ("--version", "-V"):
        print(f"soma {__version__}")
        return 0
    user_message = " ".join(sys.argv[1:])
    result = asyncio.run(run(user_message))
    print(json.dumps({
        "ok": result["ok"],
        "iterations": result["iterations"],
        "blocks_executed": result["blocks_executed"],
        "final_text": result["final_text"][:500],
    }, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
