"""Smoke test for core.py auto-maintenance (ohne vLLM-Call)."""
import tempfile
from pathlib import Path

from events import EventLog
from memory import MemoryStore
from core import _post_run_maintenance, AUTO_CRYSTALLIZE_THRESHOLD


def test_auto_fuse_runs_every_time():
    """Auto-Fuse mergt duplicates auch bei kleiner Memory-Count."""
    with tempfile.TemporaryDirectory() as td:
        events_p = Path(td) / "events.jsonl"
        memory_p = Path(td) / "memory.jsonl"
        events = EventLog(events_p)
        ml = MemoryStore(memory_p)
        ml.append("semantic", "User mag Kaffee")
        ml.append("semantic", "User mag Kaffee")  # dup
        _post_run_maintenance(events, ml)
        assert len(ml.load()) == 1
        assert events.last("memory_fused") is not None
        print("✓ auto_fuse_runs_every_time")


def test_auto_fuse_no_event_when_nothing_to_merge():
    with tempfile.TemporaryDirectory() as td:
        events_p = Path(td) / "events.jsonl"
        memory_p = Path(td) / "memory.jsonl"
        events = EventLog(events_p)
        ml = MemoryStore(memory_p)
        ml.append("semantic", "fact a")
        ml.append("semantic", "completely different")
        _post_run_maintenance(events, ml)
        assert events.last("memory_fused") is None
        print("✓ auto_fuse_no_event_when_nothing_to_merge")


def test_auto_crystallize_triggers_at_threshold():
    with tempfile.TemporaryDirectory() as td:
        events_p = Path(td) / "events.jsonl"
        memory_p = Path(td) / "memory.jsonl"
        events = EventLog(events_p)
        ml = MemoryStore(memory_p)
        # Seed 25 echo events (über default-threshold 20)
        for i in range(25):
            events.log("code_executed", iteration=1, lang="shell", ok=True,
                       code_snippet=f"echo 'x{i}' > /tmp/{i}.txt")
        _post_run_maintenance(events, ml)
        last = events.last("auto_crystallized")
        assert last is not None, "auto_crystallized event missing"
        assert last["new_skills"] >= 1
        # Memory hat neuen procedural entry
        mems = ml.load()
        proc = [m for m in mems
                if m["type"] == "procedural"
                and "crystallized" in m.get("tags", [])]
        assert len(proc) >= 1
        print("✓ auto_crystallize_triggers_at_threshold")


def test_auto_crystallize_below_threshold():
    with tempfile.TemporaryDirectory() as td:
        events_p = Path(td) / "events.jsonl"
        memory_p = Path(td) / "memory.jsonl"
        events = EventLog(events_p)
        ml = MemoryStore(memory_p)
        for i in range(5):
            events.log("code_executed", iteration=1, lang="shell", ok=True,
                       code_snippet=f"echo {i}")
        _post_run_maintenance(events, ml)
        assert events.last("auto_crystallized") is None
        print("✓ auto_crystallize_below_threshold")


def test_auto_crystallize_does_not_loop():
    """Nach einem auto-crystallized werden nur NEUE code_executed gezählt."""
    with tempfile.TemporaryDirectory() as td:
        events_p = Path(td) / "events.jsonl"
        memory_p = Path(td) / "memory.jsonl"
        events = EventLog(events_p)
        ml = MemoryStore(memory_p)
        # Erste Welle: 25 echos → triggert crystallize
        for i in range(25):
            events.log("code_executed", iteration=1, lang="shell", ok=True,
                       code_snippet=f"echo {i}")
        _post_run_maintenance(events, ml)
        first = events.last("auto_crystallized")
        assert first is not None

        # Zweiter Aufruf direkt danach OHNE neue code_executed → kein Trigger
        _post_run_maintenance(events, ml)
        # Letzter auto_crystallized hat sich nicht verändert
        second = events.last("auto_crystallized")
        assert second["ts"] == first["ts"], f"unexpected re-trigger: {second}"
        print("✓ auto_crystallize_does_not_loop")


def test_threshold_constant():
    assert AUTO_CRYSTALLIZE_THRESHOLD == 20
    print("✓ threshold_constant")


def test_compress_middle_head_tail_intact():
    """50 Messages → compress_middle: head 5 + summary + tail 10 = 16 msgs."""
    import asyncio
    from unittest.mock import patch
    from core import (compress_middle, COMPRESS_HEAD_KEEP, COMPRESS_TAIL_KEEP)

    async def go():
        # Build 50 messages
        msgs = [{"role": "system", "content": "Du bist ein Begleiter."}]
        for i in range(24):
            msgs.append({"role": "user", "content": f"user msg {i}"})
            msgs.append({"role": "assistant", "content": f"assistant msg {i}"})
        msgs.append({"role": "user", "content": "FINAL user msg"})
        assert len(msgs) == 50

        async def fake_call(client, messages):
            return ("Resolved: alle vorigen Tasks abgeschlossen.\n"
                    "Pending: nichts mehr offen.\n"
                    "Decisions: kompakter Stil bestätigt.\n"
                    "Results: 24 Turns durchlaufen.")

        with patch("core.call_model", fake_call):
            result = await compress_middle(None, msgs)
        # head 5 + summary 1 + tail 10
        assert len(result) == COMPRESS_HEAD_KEEP + 1 + COMPRESS_TAIL_KEEP
        # Head intakt
        assert result[0]["content"] == "Du bist ein Begleiter."
        assert result[COMPRESS_HEAD_KEEP - 1] == msgs[COMPRESS_HEAD_KEEP - 1]
        # Summary in der Mitte
        summary = result[COMPRESS_HEAD_KEEP]
        assert summary["role"] == "system"
        assert "middle-compression" in summary["content"]
        assert "Resolved" in summary["content"]
        # Tail intakt
        assert result[-1]["content"] == "FINAL user msg"
        assert result[-1]["content"] == msgs[-1]["content"]
    asyncio.run(go())
    print("✓ compress_middle_head_tail_intact")


def test_compress_middle_short_passthrough():
    """Wenig Messages → keine Compression."""
    import asyncio
    from core import compress_middle
    async def go():
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"}]
        result = await compress_middle(None, msgs)
        assert result == msgs
    asyncio.run(go())
    print("✓ compress_middle_short_passthrough")


def test_post_run_conflict_detected():
    """Schreibe zwei Memories mit Tag-Overlap → memory_conflict_detected event."""
    import tempfile
    from pathlib import Path
    from events import EventLog
    from memory import MemoryStore
    from core import _post_run_maintenance
    with tempfile.TemporaryDirectory() as td:
        events_p = Path(td) / "events.jsonl"
        memory_p = Path(td) / "memory.jsonl"
        events = EventLog(events_p)
        ml = MemoryStore(memory_p)
        # initial: 1 memory
        m1 = ml.append("semantic", "Acme in Berlin",
                       tags=["customer", "acme"])
        initial_ids = {m["id"] for m in ml.load()}
        # neue: 1 Memory mit gleichen Tags aber widersprüchlichem content
        ml.append("semantic", "Acme in Hamburg",
                  tags=["customer", "acme"])
        # Maintenance ausführen
        _post_run_maintenance(events, ml, initial_mem_ids=initial_ids)
        # memory_conflict_detected event geloggt?
        conflicts = events.by_type("memory_conflict_detected")
        assert len(conflicts) == 1
        assert "Acme" in conflicts[0].get("new_content", "")
        assert "Hamburg" in conflicts[0].get("new_content", "")
        assert "Berlin" in conflicts[0].get("existing_content", "")
        assert "customer" in conflicts[0].get("shared_tags", [])
        print("✓ post_run_conflict_detected")


if __name__ == "__main__":
    test_threshold_constant()
    test_auto_fuse_runs_every_time()
    test_auto_fuse_no_event_when_nothing_to_merge()
    test_auto_crystallize_below_threshold()
    test_auto_crystallize_triggers_at_threshold()
    test_auto_crystallize_does_not_loop()
    test_compress_middle_short_passthrough()
    test_compress_middle_head_tail_intact()
    test_post_run_conflict_detected()
    print("\nAll core tests passed.")
