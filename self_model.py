"""
Self-model — lightweight self-awareness layer.

Aggregates state from MemoryStore + EventLog into two views:
- `summarize()` → 1-2 sentence block for the system prompt (model's view)
- `describe()` → human-readable CLI overview (user's view, via status.py)

No own file. Read-only derivation from existing stores.
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
    suffix = f" (+{len(skills) - max_n} more)" if len(skills) > max_n else ""
    return ", ".join(head) + suffix


def derive(store: MemoryStore, events: EventLog) -> dict:
    """Raw numbers from store + event log."""
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
    """1-2 sentence self-awareness block for the system prompt."""
    d = derive(store, events)
    parts: list[str] = []
    if d["total_memories"]:
        c = d["memory_counts"]
        parts.append(
            f"What you know: {d['total_memories']} memories "
            f"({c['semantic']} facts, {c['procedural']} procedures, "
            f"{c['episodic']} episodes)."
        )
    else:
        parts.append("You have no memories yet.")
    if d["skills"]:
        parts.append(
            f"You know these skills: {_format_skill_list(d['skills'])}."
        )
    if d["prompts_today"] >= 2:
        parts.append(f"There have been {d['prompts_today']} conversations today.")
    return " ".join(parts)


def describe(store: MemoryStore, events: EventLog) -> str:
    """Human-readable overview for the CLI."""
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
