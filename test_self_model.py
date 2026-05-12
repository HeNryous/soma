"""Smoke test for self_model."""
import tempfile
from datetime import datetime
from pathlib import Path

from events import EventLog
from memory import MemoryStore
from self_model import derive, summarize, describe


def test_derive_empty():
    with tempfile.TemporaryDirectory() as td:
        s = MemoryStore(Path(td) / "m.jsonl")
        e = EventLog(Path(td) / "e.jsonl")
        d = derive(s, e)
        assert d["total_memories"] == 0
        assert d["skills"] == []
        assert d["behaviors"] == []
        assert d["prompts_today"] == 0
        print("✓ derive_empty")


def test_derive_counts():
    with tempfile.TemporaryDirectory() as td:
        s = MemoryStore(Path(td) / "m.jsonl")
        e = EventLog(Path(td) / "e.jsonl")
        s.append("semantic", "Alex wohnt in DE")
        s.append("semantic", "User mag knappe Antworten", tags=["preference"])
        s.append("procedural", "PROCEDURE: shell:echo …",
                 tags=["crystallized", "shell", "echo"])
        s.append("procedural", "PROCEDURE: python:print …",
                 tags=["crystallized", "python", "print"])
        s.append("episodic", "heutiges Ereignis")
        d = derive(s, e)
        assert d["total_memories"] == 5
        assert d["memory_counts"]["semantic"] == 2
        assert d["memory_counts"]["procedural"] == 2
        assert d["memory_counts"]["episodic"] == 1
        assert "shell:echo" in d["skills"]
        assert "python:print" in d["skills"]
        assert any("knappe" in b for b in d["behaviors"])
        print("✓ derive_counts")


def test_derive_today_events():
    with tempfile.TemporaryDirectory() as td:
        s = MemoryStore(Path(td) / "m.jsonl")
        e = EventLog(Path(td) / "e.jsonl")
        # heutige events
        for i in range(3):
            e.log("prompt_received", user_message=f"hi {i}")
        for i in range(5):
            e.log("code_executed", iteration=1, lang="shell", ok=True)
        # 1 alter event (manuelle insert)
        p = Path(td) / "e.jsonl"
        with p.open("a") as f:
            f.write('{"ts": "2025-01-01T00:00:00", "type": "prompt_received"}\n')
        d = derive(s, e)
        assert d["prompts_today"] == 3  # alter event darf nicht zählen
        assert d["code_today"] == 5
        assert d["last_activity"]  # nicht leer
        print("✓ derive_today_events")


def test_summarize_empty():
    with tempfile.TemporaryDirectory() as td:
        s = MemoryStore(Path(td) / "m.jsonl")
        e = EventLog(Path(td) / "e.jsonl")
        out = summarize(s, e)
        assert "noch keine Memories" in out
        print("✓ summarize_empty")


def test_summarize_with_skills():
    with tempfile.TemporaryDirectory() as td:
        s = MemoryStore(Path(td) / "m.jsonl")
        e = EventLog(Path(td) / "e.jsonl")
        s.append("procedural", "PROCEDURE: shell:echo …",
                 tags=["crystallized", "shell", "echo"])
        s.append("procedural", "PROCEDURE: shell:cat …",
                 tags=["crystallized", "shell", "cat"])
        s.append("semantic", "Alex wohnt in DE")
        out = summarize(s, e)
        assert "3 Memories" in out
        assert "shell:echo" in out
        assert "shell:cat" in out
        print("✓ summarize_with_skills")


def test_describe_format():
    with tempfile.TemporaryDirectory() as td:
        s = MemoryStore(Path(td) / "m.jsonl")
        e = EventLog(Path(td) / "e.jsonl")
        s.append("semantic", "fact")
        s.append("procedural", "PROCEDURE: shell:ls …",
                 tags=["crystallized", "shell", "ls"])
        out = describe(s, e)
        assert "HARNESS STATUS" in out
        assert "Memories: 2 total" in out
        assert "Skills (1)" in out
        assert "shell:ls" in out
        print("✓ describe_format")


if __name__ == "__main__":
    test_derive_empty()
    test_derive_counts()
    test_derive_today_events()
    test_summarize_empty()
    test_summarize_with_skills()
    test_describe_format()
    print("\nAll self_model tests passed.")
