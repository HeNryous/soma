"""Minimal .env parser. Custom parser instead of python-dotenv (P1)."""
import os
from pathlib import Path


def load_env(path: str | Path = ".env") -> dict[str, str]:
    """Parse KEY=VALUE per line, ignore comments and blank lines.
    Sets each value into os.environ. Returns the parsed dict."""
    p = Path(path)
    result: dict[str, str] = {}
    if not p.exists():
        return result
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        result[key] = value
        # .env takes precedence over already-set OS env vars
        # (common convention with direnv / dotenv tools)
        os.environ[key] = value
    return result
