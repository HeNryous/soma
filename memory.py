"""
MemoryStore — JSONL-persistierte Memories mit drei Typen.

Read-Side (Code): load(), by_type(), search(), relevant().
Write-Side (Modell): append() ist nur für Tests da. The model writes
selbst via Code-Block (`echo '{...}' >> memories.jsonl`).

Schema pro Zeile:
  {"id": "mem_NNNN", "type": "semantic|episodic|procedural",
   "content": "...", "tags": ["..."], "created_at": "...",
   "last_used": "...", "use_count": N}

ID und created_at werden beim load() ergänzt falls nicht vorhanden —
so kann the model minimal `{"type":"semantic","content":"..."}` schreiben.

P4 Vergessen: prune() entfernt low-scoring Memories, fuse() merged
near-duplicates. style memories (Tag preference/style) are immortal.
"""
import json
import math
from datetime import datetime
from pathlib import Path


VALID_TYPES = {"semantic", "episodic", "procedural"}


class MemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[dict]:
        """All memories as a normalized list. Defective lines are
        skipped — the model kann Müll schreiben without killing us."""
        if not self.path.exists():
            return []
        items: list[dict] = []
        for i, line in enumerate(self.path.read_text().splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            d.setdefault("id", f"mem_{i:04d}")
            t = d.get("type", "episodic")
            d["type"] = t if t in VALID_TYPES else "episodic"
            d.setdefault("content", "")
            tags = d.get("tags", [])
            d["tags"] = tags if isinstance(tags, list) else []
            d.setdefault("created_at", "")
            items.append(d)
        return items

    def append(self, type: str, content: str,
               tags: list[str] | None = None) -> dict:
        """Append helper for tests. Modell schreibt via shell echo."""
        entry = {
            "type": type if type in VALID_TYPES else "episodic",
            "content": content,
            "tags": list(tags or []),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def by_type(self, type: str) -> list[dict]:
        return [m for m in self.load() if m["type"] == type]

    def search(self, query: str) -> list[dict]:
        """Naive substring search on content + tags. Sufficient for <100 entries."""
        q = query.lower()
        return [m for m in self.load()
                if q in m["content"].lower()
                or any(q in t.lower() for t in m["tags"])]

    def find_tag_conflicts(self, new_mem: dict,
                           min_tag_overlap: int = 2) -> list[dict]:
        """Finds existing memories mit same type + >= min_tag_overlap
        shared tags. Excludes exact content matches (deduplicated by fuse).
        Used for #12 Pre-Write-Conflict-Detection."""
        new_tags = {t.lower() for t in (new_mem.get("tags") or [])
                    if isinstance(t, str)}
        if len(new_tags) < min_tag_overlap:
            return []
        new_type = new_mem.get("type")
        new_content = (new_mem.get("content") or "").strip()
        hits: list[dict] = []
        for m in self.load():
            if m.get("type") != new_type:
                continue
            if (m.get("content") or "").strip() == new_content:
                continue
            m_tags = {t.lower() for t in (m.get("tags") or [])
                      if isinstance(t, str)}
            if len(new_tags & m_tags) >= min_tag_overlap:
                hits.append(m)
        return hits

    def conflict_check(self, content: str, threshold: float = 0.8) -> list[dict]:
        """Naive Near-Duplicate-Check: token-overlap ratio.
        Returns existing memories that look similar to `content`."""
        c_tokens = set(content.lower().split())
        if not c_tokens:
            return []
        hits = []
        for m in self.load():
            m_tokens = set(m["content"].lower().split())
            if not m_tokens:
                continue
            overlap = len(c_tokens & m_tokens) / max(len(c_tokens), len(m_tokens))
            if overlap >= threshold:
                hits.append(m)
        return hits

    def _rewrite(self, memories: list[dict]) -> None:
        """Atomic rewrite via tempfile + rename."""
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for m in memories:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        tmp.replace(self.path)

    def mark_used(self, id: str) -> bool:
        """Increments use_count, sets last_used to now.
        Atomic rewrite. Returns True wenn ID gefunden."""
        mems = self.load()
        now = datetime.now().isoformat(timespec="seconds")
        found = False
        for m in mems:
            if m.get("id") == id:
                m["use_count"] = int(m.get("use_count", 0)) + 1
                m["last_used"] = now
                found = True
        if found:
            self._rewrite(mems)
        return found

    def prune(self, keep: int = 100) -> int:
        """Keeps the top-N memories nach score(). Returns count removed.
        style memories (score=inf) always remain, even if keep is exceeded."""
        mems = self.load()
        if len(mems) <= keep:
            return 0
        now = datetime.now()
        scored = sorted(mems, key=lambda m: score(m, now), reverse=True)
        # First all inf-score (style memories), then fill up to keep
        immortal = [m for m in scored if score(m, now) == float("inf")]
        mortal = [m for m in scored if score(m, now) != float("inf")]
        kept = immortal + mortal[:max(0, keep - len(immortal))]
        # Stabilize file order by ID
        kept.sort(key=lambda m: m.get("id", ""))
        removed = len(mems) - len(kept)
        self._rewrite(kept)
        return removed

    def fuse(self, threshold: float = 0.85) -> int:
        """Merges near-duplicates: same type + style status, content-overlap
        ≥ threshold. On merge: sum use_count, unify tags, max
        created_at. Returns count removeder (gemergeter) Entries."""
        mems = self.load()
        if len(mems) < 2:
            return 0
        token_sets = [set(m.get("content", "").lower().split()) for m in mems]
        merged_into: set[int] = set()
        for i in range(len(mems)):
            if i in merged_into:
                continue
            for j in range(i + 1, len(mems)):
                if j in merged_into:
                    continue
                t_i, t_j = token_sets[i], token_sets[j]
                if not t_i or not t_j:
                    continue
                if mems[i].get("type") != mems[j].get("type"):
                    continue
                tags_i = {t.lower() for t in mems[i].get("tags", [])}
                tags_j = {t.lower() for t in mems[j].get("tags", [])}
                # style memories nur mit anderen style memories mergen
                if bool(tags_i & STYLE_TAGS) != bool(tags_j & STYLE_TAGS):
                    continue
                overlap = len(t_i & t_j) / max(len(t_i), len(t_j))
                if overlap < threshold:
                    continue
                # Merge j → i
                p, s = mems[i], mems[j]
                p["use_count"] = (int(p.get("use_count", 0))
                                  + int(s.get("use_count", 0)) + 1)
                p["tags"] = sorted(set(p.get("tags", []) + s.get("tags", [])))
                if s.get("created_at", "") > p.get("created_at", ""):
                    p["created_at"] = s["created_at"]
                merged_into.add(j)
        if not merged_into:
            return 0
        kept = [m for i, m in enumerate(mems) if i not in merged_into]
        self._rewrite(kept)
        return len(merged_into)


def score(m: dict, now: datetime | None = None) -> float:
    """Score function: recency × (1 + frequency) × type-weight.
    Immortal memories (Tags in IMMORTAL_TAGS — preference/style/tone/
    behavior + domain/role/identity) → inf (immune to prune)."""
    tags = {t.lower() for t in m.get("tags", [])}
    if tags & IMMORTAL_TAGS:
        return float("inf")
    now = now or datetime.now()
    last = m.get("last_used") or m.get("created_at") or ""
    try:
        ts = datetime.fromisoformat(last)
        days = max(0.0, (now - ts).total_seconds() / 86400.0)
    except (ValueError, TypeError):
        days = 30.0
    recency = 1.0 / (1.0 + days)
    frequency = math.log1p(int(m.get("use_count", 0)))
    type_weights = {"semantic": 1.0, "procedural": 1.5, "episodic": 0.5}
    type_weight = type_weights.get(m.get("type", "episodic"), 0.5)
    return recency * (1.0 + frequency) * type_weight


STYLE_TAGS = {"preference", "style", "behavior", "stil", "ton"}

# Unsterbliche Tags: score=inf, überleben jedes Prune.
# Superset von STYLE_TAGS. STYLE_TAGS bleibt das engere Set für die
# „Behavior Rules"-Render-Logik. Hier kommen Grundwissens-Tags dazu:
#   - domain: Fachbegriffe, Branchen-Regeln, Domain-Definitionen
#   - role: Identität + Arbeitskontext
#   - identity: persönliche Fakten über den User
# Diese werden selten abgerufen, sind aber immer relevant.
IMMORTAL_TAGS = STYLE_TAGS | {"domain", "role", "identity"}


def behavior_memories(memories: list[dict]) -> list[dict]:
    """Returns only memories with style / behavior tags."""
    return [m for m in memories
            if {t.lower() for t in m.get("tags", [])} & STYLE_TAGS]


def render_behaviors(memories: list[dict]) -> str:
    """Render style memories as compact reminder message.
    For injection directly before the user message (Recency-Bias)."""
    bms = behavior_memories(memories)
    if not bms:
        return ""
    lines = ["REMINDER before your response — strict:"]
    for m in bms:
        lines.append(f"• {m['content']}")
    return "\n".join(lines)


def format_for_prompt(memories: list[dict], max_chars: int = 4000) -> str:
    """Render memories as a prompt block. Memories with style tags
    (preference/style/behavior) get their own priority block
    'Behavior Rules'. Plain facts next. Learned procedures und
    Episoden zuletzt."""
    if not memories:
        return ""
    behaviors: list[dict] = []
    facts: list[dict] = []
    procedures: list[dict] = []
    episodes: list[dict] = []
    for m in memories:
        tags = {t.lower() for t in m.get("tags", [])}
        if tags & STYLE_TAGS:
            behaviors.append(m)
        elif m["type"] == "procedural":
            procedures.append(m)
        elif m["type"] == "episodic":
            episodes.append(m)
        else:
            facts.append(m)

    parts: list[str] = []
    if behaviors:
        parts.append(
            "Behavior Rules (befolge sie bei JEDER Antwort, "
            "highest priority — sie überschreiben deine Defaults):"
        )
        for m in behaviors:
            parts.append(f"- {m['content']}")
        parts.append("")
    if facts:
        parts.append("Facts:")
        for m in facts:
            parts.append(f"- {m['content']}")
        parts.append("")
    if procedures:
        parts.append("Learned procedures:")
        for m in procedures:
            parts.append(f"- {m['content']}")
        parts.append("")
    if episodes:
        parts.append("Recent events:")
        for m in episodes:
            parts.append(f"- {m['content']}")

    text = "\n".join(parts).rstrip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…[gekürzt]"
    return text


def read_context_selection(workspace_path) -> list[str]:
    """Reads <workspace>/context_selection.json — Array of memory IDs.
    Missing/empty/malformed → []. Read side for the foreground."""
    p = Path(workspace_path) / "context_selection.json"
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(d, list) and all(isinstance(x, str) for x in d):
        return d
    return []


def format_for_prompt_selected(memories: list[dict],
                                selected_ids: list[str],
                                user_query: str,
                                max_chars: int = 4000) -> str:
    """Selection-driven rendering:
      1) Behavior Rules (style tags, immer prominent)
      2) Relevant context (curator-selected IDs, excluding style tags)
      3) Further memories (tag match to user query, excluding selected and style)
    If selected_ids empty: falls back to format_for_prompt (Fallback)."""
    if not memories or not selected_ids:
        return format_for_prompt(memories, max_chars)
    behaviors: list[dict] = []
    pool: list[dict] = []
    for m in memories:
        tags = {t.lower() for t in m.get("tags") or []}
        if tags & STYLE_TAGS:
            behaviors.append(m)
        else:
            pool.append(m)
    by_id = {m["id"]: m for m in pool}
    selected = [by_id[i] for i in selected_ids if i in by_id]
    selected_id_set = {m["id"] for m in selected}
    q = (user_query or "").lower()
    further: list[dict] = []
    for m in pool:
        if m["id"] in selected_id_set:
            continue
        for t in m.get("tags") or []:
            if isinstance(t, str) and len(t) >= 3 and t.lower() in q:
                further.append(m)
                break
    parts: list[str] = []
    if behaviors:
        lines = [
            "Behavior Rules (befolge sie bei JEDER Antwort, höchste "
            "Priorität — sie überschreiben deine Defaults):"
        ]
        for m in behaviors:
            lines.append(f"- {m['content']}")
        parts.append("\n".join(lines))
    if selected:
        lines = ["Relevant context (für diesen Turn ausgewählt):"]
        for m in selected:
            lines.append(f"- {m['content']}")
        parts.append("\n".join(lines))
    if further:
        lines = ["Further memories (Tag-Match zur Frage):"]
        for m in further:
            lines.append(f"- {m['content']}")
        parts.append("\n".join(lines))
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…[gekürzt]"
    return text
