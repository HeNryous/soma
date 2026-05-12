"""Smoke test for state.py."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "src"))

import json
import tempfile
from pathlib import Path

from state import DEFAULTS, load, save, render


def test_load_defaults_when_missing():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "missing.json"
        s = load(p)
        assert s["curiosity"] == DEFAULTS["curiosity"]
        assert s["boredom"] == DEFAULTS["boredom"]
        assert s["confidence"] == DEFAULTS["confidence"]
        print("✓ load_defaults_when_missing")


def test_load_garbage_returns_defaults():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "garbage.json"
        p.write_text("not valid json")
        s = load(p)
        assert s["curiosity"] == DEFAULTS["curiosity"]
        print("✓ load_garbage_returns_defaults")


def test_load_clamps_out_of_range():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "x.json"
        p.write_text(json.dumps({"curiosity": 2.5, "boredom": -0.3,
                                 "confidence": 0.7}))
        s = load(p)
        assert s["curiosity"] == 1.0
        assert s["boredom"] == 0.0
        assert s["confidence"] == 0.7
        print("✓ load_clamps_out_of_range")


def test_save_then_load_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "x.json"
        save(p, {"curiosity": 0.8, "boredom": 0.2, "confidence": 0.6})
        s = load(p)
        assert abs(s["curiosity"] - 0.8) < 1e-6
        assert s["updated_at"]
        print("✓ save_then_load_roundtrip")


def test_render():
    s = {"curiosity": 0.7, "boredom": 0.15, "confidence": 0.92}
    r = render(s)
    assert "curiosity=0.70" in r
    assert "boredom=0.15" in r
    assert "confidence=0.92" in r
    print("✓ render")


if __name__ == "__main__":
    test_load_defaults_when_missing()
    test_load_garbage_returns_defaults()
    test_load_clamps_out_of_range()
    test_save_then_load_roundtrip()
    test_render()
    print("\nAll state tests passed.")
