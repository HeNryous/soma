"""Smoke test for core.py auto-maintenance (no vLLM call)."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "src"))

import tempfile
from pathlib import Path

from events import EventLog
from memory import MemoryStore
from core import _post_run_maintenance, AUTO_CRYSTALLIZE_THRESHOLD


def test_auto_fuse_runs_every_time():
    """Auto-fuse also merges duplicates with a small memory count."""
    with tempfile.TemporaryDirectory() as td:
        events_p = Path(td) / "events.jsonl"
        memory_p = Path(td) / "memory.jsonl"
        events = EventLog(events_p)
        ml = MemoryStore(memory_p)
        ml.append("semantic", "User likes coffee")
        ml.append("semantic", "User likes coffee")  # dup
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
        # Seed 25 echo events (above the default threshold of 20)
        for i in range(25):
            events.log("code_executed", iteration=1, lang="shell", ok=True,
                       code_snippet=f"echo 'x{i}' > /tmp/{i}.txt")
        _post_run_maintenance(events, ml)
        last = events.last("auto_crystallized")
        assert last is not None, "auto_crystallized event missing"
        assert last["new_skills"] >= 1
        # Memory has a new procedural entry
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
    """After an auto_crystallized event only NEW code_executed events count."""
    with tempfile.TemporaryDirectory() as td:
        events_p = Path(td) / "events.jsonl"
        memory_p = Path(td) / "memory.jsonl"
        events = EventLog(events_p)
        ml = MemoryStore(memory_p)
        # First wave: 25 echoes → triggers crystallize
        for i in range(25):
            events.log("code_executed", iteration=1, lang="shell", ok=True,
                       code_snippet=f"echo {i}")
        _post_run_maintenance(events, ml)
        first = events.last("auto_crystallized")
        assert first is not None

        # Second call directly afterwards WITHOUT new code_executed → no trigger
        _post_run_maintenance(events, ml)
        # Last auto_crystallized has not changed
        second = events.last("auto_crystallized")
        assert second["ts"] == first["ts"], f"unexpected re-trigger: {second}"
        print("✓ auto_crystallize_does_not_loop")


def test_threshold_constant():
    assert AUTO_CRYSTALLIZE_THRESHOLD == 20
    print("✓ threshold_constant")


def test_compress_middle_head_tail_intact():
    """50 messages → compress_middle: head 5 + summary + tail 10 = 16 msgs."""
    import asyncio
    from unittest.mock import patch
    from core import (compress_middle, COMPRESS_HEAD_KEEP, COMPRESS_TAIL_KEEP)

    async def go():
        # Build 50 messages
        msgs = [{"role": "system", "content": "You are a companion."}]
        for i in range(24):
            msgs.append({"role": "user", "content": f"user msg {i}"})
            msgs.append({"role": "assistant", "content": f"assistant msg {i}"})
        msgs.append({"role": "user", "content": "FINAL user msg"})
        assert len(msgs) == 50

        async def fake_call(client, messages):
            return ("Resolved: all prior tasks completed.\n"
                    "Pending: nothing open.\n"
                    "Decisions: concise style confirmed.\n"
                    "Results: 24 turns processed.")

        with patch("core.call_model", fake_call):
            result = await compress_middle(None, msgs)
        # head 5 + summary 1 + tail 10
        assert len(result) == COMPRESS_HEAD_KEEP + 1 + COMPRESS_TAIL_KEEP
        # Head intact
        assert result[0]["content"] == "You are a companion."
        assert result[COMPRESS_HEAD_KEEP - 1] == msgs[COMPRESS_HEAD_KEEP - 1]
        # Summary in the middle
        summary = result[COMPRESS_HEAD_KEEP]
        assert summary["role"] == "system"
        assert "middle-compression" in summary["content"]
        assert "Resolved" in summary["content"]
        # Tail intact
        assert result[-1]["content"] == "FINAL user msg"
        assert result[-1]["content"] == msgs[-1]["content"]
    asyncio.run(go())
    print("✓ compress_middle_head_tail_intact")


def test_compress_middle_short_passthrough():
    """Few messages → no compression."""
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
    """Write two memories with tag overlap → memory_conflict_detected event."""
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
        ml.append("semantic", "Acme in Berlin",
                  tags=["customer", "acme"])
        initial_ids = {m["id"] for m in ml.load()}
        # New: 1 memory with the same tags but conflicting content
        ml.append("semantic", "Acme in Hamburg",
                  tags=["customer", "acme"])
        # Run maintenance
        _post_run_maintenance(events, ml, initial_mem_ids=initial_ids)
        # memory_conflict_detected event logged?
        conflicts = events.by_type("memory_conflict_detected")
        assert len(conflicts) == 1
        assert "Acme" in conflicts[0].get("new_content", "")
        assert "Hamburg" in conflicts[0].get("new_content", "")
        assert "Berlin" in conflicts[0].get("existing_content", "")
        assert "customer" in conflicts[0].get("shared_tags", [])
        print("✓ post_run_conflict_detected")


def test_classify_correction_yes():
    """Classifier returns 'correction' when call_model says YES."""
    import asyncio
    from unittest.mock import patch
    from core import classify_correction

    async def go():
        async def fake_call(client, messages, **kwargs):
            return "YES"
        with patch("core.call_model", fake_call):
            r = await classify_correction(None, "wrong, do it again",
                                          "Here is a poem.")
        assert r == "correction"
    asyncio.run(go())
    print("✓ classify_correction_yes")


def test_classify_correction_no():
    import asyncio
    from unittest.mock import patch
    from core import classify_correction

    async def go():
        async def fake_call(client, messages, **kwargs):
            return "NO"
        with patch("core.call_model", fake_call):
            r = await classify_correction(None, "thanks!",
                                          "Here is a poem.")
        assert r is None
    asyncio.run(go())
    print("✓ classify_correction_no")


def test_classify_correction_skips_long_message():
    """Long user messages aren't corrections — no LLM call."""
    import asyncio
    from unittest.mock import patch
    from core import classify_correction

    async def go():
        called = {"n": 0}
        async def fake_call(client, messages, **kwargs):
            called["n"] += 1
            return "YES"
        long_msg = "a" * 250
        with patch("core.call_model", fake_call):
            r = await classify_correction(None, long_msg, "previous reply")
        assert r is None
        assert called["n"] == 0
    asyncio.run(go())
    print("✓ classify_correction_skips_long_message")


def test_classify_correction_skips_no_prev():
    import asyncio
    from unittest.mock import patch
    from core import classify_correction

    async def go():
        called = {"n": 0}
        async def fake_call(client, messages, **kwargs):
            called["n"] += 1
            return "YES"
        with patch("core.call_model", fake_call):
            r = await classify_correction(None, "x", "")
        assert r is None
        assert called["n"] == 0
    asyncio.run(go())
    print("✓ classify_correction_skips_no_prev")


def test_classify_memory_promise_yes():
    import asyncio
    from unittest.mock import patch
    from core import classify_memory_promise

    async def go():
        async def fake_call(client, messages, **kwargs):
            return "YES"
        with patch("core.call_model", fake_call):
            r = await classify_memory_promise(None,
                                              "Noted that down.", 0)
        assert r == "memory_promise"
    asyncio.run(go())
    print("✓ classify_memory_promise_yes")


def test_classify_memory_promise_skips_when_blocks_executed():
    """If blocks_executed > 0, no promise check (bot wrote a memory)."""
    import asyncio
    from unittest.mock import patch
    from core import classify_memory_promise

    async def go():
        called = {"n": 0}
        async def fake_call(client, messages, **kwargs):
            called["n"] += 1
            return "YES"
        with patch("core.call_model", fake_call):
            r = await classify_memory_promise(None, "Noted.", 3)
        assert r is None
        assert called["n"] == 0
    asyncio.run(go())
    print("✓ classify_memory_promise_skips_when_blocks_executed")


def test_classify_handles_call_failure():
    """LLM call failure → graceful None, no exception."""
    import asyncio
    from unittest.mock import patch
    from core import classify_correction, classify_memory_promise

    async def go():
        async def failing(client, messages, **kwargs):
            raise RuntimeError("vLLM down")
        with patch("core.call_model", failing):
            r1 = await classify_correction(None, "x", "y")
            r2 = await classify_memory_promise(None, "Noted.", 0)
        assert r1 is None
        assert r2 is None
    asyncio.run(go())
    print("✓ classify_handles_call_failure")


def test_block_signature_stable():
    """Same blocks → same signature. Different blocks → different signature."""
    from core import block_signature
    a = [("shell", "echo hi")]
    b = [("shell", "echo hi")]
    c = [("shell", "echo bye")]
    assert block_signature(a) == block_signature(b)
    assert block_signature(a) != block_signature(c)
    assert block_signature([]) == ""
    print("✓ block_signature_stable")


def test_repeat_block_loop_detected():
    """3 identical iterations in a row → repeat_block_loop event + abort."""
    import asyncio
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    from core import run
    from events import EventLog

    with tempfile.TemporaryDirectory() as td:
        events_p = Path(td) / "events.jsonl"
        memory_p = Path(td) / "memory.jsonl"
        workspace = Path(td) / "workspace"
        workspace.mkdir(parents=True)
        memory_p.parent.mkdir(parents=True, exist_ok=True)

        import core as core_mod
        original_event_path = core_mod.EVENT_PATH
        original_memory_path = core_mod.MEMORY_PATH
        core_mod.EVENT_PATH = str(events_p)
        core_mod.MEMORY_PATH = str(memory_p)

        try:
            # call_model always returns the SAME shell block → repeat-loop.
            stuck_response = "```shell\necho stuck\n```"

            async def fake_call(client, messages, **kwargs):
                return stuck_response

            async def fake_execute(lang, code):
                return {"ok": True, "out": "stuck", "err": ""}

            async def fake_classify_correction(*args, **kwargs):
                return None

            async def fake_classify_memory_promise(*args, **kwargs):
                return None

            async def go():
                with patch("core.call_model", fake_call), \
                     patch("core.execute", fake_execute), \
                     patch("core.classify_correction",
                           fake_classify_correction), \
                     patch("core.classify_memory_promise",
                           fake_classify_memory_promise):
                    result = await run("please do something")
                return result

            result = asyncio.run(go())

            # Loop should have fired; ok=False (budget exhausted path)
            assert result["ok"] is False
            # No iter cap exists anymore; repeat-block detector is the
            # only forced exit. Should stop within a handful of iters.
            assert result["iterations"] <= 5, \
                f"expected early stop, ran {result['iterations']} iters"

            ev = EventLog(events_p)
            loop_events = ev.by_type("repeat_block_loop")
            assert len(loop_events) >= 1, "repeat_block_loop never logged"
            assert loop_events[0]["threshold"] == 3
            print("✓ repeat_block_loop_detected")
        finally:
            core_mod.EVENT_PATH = original_event_path
            core_mod.MEMORY_PATH = original_memory_path


def test_repeat_block_no_false_positive_on_two():
    """2 identical blocks should NOT trigger (legitimate retry-after-error)."""
    from core import block_signature, REPEAT_BLOCK_THRESHOLD
    assert REPEAT_BLOCK_THRESHOLD == 3, \
        "threshold change would alter false-positive behavior"
    # Manual simulation of the tracker logic
    sigs = []
    a = block_signature([("shell", "echo hi")])
    sigs.append(a); sigs = sigs[-REPEAT_BLOCK_THRESHOLD:]
    fires = (len(sigs) >= REPEAT_BLOCK_THRESHOLD
             and len(set(sigs)) == 1 and sigs[-1])
    assert not fires, "fires on 1"
    sigs.append(a); sigs = sigs[-REPEAT_BLOCK_THRESHOLD:]
    fires = (len(sigs) >= REPEAT_BLOCK_THRESHOLD
             and len(set(sigs)) == 1 and sigs[-1])
    assert not fires, "fires on 2 — false positive"
    sigs.append(a); sigs = sigs[-REPEAT_BLOCK_THRESHOLD:]
    fires = (len(sigs) >= REPEAT_BLOCK_THRESHOLD
             and len(set(sigs)) == 1 and sigs[-1])
    assert fires, "doesn't fire on 3"
    print("✓ repeat_block_no_false_positive_on_two")


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
    test_classify_correction_yes()
    test_classify_correction_no()
    test_classify_correction_skips_long_message()
    test_classify_correction_skips_no_prev()
    test_classify_memory_promise_yes()
    test_classify_memory_promise_skips_when_blocks_executed()
    test_classify_handles_call_failure()
    test_block_signature_stable()
    test_repeat_block_no_false_positive_on_two()
    test_repeat_block_loop_detected()
    print("\nAll core tests passed.")
