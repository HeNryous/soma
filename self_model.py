"""
Self-Model — leichte Self-Awareness-Schicht.

Aggregiert State aus MemoryStore + EventLog zu zwei Views:
- `summarize()` → 1-2-Satz-Block für System-Prompt (Modell-Sicht)
- `describe()` → menschenlesbare CLI-Übersicht (User-Sicht via status.py)

Keine eigene Datei. Nur Read-Only-Derivation aus existierenden Stores.
"""
from datetime import datetime

from memory import MemoryStore, STYLE_TAGS
from events import EventLog


def _today_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _format_skill_list(skills: list[str], max_n: int = 5) -> str:
    if not skills:
        return ""
    head = skills[:max_n]
    suffix = f" (+{len(skills) - max_n} weitere)" if len(skills) > max_n else ""
    return ", ".join(head) + suffix


def derive(store: MemoryStore, events: EventLog) -> dict:
    """Rohe Zahlen aus Store + Event-Log."""
    mems = store.load()
    counts = {"semantic": 0, "procedural": 0, "episodic": 0}
    skills: list[str] = []
    behaviors: list[str] = []
    for m in mems:
        mtype = m.get("type", "episodic")
        counts[mtype] = counts.get(mtype, 0) + 1
        tags = [tg for tg in (m.get("tags") or []) if isinstance(tg, str)]
        lowered = {tg.lower() for tg in tags}
        if "crystallized" in tags:
            rest = [tg for tg in tags if tg != "crystallized"]
            if len(rest) >= 2:
                skills.append(f"{rest[0]}:{rest[1]}")
        if lowered & STYLE_TAGS:
            behaviors.append((m.get("content") or "")[:80])

    all_events = events.load()
    today_prefix = _today_iso()
    today_events = [e for e in all_events
                    if e.get("ts", "").startswith(today_prefix)]
    prompts_today = sum(1 for e in today_events
                        if e.get("type") == "prompt_received")
    code_today = sum(1 for e in today_events
                     if e.get("type") == "code_executed")
    last_ts = all_events[-1]["ts"] if all_events else ""

    return {
        "memory_counts": counts,
        "total_memories": sum(counts.values()),
        "behaviors": behaviors,
        "skills": skills,
        "prompts_today": prompts_today,
        "code_today": code_today,
        "last_activity": last_ts,
    }


def summarize(store: MemoryStore, events: EventLog) -> str:
    """1-2-Satz-Self-Awareness-Block für den System-Prompt."""
    d = derive(store, events)
    parts: list[str] = []
    if d["total_memories"]:
        c = d["memory_counts"]
        parts.append(
            f"Was du weißt: {d['total_memories']} Memories "
            f"({c['semantic']} Fakten, {c['procedural']} Prozeduren, "
            f"{c['episodic']} Episoden)."
        )
    else:
        parts.append("Du hast noch keine Memories.")
    if d["skills"]:
        parts.append(
            f"Du kennst die Skills: {_format_skill_list(d['skills'])}."
        )
    if d["prompts_today"] >= 2:
        parts.append(f"Heute gab es schon {d['prompts_today']} Gespräche.")
    return " ".join(parts)


def describe(store: MemoryStore, events: EventLog) -> str:
    """Menschenlesbare Übersicht für CLI."""
    d = derive(store, events)
    c = d["memory_counts"]
    lines = ["=== HARNESS STATUS ==="]
    lines.append(f"Memories: {d['total_memories']} total")
    lines.append(f"  semantic:   {c['semantic']}")
    lines.append(f"  procedural: {c['procedural']}")
    lines.append(f"  episodic:   {c['episodic']}")
    if d["behaviors"]:
        lines.append(f"Behaviors ({len(d['behaviors'])}):")
        for b in d["behaviors"]:
            lines.append(f"  • {b}")
    if d["skills"]:
        lines.append(f"Skills ({len(d['skills'])}):")
        for s in d["skills"]:
            lines.append(f"  • {s}")
    lines.append("")
    lines.append(f"Today: {d['prompts_today']} prompts, "
                 f"{d['code_today']} code-execs")
    lines.append(f"Last activity: {d['last_activity'] or '—'}")
    return "\n".join(lines)
