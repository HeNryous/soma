"""Smoke test for MemoryStore."""
import tempfile
from datetime import datetime
from pathlib import Path

from memory import MemoryStore, format_for_prompt


def test_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "m.jsonl")
        assert store.load() == []
        store.append("semantic", "User's name is Alex", tags=["name", "user"])
        store.append("procedural", "Create file: echo X > /workspace/Y",
                     tags=["file", "create"])
        store.append("episodic", "First conversation this evening")
        ms = store.load()
        assert len(ms) == 3
        assert ms[0]["id"] == "mem_0001"
        assert ms[0]["type"] == "semantic"
        assert ms[0]["content"] == "User's name is Alex"
        assert "name" in ms[0]["tags"]
        print("✓ roundtrip")


def test_by_type():
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "m.jsonl")
        store.append("semantic", "fact a")
        store.append("procedural", "proc b")
        store.append("semantic", "fact c")
        assert len(store.by_type("semantic")) == 2
        assert len(store.by_type("procedural")) == 1
        assert len(store.by_type("episodic")) == 0
        print("✓ by_type")


def test_search():
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "m.jsonl")
        store.append("semantic", "Alex lives in Germany", tags=["location"])
        store.append("semantic", "Favorite color is blue", tags=["preference"])
        hits = store.search("Alex")
        assert len(hits) == 1
        hits = store.search("preference")
        assert len(hits) == 1
        hits = store.search("missing")
        assert hits == []
        print("✓ search")


def test_conflict_check():
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "m.jsonl")
        store.append("semantic", "User's name is Alex")
        hits = store.conflict_check("User's name is Alex Smith", threshold=0.6)
        assert len(hits) == 1
        hits = store.conflict_check("Weather is fine", threshold=0.6)
        assert hits == []
        print("✓ conflict_check")


def test_format_for_prompt():
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "m.jsonl")
        store.append("semantic", "User's name is Alex")
        store.append("procedural", "echo X > /workspace/Y")
        block = format_for_prompt(store.load())
        assert "Facts:" in block
        assert "Learned procedures:" in block
        assert "Alex" in block
        # Semantic must come BEFORE procedural (render order)
        assert block.index("Facts:") < block.index("Learned procedures:")
        print("✓ format_for_prompt")


def test_format_behaviors_first():
    """Style memories (preference/style tag) get the priority block."""
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "m.jsonl")
        store.append("semantic", "User's name is Alex", tags=["name"])
        store.append("semantic", "User prefers concise replies",
                     tags=["preference"])
        store.append("procedural", "Create file: echo X > /workspace/Y",
                     tags=["file"])
        block = format_for_prompt(store.load())
        assert "Behavior Rules" in block
        assert "concise replies" in block
        # Behavior Rules must come FIRST
        assert block.index("Behavior Rules") < block.index("Facts:")
        assert block.index("Facts:") < block.index("Learned procedures:")
        # User's name is NOT a behavior rule, belongs in facts
        behavior_section = block[
            block.index("Behavior Rules"):block.index("Facts:")
        ]
        assert "Alex" not in behavior_section
        assert "concise" in behavior_section
        print("✓ format_behaviors_first")


def test_robust_to_garbage():
    """The model sometimes writes garbage. The loader must survive."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "m.jsonl"
        p.write_text(
            '{"type":"semantic","content":"ok"}\n'
            'this is not json\n'
            '\n'
            '{"type":"bogus","content":"unknown-type"}\n'
            '[not a dict]\n'
            '{"content":"missing type"}\n'
        )
        store = MemoryStore(p)
        ms = store.load()
        assert len(ms) == 3, f"expected 3, got {len(ms)}: {ms}"
        # bogus type → fallback to episodic
        assert ms[1]["type"] == "episodic"
        # missing type → also episodic
        assert ms[2]["type"] == "episodic"
        print("✓ robust_to_garbage")


def test_mark_used():
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "m.jsonl")
        store.append("semantic", "fact A")
        store.append("semantic", "fact B")
        assert store.mark_used("mem_0001") is True
        m1 = next(m for m in store.load() if m["id"] == "mem_0001")
        assert m1["use_count"] == 1
        assert m1["last_used"]
        store.mark_used("mem_0001")
        m1 = next(m for m in store.load() if m["id"] == "mem_0001")
        assert m1["use_count"] == 2
        # Others unchanged
        m2 = next(m for m in store.load() if m["id"] == "mem_0002")
        assert m2.get("use_count", 0) == 0
        assert store.mark_used("mem_9999") is False
        print("✓ mark_used")


def test_prune_preserves_style():
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "m.jsonl")
        store.append("semantic", "User prefers concise replies",
                     tags=["preference"])
        store.append("semantic", "User likes dark mode", tags=["style"])
        for i in range(8):
            store.append("episodic", f"random episode {i}")
        # Keep=3: 2 style memories are immortal → 3 = 2 + 1 mortal
        removed = store.prune(keep=3)
        ms = store.load()
        contents = [m["content"] for m in ms]
        assert "User prefers concise replies" in contents
        assert "User likes dark mode" in contents
        assert len(ms) == 3
        assert removed == 7
        print("✓ prune_preserves_style")


def test_prune_immortals_exceed_keep():
    """When more style memories than `keep` exist, they all stay."""
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "m.jsonl")
        for i in range(5):
            store.append("semantic", f"preference {i}", tags=["preference"])
        # 3 mortal
        for i in range(3):
            store.append("episodic", f"episode {i}")
        # keep=2 must NOT kill the 5 immortals
        removed = store.prune(keep=2)
        ms = store.load()
        # All 5 immortals + 0 mortals (because 5 > keep=2)
        assert len(ms) == 5
        assert all("preference" in (m.get("tags") or []) for m in ms)
        assert removed == 3
        print("✓ prune_immortals_exceed_keep")


def test_fuse_merges_duplicates():
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "m.jsonl")
        store.append("semantic", "User likes coffee with milk", tags=["food"])
        store.append("semantic", "User likes coffee with milk",
                     tags=["beverage"])
        store.append("semantic", "Weather is fine today")
        merged = store.fuse(threshold=0.8)
        ms = store.load()
        assert merged == 1
        assert len(ms) == 2
        contents = [m["content"] for m in ms]
        assert "Weather is fine today" in contents
        # Tags were unified
        coffee = next(m for m in ms if "coffee" in m["content"])
        assert "food" in coffee["tags"]
        assert "beverage" in coffee["tags"]
        # use_count went up (>=1 because of merge bonus)
        assert coffee["use_count"] >= 1
        print("✓ fuse_merges_duplicates")


def test_fuse_respects_type_and_style():
    """Fuse does NOT merge across types and NOT across preference vs non-preference.
    Threshold=0.9 excludes false positives from stop-word overlap."""
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "m.jsonl")
        # Same content, different type → type-check blocks
        store.append("semantic", "User likes coffee")
        store.append("episodic", "User likes coffee")
        # Same content, mixed preference → style-check blocks
        store.append("semantic", "User likes tea", tags=["preference"])
        store.append("semantic", "User likes tea", tags=["random"])
        merged = store.fuse(threshold=0.9)
        ms = store.load()
        assert merged == 0, f"unexpected merges: {ms}"
        assert len(ms) == 4
        print("✓ fuse_respects_type_and_style")


def test_score_ordering():
    """High use_count + recently used → higher score."""
    from memory import score
    fresh_used = {"type": "semantic", "use_count": 10,
                  "last_used": datetime.now().isoformat(timespec="seconds"),
                  "tags": []}
    old_unused = {"type": "semantic", "use_count": 0,
                  "created_at": "2025-01-01T00:00:00", "tags": []}
    assert score(fresh_used) > score(old_unused)
    # Style memory is inf
    style = {"type": "semantic", "use_count": 0, "tags": ["preference"]}
    assert score(style) == float("inf")
    print("✓ score_ordering")


def test_find_tag_conflicts():
    with tempfile.TemporaryDirectory() as td:
        s = MemoryStore(Path(td) / "m.jsonl")
        s.append("semantic", "Acme in Berlin", tags=["customer", "acme"])
        s.append("semantic", "Another topic", tags=["domain"])
        # New mem with same type + 2 shared tags
        new1 = {"type": "semantic", "content": "Acme in Hamburg",
                "tags": ["customer", "acme"]}
        hits = s.find_tag_conflicts(new1)
        assert len(hits) == 1
        assert "Berlin" in hits[0]["content"]
        # New mem with same type + only 1 shared tag → no hit
        new2 = {"type": "semantic", "content": "Acme somewhere",
                "tags": ["customer", "geo"]}
        assert s.find_tag_conflicts(new2) == []
        # Exact-content match → NO conflict (fuse's job)
        new3 = {"type": "semantic", "content": "Acme in Berlin",
                "tags": ["customer", "acme"]}
        assert s.find_tag_conflicts(new3) == []
        # Different type → no hit
        new4 = {"type": "procedural", "content": "Acme process",
                "tags": ["customer", "acme"]}
        assert s.find_tag_conflicts(new4) == []
        print("✓ find_tag_conflicts")


def test_immortal_tags_extend_style():
    """domain/role/identity are also immortal now."""
    from memory import IMMORTAL_TAGS, STYLE_TAGS, score
    assert STYLE_TAGS <= IMMORTAL_TAGS
    assert "domain" in IMMORTAL_TAGS
    assert "role" in IMMORTAL_TAGS
    assert "identity" in IMMORTAL_TAGS
    m_domain = {"type": "semantic", "tags": ["domain", "manufacturer"],
                "use_count": 0, "created_at": "2025-01-01T00:00:00"}
    assert score(m_domain) == float("inf")
    m_role = {"type": "semantic", "tags": ["role"], "use_count": 0,
              "created_at": "2025-01-01T00:00:00"}
    assert score(m_role) == float("inf")
    m_normal = {"type": "semantic", "tags": ["random"], "use_count": 0,
                "created_at": "2025-01-01T00:00:00"}
    assert score(m_normal) < float("inf")
    print("✓ immortal_tags_extend_style")


def test_read_context_selection():
    from memory import read_context_selection
    with tempfile.TemporaryDirectory() as td:
        assert read_context_selection(td) == []
        (Path(td) / "context_selection.json").write_text("[]")
        assert read_context_selection(td) == []
        (Path(td) / "context_selection.json").write_text(
            '["mem_0001","mem_0002"]')
        assert read_context_selection(td) == ["mem_0001", "mem_0002"]
        (Path(td) / "context_selection.json").write_text("not json")
        assert read_context_selection(td) == []
        (Path(td) / "context_selection.json").write_text('{"foo": "bar"}')
        assert read_context_selection(td) == []
    print("✓ read_context_selection")


def test_format_for_prompt_selected():
    from memory import format_for_prompt_selected
    mems = [
        {"id": "mem_0001", "type": "semantic", "content": "User likes concise",
         "tags": ["preference"]},
        {"id": "mem_0002", "type": "semantic",
         "content": "Acme is the main customer", "tags": ["customer", "acme"]},
        {"id": "mem_0003", "type": "semantic",
         "content": "Beta has 50 servers", "tags": ["customer", "beta"]},
        {"id": "mem_0004", "type": "semantic",
         "content": "DDR4 EOL 2025", "tags": ["fact", "memory"]},
    ]
    block = format_for_prompt_selected(mems, ["mem_0002"],
                                        user_query="What do you know about Beta?")
    assert "Behavior Rules" in block
    assert "Relevant context" in block
    assert "Acme" in block
    assert "Further memories" in block
    assert "Beta" in block
    assert "DDR4" not in block
    i_behav = block.index("Behavior Rules")
    i_sel = block.index("Relevant context")
    i_more = block.index("Further memories")
    assert i_behav < i_sel < i_more
    print("✓ format_for_prompt_selected")


def test_format_selected_fallback_when_empty_ids():
    from memory import format_for_prompt_selected
    mems = [{"id": "m_1", "type": "semantic", "content": "fact",
             "tags": []}]
    block = format_for_prompt_selected(mems, [], user_query="x")
    assert "Facts:" in block
    assert "Relevant context" not in block
    print("✓ format_selected_fallback_when_empty_ids")


if __name__ == "__main__":
    test_roundtrip()
    test_by_type()
    test_search()
    test_conflict_check()
    test_format_for_prompt()
    test_format_behaviors_first()
    test_robust_to_garbage()
    test_mark_used()
    test_prune_preserves_style()
    test_prune_immortals_exceed_keep()
    test_fuse_merges_duplicates()
    test_fuse_respects_type_and_style()
    test_score_ordering()
    test_find_tag_conflicts()
    test_immortal_tags_extend_style()
    test_read_context_selection()
    test_format_for_prompt_selected()
    test_format_selected_fallback_when_empty_ids()
    print("\nAll memory tests passed.")
