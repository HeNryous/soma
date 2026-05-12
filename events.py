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


# --- Correction detection (P6 closed-loop) ---

import re as _re

# Matched with word boundaries to avoid false positives (e.g. "no" must
# not fire inside "node" / "nochmal"). For German triggers we keep the
# substring path because some phrases contain spaces ("stimmt nicht").
CORRECTION_PATTERNS = (
    "no", "not", "wrong", "shorter", "different", "actually",
    "doesn't fit", "again", "too long", "too short", "too much",
    "not like that", "do it differently",
    # German triggers — user may write in German
    "nein", "kürzer", "knapper", "anders", "falsch", "stimmt nicht",
    "passt nicht", "nochmal", "zu lang", "zu kurz", "zu viel",
)

_CORRECTION_REGEXES = tuple(
    (kw, _re.compile(r"\b" + _re.escape(kw) + r"\b", _re.IGNORECASE))
    for kw in CORRECTION_PATTERNS
)


def detect_correction(user_message: str) -> str | None:
    """Return the trigger string when the message looks like a correction of
    the previous reply — otherwise None. Heuristic, not NLP."""
    msg = user_message.lower().strip()
    # Corrections are usually short. Skip long sentences.
    if len(msg) > 100:
        return None
    for kw, rx in _CORRECTION_REGEXES:
        if rx.search(msg):
            return kw
    return None


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


# --- Broken-promise detection (anti-hallucination) ---

MEMORY_PROMISE_PATTERNS = (
    # English filler phrases
    "noted", "i'll note", "i will note", "i'll remember",
    "i will remember", "remembered", "saving that", "saved that",
    "i'll save", "i will save", "writing that down", "wrote that down",
    # German filler phrases (model may answer in German)
    "gemerkt", "ich merke", "ich merk ", "ich werde mir merken",
    "ich speicher", "gespeichert", "speichere das",
    "ich notier", "notiert",
    "ich schreib mir auf", "schreib das auf",
    "ich behalte", "behalt ich",
    "ich habe es",
)


def detect_memory_promise(text: str) -> str | None:
    """Return the matched phrase when the text contains a memory-write
    promise (filler-phrase detector). If the model says such a thing
    without running a code block → hallucination signal."""
    msg = (text or "").lower()
    for pat in MEMORY_PROMISE_PATTERNS:
        if pat in msg:
            return pat
    return None


def build_broken_promise_note(last_response_event: dict | None) -> str:
    """If the last reply said 'noted' / 'gemerkt' but no memory write
    happened → reminder injected into the current iteration."""
    if not last_response_event:
        return ""
    if last_response_event.get("blocks_executed", 0) != 0:
        return ""
    last_text = last_response_event.get("final_text") or ""
    phrase = detect_memory_promise(last_text)
    if not phrase:
        return ""
    return (
        f"BROKEN-PROMISE: in your previous reply you said \"{phrase}…\""
        f" — but you wrote NO memory.\n"
        f"Previous reply:\n---\n{last_text[:400]}\n---\n"
        f"Look at the current user message AND the recent conversations, "
        f"extract the facts worth remembering and write the JSONL "
        f"line(s) NOW via shell-echo. NO filler phrase without a write."
    )
