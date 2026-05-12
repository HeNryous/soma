"""
Skill crystallization — extract recurring code patterns from events.jsonl
and persist them as procedural memories.

Hermes pattern (XSkill/SkillOS): after N similar tool-calls the operation
is abstracted into a reusable procedure. Here: after >= 3 successful code
blocks with the same pattern (lang + first token).

Pattern examples:
  shell:echo, shell:cat, shell:ls, python:import, python:print

Memory schema on crystallize:
  {"type":"procedural",
   "content":"PROCEDURE: <pat> (used NxN times). Example:\\n```...```",
   "tags":["crystallized", lang, op]}

Idempotency: before writing we check whether a procedural memory with
the same (crystallized + lang + op) tag-set already exists.
"""
import re
import sys
from collections import Counter
from pathlib import Path

# Allow standalone execution
sys.path.insert(0, str(Path(__file__).parent))

from events import EventLog
from memory import MemoryStore


from core import EVENT_PATH, MEMORY_PATH  # noqa: E402

THRESHOLD = 3

FIRST_TOKEN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z_0-9-]*)")


def extract_pattern(lang: str, code_snippet: str) -> str | None:
    """'lang:firsttoken'. Returns None when there is no leading identifier."""
    m = FIRST_TOKEN_RE.match(code_snippet or "")
    if not m:
        return None
    first = m.group(1).lower()
    norm_lang = "shell" if lang in ("shell", "bash", "sh") else lang
    return f"{norm_lang}:{first}"


def existing_crystallized_patterns(store: MemoryStore) -> set[str]:
    """Return the set of 'lang:op' patterns that have already been crystallized."""
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
    """Find patterns with count >= threshold that have not been crystallized
    yet. Write one procedural memory per new pattern. Returns the list of
    written entries (with extra fields pattern + count)."""
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
        # Keep the longest / most informative example
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
            f"PROCEDURE: {pat} (used {count}x). Example:\n"
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
