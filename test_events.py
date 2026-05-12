"""Smoke test for EventLog + correction detection."""
import tempfile
from pathlib import Path

from events import (EventLog, build_correction_note,
                    build_broken_promise_note, render_recent_turns)


def test_log_and_load():
    with tempfile.TemporaryDirectory() as td:
        log = EventLog(Path(td) / "e.jsonl")
        assert log.load() == []
        log.log("prompt_received", user_message="hi")
        log.log("model_call", iteration=1, blocks=0)
        log.log("response_sent", iterations=1, final_text="hello")
        events = log.load()
        assert len(events) == 3
        assert events[0]["type"] == "prompt_received"
        assert events[1]["iteration"] == 1
        assert events[2]["final_text"] == "hello"
        print("✓ log_and_load")


def test_by_type_and_recent():
    with tempfile.TemporaryDirectory() as td:
        log = EventLog(Path(td) / "e.jsonl")
        for i in range(5):
            log.log("model_call", iteration=i)
            log.log("code_executed", iteration=i, ok=True)
        calls = log.by_type("model_call")
        execs = log.by_type("code_executed")
        assert len(calls) == 5
        assert len(execs) == 5
        recent = log.recent(3)
        assert len(recent) == 3
        # `recent` returns the LAST N
        assert recent[-1]["type"] == "code_executed"
        assert recent[-1]["iteration"] == 4
        print("✓ by_type_and_recent")


def test_last():
    with tempfile.TemporaryDirectory() as td:
        log = EventLog(Path(td) / "e.jsonl")
        log.log("response_sent", final_text="first")
        log.log("response_sent", final_text="second")
        log.log("model_call", iteration=1)
        last_resp = log.last("response_sent")
        assert last_resp is not None
        assert last_resp["final_text"] == "second"
        assert log.last("nonexistent") is None
        print("✓ last")


def test_build_correction_note():
    note = build_correction_note(
        {"final_text": "Here is a very long poem..."},
        "correction",
        "shorter please",
    )
    assert "CORRECTION-SIGNAL" in note
    assert "shorter please" in note
    assert "poem" in note
    # Without last_response → empty string
    assert build_correction_note(None, "correction", "x") == ""
    print("✓ build_correction_note")


def test_robust_to_garbage():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "e.jsonl"
        p.write_text(
            '{"type":"valid","ts":"2026-01-01"}\n'
            'not json\n'
            '\n'
            '{"type":"valid2","ts":"2026-01-02"}\n'
        )
        log = EventLog(p)
        events = log.load()
        assert len(events) == 2
        print("✓ robust_to_garbage")


def test_recent_turns():
    with tempfile.TemporaryDirectory() as td:
        log = EventLog(Path(td) / "e.jsonl")
        # Turn 1
        log.log("prompt_received", user_message="first question")
        log.log("model_call", iteration=1)
        log.log("response_sent", iterations=1, final_text="first answer")
        # Turn 2
        log.log("prompt_received", user_message="second question")
        log.log("response_sent", iterations=1, final_text="second answer")
        # Turn 3 (orphan prompt, no response)
        log.log("prompt_received", user_message="third unanswered")
        # Turn 4 — complete
        log.log("prompt_received", user_message="fourth question")
        log.log("response_sent", iterations=2, final_text="fourth answer")

        pairs = log.recent_turns(n=10)
        # Three COMPLETE turns; orphan ignored
        assert len(pairs) == 3
        assert pairs[0]["user"] == "first question"
        assert pairs[0]["assistant"] == "first answer"
        assert pairs[-1]["user"] == "fourth question"
        assert pairs[-1]["assistant"] == "fourth answer"
        # n=2 → last 2
        pairs2 = log.recent_turns(n=2)
        assert len(pairs2) == 2
        assert pairs2[0]["user"] == "second question"
        print("✓ recent_turns")


def test_build_broken_promise_note():
    # With promise signal + blocks_executed=0 → reminder rendered
    ev = {"final_text": "Noted. Cloudly is a neo-cloud provider.",
          "blocks_executed": 0}
    note = build_broken_promise_note(ev, "memory_promise")
    assert "BROKEN-PROMISE" in note
    assert "Cloudly" in note
    # blocks > 0 → NOT a broken promise even with signal
    ev2 = {"final_text": "Noted.", "blocks_executed": 1}
    assert build_broken_promise_note(ev2, "memory_promise") == ""
    # No signal → empty
    ev3 = {"final_text": "Noted.", "blocks_executed": 0}
    assert build_broken_promise_note(ev3, "") == ""
    # None → empty
    assert build_broken_promise_note(None, "memory_promise") == ""
    print("✓ build_broken_promise_note")


def test_render_recent_turns():
    pairs = [
        {"user": "Hello", "assistant": "Hi!"},
        {"user": "How are you?", "assistant": "Good, thanks."},
    ]
    block = render_recent_turns(pairs)
    assert "Recent conversations" in block
    assert "Hello" in block
    assert "Hi!" in block
    assert "How are you?" in block
    assert "Good, thanks" in block
    # Empty pairs → empty string
    assert render_recent_turns([]) == ""
    # Truncation
    long_pair = [{"user": "x" * 500, "assistant": "y" * 500}]
    block = render_recent_turns(long_pair, max_chars=50)
    assert "…" in block  # truncated marker
    print("✓ render_recent_turns")


if __name__ == "__main__":
    test_log_and_load()
    test_by_type_and_recent()
    test_last()
    test_build_correction_note()
    test_robust_to_garbage()
    test_recent_turns()
    test_render_recent_turns()
    test_build_broken_promise_note()
    print("\nAll event tests passed.")
