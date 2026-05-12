"""Smoke test for EventLog + correction detection."""
import tempfile
import time
from pathlib import Path

from events import (EventLog, detect_correction, build_correction_note,
                    render_recent_turns)


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
        # recent gibt die LETZTEN N
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


def test_detect_correction():
    # "Nein, kürzer" trifft auf "nein" als erstes Pattern in der Liste
    assert detect_correction("Nein, kürzer") == "nein"
    assert detect_correction("kürzer bitte") == "kürzer"
    assert detect_correction("Das ist falsch") == "falsch"
    assert detect_correction("nochmal bitte") == "nochmal"
    assert detect_correction("Was ist 2+2?") is None
    assert detect_correction("Erstelle eine Datei") is None
    # Lange Sätze nicht als Korrektur
    long_msg = "Erstelle eine Datei " * 10
    assert detect_correction(long_msg) is None
    print("✓ detect_correction")


def test_build_correction_note():
    note = build_correction_note(
        {"final_text": "Hier ist ein sehr langes Gedicht..."},
        "kürzer",
        "Nein, kürzer",
    )
    assert "CORRECTION-SIGNAL" in note
    assert "kürzer" in note
    assert "Gedicht" in note
    # Ohne last_response leerer String
    assert build_correction_note(None, "nein", "nein") == ""
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
        log.log("prompt_received", user_message="erste frage")
        log.log("model_call", iteration=1)
        log.log("response_sent", iterations=1, final_text="erste antwort")
        # Turn 2
        log.log("prompt_received", user_message="zweite frage")
        log.log("response_sent", iterations=1, final_text="zweite antwort")
        # Turn 3 (orphan prompt, no response)
        log.log("prompt_received", user_message="dritte unbeantwortet")
        # Turn 4 — komplett
        log.log("prompt_received", user_message="vierte frage")
        log.log("response_sent", iterations=2, final_text="vierte antwort")

        pairs = log.recent_turns(n=10)
        # Drei VOLLSTÄNDIGE Turns; orphan ignoriert
        assert len(pairs) == 3
        assert pairs[0]["user"] == "erste frage"
        assert pairs[0]["assistant"] == "erste antwort"
        assert pairs[-1]["user"] == "vierte frage"
        assert pairs[-1]["assistant"] == "vierte antwort"
        # n=2 → letzte 2
        pairs2 = log.recent_turns(n=2)
        assert len(pairs2) == 2
        assert pairs2[0]["user"] == "zweite frage"
        print("✓ recent_turns")


def test_detect_memory_promise():
    from events import detect_memory_promise
    assert detect_memory_promise("Gemerkt. Cloudly = Neo-Cloud.") == "gemerkt"
    assert detect_memory_promise("Ich notiere das.") == "ich notier"
    assert detect_memory_promise("Ich behalte es im Kopf.") == "ich behalte"
    assert detect_memory_promise("Habe ich gespeichert.") == "gespeichert"
    assert detect_memory_promise("Verstanden, danke.") is None
    assert detect_memory_promise("") is None
    print("✓ detect_memory_promise")


def test_build_broken_promise_note():
    from events import build_broken_promise_note
    # Broken: promise-phrase + blocks_executed=0
    ev = {"final_text": "Gemerkt. Cloudly = Neo-Cloud-Anbieter.",
          "blocks_executed": 0}
    note = build_broken_promise_note(ev)
    assert "BROKEN-PROMISE" in note
    assert "gemerkt" in note
    assert "Cloudly" in note
    # Wenn blocks > 0 → KEIN broken-promise
    ev2 = {"final_text": "Gemerkt.", "blocks_executed": 1}
    assert build_broken_promise_note(ev2) == ""
    # Wenn keine promise-Phrase → leer
    ev3 = {"final_text": "Verstanden.", "blocks_executed": 0}
    assert build_broken_promise_note(ev3) == ""
    # None → leer
    assert build_broken_promise_note(None) == ""
    print("✓ build_broken_promise_note")


def test_render_recent_turns():
    pairs = [
        {"user": "Hallo", "assistant": "Hi!"},
        {"user": "Wie geht's?", "assistant": "Gut, danke."},
    ]
    block = render_recent_turns(pairs)
    assert "Recent conversations" in block
    assert "Hallo" in block
    assert "Hi!" in block
    assert "Wie geht's?" in block
    assert "Gut, danke" in block
    # leere pairs → leerer string
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
    test_detect_correction()
    test_build_correction_note()
    test_robust_to_garbage()
    test_recent_turns()
    test_render_recent_turns()
    test_detect_memory_promise()
    test_build_broken_promise_note()
    print("\nAll event tests passed.")
