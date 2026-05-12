"""Minimaler .env-Parser. Eigener Parser statt python-dotenv (P1)."""
import os
from pathlib import Path


def load_env(path: str | Path = ".env") -> dict[str, str]:
    """Parst KEY=VALUE pro Zeile, ignoriert Kommentare und leere Zeilen.
    Setzt die Werte in os.environ. Returns das geparste Dict."""
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
        # .env hat Vorrang vor bereits gesetzten OS-Env-Vars
        # (übliche Konvention bei direnv/dotenv-Tools)
        os.environ[key] = value
    return result
