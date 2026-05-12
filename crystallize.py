"""
Skill-Kristallisation — extrahiert wiederkehrende Code-Patterns aus
events.jsonl und schreibt prozedurale Memories.

Hermes-Pattern (XSkill/SkillOS): nach N gleichartigen Tool-Calls wird die
Operation als wiederverwendbare Prozedur abstrahiert. Hier: nach >= 3
erfolgreichen Code-Blocks mit gleichem Pattern (lang + first-token).

Pattern-Beispiele:
  shell:echo, shell:cat, shell:ls, python:import, python:print

Memory-Schema beim Crystallize:
  {"type":"procedural",
   "content":"PROCEDURE: <pat> (NxN genutzt). Example:\\n```...```",
   "tags":["crystallized", lang, op]}

Idempotenz: vor Schreiben wird geprüft ob procedural memory mit
gleichem (crystallized + lang + op)-Tag-Set bereits existiert.
"""
import re
import sys
from collections import Counter
from pathlib import Path

# Standalone-Ausführung erlauben
sys.path.insert(0, str(Path(__file__).parent))

from events import EventLog
from memory import MemoryStore


from core import EVENT_PATH, MEMORY_PATH  # noqa: E402

THRESHOLD = 3

FIRST_TOKEN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z_0-9-]*)")


def extract_pattern(lang: str, code_snippet: str) -> str | None:
    """'lang:firsttoken'. Returns None falls kein Identifier am Anfang."""
    m = FIRST_TOKEN_RE.match(code_snippet or "")
    if not m:
        return None
    first = m.group(1).lower()
    norm_lang = "shell" if lang in ("shell", "bash", "sh") else lang
    return f"{norm_lang}:{first}"


def existing_crystallized_patterns(store: MemoryStore) -> set[str]:
    """Liefert Set von 'lang:op' Patterns die bereits kristallisiert sind."""
    result: set[str] = set()
    for m in store.load():
        tags = [t for t in m.get("tags", []) if isinstance(t, str)]
        if "crystallized" not in tags:
            continue
        rest = [t for t in tags if t != "crystallized"]
        if len(rest) >= 2:
            result.add(f"{rest[0]}:{rest[1]}")
    return result


def crystallize(events_path: str = EVENT_PATH,
                memory_path: str = MEMORY_PATH,
                threshold: int = THRESHOLD) -> list[dict]:
    """Findet Patterns mit count >= threshold die noch nicht kristallisiert
    sind. Schreibt eine procedural Memory pro neuem Pattern. Returns Liste
    der geschriebenen Entries (mit zusätzlichen Feldern pattern + count)."""
    events = EventLog(events_path)
    store = MemoryStore(memory_path)

    counter: Counter[str] = Counter()
    examples: dict[str, str] = {}
    for ev in events.by_type("code_executed"):
        if not ev.get("ok"):
            continue
        snippet = ev.get("code_snippet", "")
        pat = extract_pattern(ev.get("lang", ""), snippet)
        if not pat:
            continue
        counter[pat] += 1
        # Behalte das längste/aussagekräftigste Beispiel
        if pat not in examples or len(snippet) > len(examples[pat]):
            examples[pat] = snippet[:200]

    skip = existing_crystallized_patterns(store)
    written: list[dict] = []
    for pat, count in counter.items():
        if count < threshold or pat in skip:
            continue
        lang, op = pat.split(":", 1)
        example = examples[pat]
        content = (
            f"PROCEDURE: {pat} (bereits {count}x genutzt). Example:\n"
            f"```{lang}\n{example}\n```"
        )
        entry = store.append("procedural", content,
                             tags=["crystallized", lang, op])
        entry = dict(entry)
        entry["pattern"] = pat
        entry["count"] = count
        written.append(entry)
    return written


def main() -> int:
    written = crystallize()
    if not written:
        print("No new patterns to crystallize.")
        return 0
    for w in written:
        print(f"✓ crystallized: {w['pattern']} (count={w['count']})")
        print(f"  → {w['content'][:120]}…")
    print(f"\nTotal: {len(written)} new procedural memories.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
