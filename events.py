"""
EventLog — append-only JSONL audit trail (P9).

Every relevant action is logged as a JSON line with a timestamp.
The state of the world can be reconstructed from this log.

Schema per line:
  {"ts": "ISO-8601", "type": "name", ...payload-keys}

Event types written by `core.run()`:
- prompt_received   {"user_message": ...}
- correction        {"trigger": "...", "last_response": "..."}
- model_call        {"iteration": N, "blocks": M, "chars": K}
- code_executed     {"iteration": N, "lang": "...", "ok": bool}
- response_sent     {"iterations": N, "blocks_executed": M, "final_text": "..."}
- error             {"where": "...", "message": "..."}
"""
import json
from datetime import datetime
from pathlib import Path


class EventLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, type: str, **data) -> dict:
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "type": type,
            **data,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        items: list[dict] = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return items

    def by_type(self, type: str) -> list[dict]:
        return [e for e in self.load() if e.get("type") == type]

    def recent(self, n: int = 20) -> list[dict]:
        return self.load()[-n:]

    def last(self, type: str) -> dict | None:
        for e in reversed(self.load()):
            if e.get("type") == type:
                return e
        return None

    def since(self, iso_timestamp: str) -> list[dict]:
        return [e for e in self.load() if e.get("ts", "") >= iso_timestamp]

    def recent_turns(self, n: int = 3) -> list[dict]:
        """Return the last N (user, assistant) pairs as dicts.
        Pairs a prompt_received event with the next response_sent event.
        Orphan prompts (no matching response) are ignored."""
        pairs: list[dict] = []
        current_prompt: dict | None = None
        for ev in self.load():
            t = ev.get("type")
            if t == "prompt_received":
                current_prompt = ev
            elif t == "response_sent" and current_prompt is not None:
                pairs.append({
                    "user": current_prompt.get("user_message", ""),
                    "assistant": ev.get("final_text", ""),
                    "ts": current_prompt.get("ts", ""),
                })
                current_prompt = None
        return pairs[-n:]


def render_recent_turns(pairs: list[dict], max_chars: int = 200) -> str:
    """Render turn pairs as a compact system block for conversation continuity."""
    if not pairs:
        return ""
    lines = [
        "Recent conversations (you remember this history — "
        "use it when relevant):"
    ]
    for p in pairs:
        u = (p.get("user") or "")[:max_chars]
        a = (p.get("assistant") or "")[:max_chars]
        if len(p.get("user") or "") > max_chars:
            u += "…"
        if len(p.get("assistant") or "") > max_chars:
            a += "…"
        lines.append(f"[User said]: {u}")
        lines.append(f"[I replied]: {a}")
        lines.append("")
    return "\n".join(lines).rstrip()


# --- Correction signal rendering (P6 closed-loop) ---
#
# Detection itself is done by the LLM classifier in core.classify_correction —
# language-neutral. These helpers only RENDER the signal once detection has
# fired.


def build_correction_note(last_response_event: dict | None,
                          trigger: str,
                          user_message: str) -> str:
    """Render the correction signal as a system-message body."""
    if not last_response_event:
        return ""
    last_text = (last_response_event.get("final_text") or "")[:500]
    return (
        "CORRECTION-SIGNAL: the user reacted with \""
        f"{user_message}\" to your previous reply.\n"
        f"Previous reply was:\n---\n{last_text}\n---\n"
        f"Adapt your next reply. If the hint is a lasting preference "
        f"(style, length, language), persist it as an episodic or "
        f"semantic memory via a shell block."
    )


# --- Broken-promise note rendering (anti-hallucination) ---
#
# Detection is done by core.classify_memory_promise. The caller passes the
# classification result into this builder.


def build_broken_promise_note(last_response_event: dict | None,
                              promise_signal: str = "") -> str:
    """Render the broken-promise reminder when the classifier flagged the
    previous reply as a memory-write promise WITHOUT a code block."""
    if not last_response_event or not promise_signal:
        return ""
    if last_response_event.get("blocks_executed", 0) != 0:
        return ""
    last_text = last_response_event.get("final_text") or ""
    return (
        "BROKEN-PROMISE: in your previous reply you claimed to have "
        "remembered / noted / saved something — but you wrote NO "
        "memory.\n"
        f"Previous reply:\n---\n{last_text[:400]}\n---\n"
        "Look at the current user message AND the recent conversations, "
        "extract the facts worth remembering and write the JSONL "
        "line(s) NOW via shell-echo. NO filler phrase without a write."
    )
