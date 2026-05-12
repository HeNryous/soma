"""Smoke test for background.py — threshold + queue without a model call."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "src"))

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from background import (Background, should_curate,
                        CURATION_MIN_USER_CHARS,
                        CURATION_SKIP_IF_FRESH_DELTA_AND_SHORT,
                        find_recent_failed_exec)
from events import EventLog
from memory import MemoryStore


def test_should_curate_short_skip():
    assert should_curate("ok", 0) is False
    assert should_curate("short text", 0) is False
    assert should_curate("a" * (CURATION_MIN_USER_CHARS - 1), 0) is False
    print("✓ should_curate_short_skip")


def test_should_curate_substantive():
    long_msg = "a" * (CURATION_MIN_USER_CHARS + 10)
    assert should_curate(long_msg, 0) is True
    print("✓ should_curate_substantive")


def test_should_curate_fresh_delta_short_skips():
    """If the foreground already wrote something + message is short: skip."""
    msg = "a" * (CURATION_MIN_USER_CHARS + 10)  # > threshold but < 400
    assert should_curate(msg, 1) is False
    print("✓ should_curate_fresh_delta_short_skips")


def test_should_curate_long_message_even_with_delta():
    """Long messages → curate even with delta>0 (foreground was
    presumably only partial)."""
    long_msg = "a" * (CURATION_SKIP_IF_FRESH_DELTA_AND_SHORT + 10)
    assert should_curate(long_msg, 2) is True
    print("✓ should_curate_long_message_even_with_delta")


def test_background_queue_processes_turn():
    """Queue → background extracts code blocks and executes them."""
    async def go():
        with tempfile.TemporaryDirectory() as td:
            mem_path = Path(td) / "m.jsonl"
            ev_path = Path(td) / "e.jsonl"
            queue: asyncio.Queue = asyncio.Queue()
            bg = Background(queue,
                            memory_path=str(mem_path),
                            event_path=str(ev_path))
            # Mock call_model: returns shell block; execute writes memory
            fake_response = (
                "```shell\n"
                "echo '{\"type\":\"semantic\",\"content\":\"Test\","
                "\"tags\":[\"fact\"]}' >> /workspace/memories.jsonl\n"
                "```"
            )
            async def fake_call(client, messages):
                return fake_response

            async def fake_exec(lang, code):
                # Simulate: write directly to mem file
                mem_path.write_text(
                    '{"type":"semantic","content":"Test","tags":["fact"]}\n')
                return {"ok": True, "out": "", "err": ""}

            with patch("background.call_model", fake_call), \
                 patch("background.execute", fake_exec):
                bg_task = asyncio.create_task(bg.run())
                # Push a substantive turn
                await queue.put({
                    "user_message": "x" * 200,
                    "final_text": "bot answer",
                    "blocks_executed": 0,
                    "memory_delta": 0,
                })
                # Wait until queue is empty
                await queue.join()
                bg_task.cancel()
                try:
                    await bg_task
                except asyncio.CancelledError:
                    pass

            # Memory was written
            assert mem_path.exists()
            assert "Test" in mem_path.read_text()
            # Event logged
            assert "background_curated" in ev_path.read_text()
        print("✓ background_queue_processes_turn")
    asyncio.run(go())


def test_background_skips_trivial():
    """Trivial message (short msg, no delta) → no curation call."""
    async def go():
        with tempfile.TemporaryDirectory() as td:
            mem_path = Path(td) / "m.jsonl"
            ev_path = Path(td) / "e.jsonl"
            queue: asyncio.Queue = asyncio.Queue()
            bg = Background(queue,
                            memory_path=str(mem_path),
                            event_path=str(ev_path))
            call_counter = {"n": 0}
            async def fake_call(client, messages):
                call_counter["n"] += 1
                return ""
            with patch("background.call_model", fake_call):
                bg_task = asyncio.create_task(bg.run())
                await queue.put({
                    "user_message": "ok",
                    "final_text": "ack",
                    "blocks_executed": 0,
                    "memory_delta": 0,
                })
                await queue.join()
                bg_task.cancel()
                try:
                    await bg_task
                except asyncio.CancelledError:
                    pass
            assert call_counter["n"] == 0
        print("✓ background_skips_trivial")
    asyncio.run(go())


def test_find_recent_failed_exec():
    with tempfile.TemporaryDirectory() as td:
        ev = EventLog(Path(td) / "e.jsonl")
        ev.log("code_executed", iteration=1, lang="shell", ok=True,
               code_snippet="ok command")
        ev.log("code_executed", iteration=2, lang="shell", ok=False,
               code_snippet="bad command")
        ev.log("code_executed", iteration=3, lang="python", ok=True,
               code_snippet="another ok")
        last_fail = find_recent_failed_exec(ev)
        assert last_fail is not None
        assert last_fail["code_snippet"] == "bad command"
        # since_ts cuts off
        # Use a ts AFTER all events
        future = "9999"
        assert find_recent_failed_exec(ev, future) is None
        print("✓ find_recent_failed_exec")


def test_browse_thresholds_constants():
    from background import (BROWSE_BOREDOM_THRESHOLD,
                            BROWSE_CURIOSITY_THRESHOLD,
                            BROWSE_REQUESTS_PER_TICK,
                            BROWSE_REQUESTS_PER_HOUR)
    assert 0 < BROWSE_BOREDOM_THRESHOLD < 1
    assert 0 < BROWSE_CURIOSITY_THRESHOLD < 1
    assert BROWSE_REQUESTS_PER_TICK <= BROWSE_REQUESTS_PER_HOUR
    print("✓ browse_thresholds_constants")


def test_maybe_browse_skips_when_low_state():
    """Default state — no browse."""
    with tempfile.TemporaryDirectory() as td:
        events_p = Path(td) / "events.jsonl"
        memory_p = Path(td) / "memory.jsonl"
        state_p = Path(td) / "state.json"
        queue: asyncio.Queue = asyncio.Queue()
        bg = Background(queue,
                        memory_path=str(memory_p),
                        event_path=str(events_p),
                        state_path=str(state_p))
        called = {"n": 0}
        async def fake_call(client, messages):
            called["n"] += 1
            return ""
        async def go():
            with patch("background.call_model", fake_call):
                await bg._maybe_browse(None)
        asyncio.run(go())
        assert called["n"] == 0
        print("✓ maybe_browse_skips_when_low_state")


def test_maybe_browse_rate_limited():
    """Hourly budget exhausted → no call despite high state."""
    with tempfile.TemporaryDirectory() as td:
        events_p = Path(td) / "events.jsonl"
        memory_p = Path(td) / "memory.jsonl"
        state_p = Path(td) / "state.json"
        state_p.write_text(json.dumps(
            {"boredom": 0.8, "curiosity": 0.7}))
        ml = MemoryStore(memory_p)
        for i in range(5):
            ml.append("semantic", f"fact {i}", tags=["t"])
        ev = EventLog(events_p)
        for _ in range(5):
            ev.log("background_browse", requests_made=2)
        queue: asyncio.Queue = asyncio.Queue()
        bg = Background(queue,
                        memory_path=str(memory_p),
                        event_path=str(events_p),
                        state_path=str(state_p))
        called = {"n": 0}
        async def fake_call(client, messages):
            called["n"] += 1
            return ""
        async def go():
            with patch("background.call_model", fake_call):
                await bg._maybe_browse(None)
        asyncio.run(go())
        assert called["n"] == 0, "hour-budget should have blocked"
        assert any(e["type"] == "background_browse_skipped"
                   for e in ev.load())
        print("✓ maybe_browse_rate_limited")


def test_parse_id_array():
    from background import _parse_id_array
    # Pure JSON
    assert _parse_id_array('["mem_0001","mem_0007"]') == ["mem_0001", "mem_0007"]
    # Markdown fence
    assert _parse_id_array('```json\n["mem_0001"]\n```') == ["mem_0001"]
    # Explanation text around it
    assert _parse_id_array(
        'Here is my pick: ["mem_0042", "mem_0009"] fits best.'
    ) == ["mem_0042", "mem_0009"]
    # Garbage
    assert _parse_id_array("nothing here") == []
    assert _parse_id_array("") == []
    # Non-list-array
    assert _parse_id_array('{"foo": [1, 2]}') == []
    print("✓ parse_id_array")


def test_curate_selection_skips_under_threshold():
    """With fewer than SELECTION_MIN_TOTAL memories the curator must NOT
    make a selection call (no file, no model call)."""
    from background import SELECTION_MIN_TOTAL
    with tempfile.TemporaryDirectory() as td:
        events_p = Path(td) / "events.jsonl"
        memory_p = Path(td) / "memory.jsonl"
        state_p = Path(td) / "state.json"
        ml = MemoryStore(memory_p)
        # Fewer than SELECTION_MIN_TOTAL
        for i in range(SELECTION_MIN_TOTAL - 5):
            ml.append("semantic", f"fact {i}", tags=["random"])
        queue: asyncio.Queue = asyncio.Queue()
        bg = Background(queue,
                        memory_path=str(memory_p),
                        event_path=str(events_p),
                        state_path=str(state_p))
        # Override selection-output-path
        import background as bg_mod
        bg_mod.SELECTION_PATH = str(Path(td) / "context_selection.json")
        called = {"n": 0}
        async def fake_call(client, messages):
            called["n"] += 1
            return ""
        async def go():
            with patch("background.call_model", fake_call):
                await bg._curate_selection(None, "test", "test")
        asyncio.run(go())
        assert called["n"] == 0
        assert not Path(bg_mod.SELECTION_PATH).exists()
    print("✓ curate_selection_skips_under_threshold")


def test_curate_selection_writes_file():
    """With enough memories + valid model answer → context_selection.json
    is written with filtered IDs."""
    from background import SELECTION_MIN_TOTAL
    with tempfile.TemporaryDirectory() as td:
        events_p = Path(td) / "events.jsonl"
        memory_p = Path(td) / "memory.jsonl"
        state_p = Path(td) / "state.json"
        ml = MemoryStore(memory_p)
        for i in range(SELECTION_MIN_TOTAL + 5):
            ml.append("semantic", f"fact {i}", tags=[f"t{i}"])
        queue: asyncio.Queue = asyncio.Queue()
        bg = Background(queue,
                        memory_path=str(memory_p),
                        event_path=str(events_p),
                        state_path=str(state_p))
        import background as bg_mod
        bg_mod.SELECTION_PATH = str(Path(td) / "context_selection.json")
        # Model returns valid JSON array, plus garbage ID (will be filtered)
        async def fake_call(client, messages):
            return '["mem_0001", "mem_0003", "mem_9999"]'
        async def go():
            with patch("background.call_model", fake_call):
                await bg._curate_selection(None, "test user", "test bot")
        asyncio.run(go())
        assert Path(bg_mod.SELECTION_PATH).exists()
        import json as _j
        ids = _j.loads(Path(bg_mod.SELECTION_PATH).read_text())
        # mem_9999 must be filtered out (does not exist)
        assert ids == ["mem_0001", "mem_0003"]
    print("✓ curate_selection_writes_file")


if __name__ == "__main__":
    test_should_curate_short_skip()
    test_should_curate_substantive()
    test_should_curate_fresh_delta_short_skips()
    test_should_curate_long_message_even_with_delta()
    test_background_queue_processes_turn()
    test_background_skips_trivial()
    test_find_recent_failed_exec()
    test_browse_thresholds_constants()
    test_maybe_browse_skips_when_low_state()
    test_maybe_browse_rate_limited()
    test_parse_id_array()
    test_curate_selection_skips_under_threshold()
    test_curate_selection_writes_file()
    print("\nAll background tests passed.")
