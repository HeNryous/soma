"""
InnerState — three values (0.0-1.0) shaping the background's behavior.

Read side: code (load with default fallback on broken file).
Write side: the model (writes via shell-echo; no code computes them).

Persisted in /workspace/state.json. The model sees the values in the
background prompt and decides on its own updates (steps 0.05-0.15 per
cycle, clamped to [0.0, 1.0]).
"""
import json
from datetime import datetime
from pathlib import Path


DEFAULTS = {
    "curiosity": 0.5,
    "boredom": 0.0,
    "confidence": 0.5,
}


def load(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return dict(DEFAULTS)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    if not isinstance(d, dict):
        return dict(DEFAULTS)
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = d.get(k)
        try:
            v = float(v) if v is not None else DEFAULTS[k]
        except (TypeError, ValueError):
            v = DEFAULTS[k]
        out[k] = max(0.0, min(1.0, v))
    out["updated_at"] = d.get("updated_at", "")
    return out


def save(path: str | Path, state: dict) -> None:
    """Helper for tests — the model normally writes via shell."""
    clean = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in state:
            try:
                clean[k] = max(0.0, min(1.0, float(state[k])))
            except (TypeError, ValueError):
                pass
    clean["updated_at"] = datetime.now().isoformat(timespec="seconds")
    Path(path).write_text(json.dumps(clean), encoding="utf-8")


def render(state: dict) -> str:
    """Render for prompt injection."""
    c = state.get("curiosity", 0.5)
    b = state.get("boredom", 0.0)
    z = state.get("confidence", 0.5)
    return (f"Inner state: curiosity={c:.2f}, boredom={b:.2f}, "
            f"confidence={z:.2f}.")
