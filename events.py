"""
EventLog — Append-Only JSONL Audit Trail (P9).

Jede relevante Handlung wird als JSON-Zeile mit Timestamp geloggt.
State der Welt ist aus dem Log rekonstruierbar.

Schema pro Zeile:
  {"ts": "ISO-8601", "type": "name", ...payload-keys}

Event-Typen die `core.run()` schreibt:
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
        """Liefert die letzten N (user, assistant)-Paare als Dicts.
        Paart prompt_received-Event mit dem nächsten response_sent-Event.
        Verwaiste prompts (kein response davor) werden ignoriert."""
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
    """Render Turn-Paare als kompakter System-Block für Conversation Continuity."""
    if not pairs:
        return ""
    lines = [
        "Recent conversations (du erinnerst dich an diesen Verlauf — "
        "nutze ihn wenn relevant):"
    ]
    for p in pairs:
        u = (p.get("user") or "")[:max_chars]
        a = (p.get("assistant") or "")[:max_chars]
        if len(p.get("user") or "") > max_chars:
            u += "…"
        if len(p.get("assistant") or "") > max_chars:
            a += "…"
        lines.append(f"[Du sagtest]: {u}")
        lines.append(f"[Ich antwortete]: {a}")
        lines.append("")
    return "\n".join(lines).rstrip()


# --- Korrektur-Detektion (P6 Closed-Loop) ---

CORRECTION_PATTERNS = (
    "nein", "kürzer", "knapper", "anders", "falsch", "stimmt nicht",
    "passt nicht", "nochmal", "zu lang", "zu kurz", "zu viel",
    "nicht so", "anders machen",
)


def detect_correction(user_message: str) -> str | None:
    """Liefert den Trigger-String falls die Message wie eine Korrektur
    der letzten Antwort aussieht — sonst None. Heuristik, keine NLP."""
    msg = user_message.lower().strip()
    # Korrekturen sind meistens kurz. Längere Sätze nicht als Korrektur werten.
    if len(msg) > 100:
        return None
    for kw in CORRECTION_PATTERNS:
        if kw in msg:
            return kw
    return None


def build_correction_note(last_response_event: dict | None,
                          trigger: str,
                          user_message: str) -> str:
    """Render das Korrektur-Signal als System-Message-Inhalt."""
    if not last_response_event:
        return ""
    last_text = (last_response_event.get("final_text") or "")[:500]
    return (
        "CORRECTION-SIGNAL: Der User hat mit \""
        f"{user_message}\" auf deine letzte Antwort reagiert.\n"
        f"Letzte Antwort war:\n---\n{last_text}\n---\n"
        f"Passe deine nächste Antwort an. Wenn der Hinweis eine "
        "dauerhafte Präferenz ist (Stil, Länge, Sprache), schreibe "
        "eine episodische oder semantische Memory über shell-Block."
    )


# --- Broken-Promise-Detection (Anti-Halluzination) ---

MEMORY_PROMISE_PATTERNS = (
    "gemerkt", "ich merke", "ich merk ", "ich werde mir merken",
    "ich speicher", "gespeichert", "speichere das",
    "ich notier", "notiert",
    "ich schreib mir auf", "schreib das auf",
    "ich behalte", "behalt ich",
    "ich habe es",
)


def detect_memory_promise(text: str) -> str | None:
    """Returns matched phrase wenn der Text ein Memory-Write-Versprechen
    macht (Floskel-Erkennung). Wenn the model so was sagt aber kein
    code-block ausgeführt wurde → Halluzinations-Signal."""
    msg = (text or "").lower()
    for pat in MEMORY_PROMISE_PATTERNS:
        if pat in msg:
            return pat
    return None


def build_broken_promise_note(last_response_event: dict | None) -> str:
    """Wenn die letzte Antwort 'gemerkt'/'notiert' sagte aber kein
    Memory-Write passierte → Reminder für die aktuelle Iteration."""
    if not last_response_event:
        return ""
    if last_response_event.get("blocks_executed", 0) != 0:
        return ""
    last_text = last_response_event.get("final_text") or ""
    phrase = detect_memory_promise(last_text)
    if not phrase:
        return ""
    return (
        f"BROKEN-PROMISE: In deiner letzten Antwort sagtest du „{phrase}…" + "\""
        f" — aber du hast KEINE Memory geschrieben.\n"
        f"Letzte Antwort:\n---\n{last_text[:400]}\n---\n"
        f"Schau dir die aktuelle User-Message UND die letzten Gespräche an, "
        f"extrahiere die zu merkenden Fakten und schreibe JETZT die JSONL-"
        f"Zeile(n) als shell-echo. KEINE Floskel ohne Write."
    )
