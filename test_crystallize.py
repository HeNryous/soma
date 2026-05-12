"""Smoke test for crystallize.py."""
import tempfile
from pathlib import Path

from events import EventLog
from memory import MemoryStore
from crystallize import extract_pattern, crystallize, existing_crystallized_patterns


def test_extract_pattern():
    assert extract_pattern("shell", "echo 'hi' > /tmp/x") == "shell:echo"
    assert extract_pattern("bash", "cat /tmp/y") == "shell:cat"
    assert extract_pattern("sh", "ls -la") == "shell:ls"
    assert extract_pattern("python", "print(2+2)") == "python:print"
    assert extract_pattern("python", "import os") == "python:import"
    assert extract_pattern("shell", "") is None
    assert extract_pattern("shell", "   ") is None
    # Code beginnend mit Sonderzeichen → kein Identifier
    assert extract_pattern("python", "@decorator") is None
    print("✓ extract_pattern")


def test_crystallize_basic():
    with tempfile.TemporaryDirectory() as td:
        events_p = Path(td) / "events.jsonl"
        memory_p = Path(td) / "memory.jsonl"
        log = EventLog(events_p)
        # 3 erfolgreiche echo (über threshold)
        for i in range(3):
            log.log("code_executed", iteration=1, lang="shell", ok=True,
                    code_snippet=f"echo 'test {i}' > /workspace/test{i}.txt")
        # 2 erfolgreiche cat (unter threshold)
        for i in range(2):
            log.log("code_executed", iteration=1, lang="shell", ok=True,
                    code_snippet=f"cat /workspace/test{i}.txt")
        # 1 echo MIT Fehler → darf NICHT zählen
        log.log("code_executed", iteration=1, lang="shell", ok=False,
                code_snippet="echo bad > /readonly")

        written = crystallize(str(events_p), str(memory_p), threshold=3)
        assert len(written) == 1, f"expected 1, got {len(written)}"
        assert written[0]["pattern"] == "shell:echo"
        assert written[0]["count"] == 3

        ms = MemoryStore(memory_p).load()
        assert len(ms) == 1
        assert ms[0]["type"] == "procedural"
        assert set(ms[0]["tags"]) == {"crystallized", "shell", "echo"}
        assert "PROCEDURE" in ms[0]["content"]
        assert "echo" in ms[0]["content"]
        print("✓ crystallize_basic")


def test_crystallize_idempotent():
    with tempfile.TemporaryDirectory() as td:
        events_p = Path(td) / "events.jsonl"
        memory_p = Path(td) / "memory.jsonl"
        log = EventLog(events_p)
        for i in range(4):
            log.log("code_executed", iteration=1, lang="shell", ok=True,
                    code_snippet=f"echo 'hi' > /tmp/{i}.txt")

        w1 = crystallize(str(events_p), str(memory_p), threshold=3)
        assert len(w1) == 1
        w2 = crystallize(str(events_p), str(memory_p), threshold=3)
        assert len(w2) == 0, f"second run wrote: {w2}"
        assert len(MemoryStore(memory_p).load()) == 1
        print("✓ crystallize_idempotent")


def test_existing_patterns_lookup():
    with tempfile.TemporaryDirectory() as td:
        memory_p = Path(td) / "memory.jsonl"
        store = MemoryStore(memory_p)
        store.append("procedural", "PROCEDURE: shell:echo …",
                     tags=["crystallized", "shell", "echo"])
        store.append("semantic", "ein Fakt", tags=["random"])
        store.append("procedural", "manuell geschrieben",
                     tags=["procedure"])  # KEIN crystallized-Tag
        patterns = existing_crystallized_patterns(store)
        assert patterns == {"shell:echo"}
        print("✓ existing_patterns_lookup")


def test_crystallize_python_pattern():
    """Mehrere Sprachen, mehrere Patterns parallel."""
    with tempfile.TemporaryDirectory() as td:
        events_p = Path(td) / "events.jsonl"
        memory_p = Path(td) / "memory.jsonl"
        log = EventLog(events_p)
        for i in range(3):
            log.log("code_executed", iteration=1, lang="python", ok=True,
                    code_snippet=f"import os; print(os.listdir('/tmp/{i}'))")
        for i in range(3):
            log.log("code_executed", iteration=1, lang="shell", ok=True,
                    code_snippet=f"ls /workspace/{i}")
        written = crystallize(str(events_p), str(memory_p), threshold=3)
        patterns = {w["pattern"] for w in written}
        assert patterns == {"python:import", "shell:ls"}, f"got {patterns}"
        print("✓ crystallize_python_pattern")


if __name__ == "__main__":
    test_extract_pattern()
    test_existing_patterns_lookup()
    test_crystallize_basic()
    test_crystallize_idempotent()
    test_crystallize_python_pattern()
    print("\nAll crystallize tests passed.")
