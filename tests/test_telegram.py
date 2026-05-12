"""Smoke test for telegram.py — helpers without a live connection."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "src"))

import asyncio
from unittest.mock import AsyncMock, MagicMock

import telegram as tg


def test_safe_send_short():
    """Short texts are sent verbatim."""
    msg = MagicMock()
    msg.answer = AsyncMock()
    asyncio.run(tg._safe_send(msg, "short text"))
    msg.answer.assert_awaited_once_with("short text")
    print("✓ safe_send_short")


def test_safe_send_truncates():
    """Long texts are truncated at TELEGRAM_MAX."""
    msg = MagicMock()
    msg.answer = AsyncMock()
    long_text = "x" * (tg.TELEGRAM_MAX + 500)
    asyncio.run(tg._safe_send(msg, long_text))
    sent = msg.answer.call_args[0][0]
    assert len(sent) <= tg.TELEGRAM_MAX + 100  # plus marker
    assert "truncated" in sent
    print("✓ safe_send_truncates")


def test_safe_send_empty():
    """Empty strings → no API call."""
    msg = MagicMock()
    msg.answer = AsyncMock()
    asyncio.run(tg._safe_send(msg, ""))
    msg.answer.assert_not_awaited()
    print("✓ safe_send_empty")


def test_check_owner_match():
    msg = MagicMock()
    msg.chat.id = 1234
    assert tg._check_owner(msg, 1234) is True
    print("✓ check_owner_match")


def test_check_owner_mismatch():
    msg = MagicMock()
    msg.chat.id = 999
    assert tg._check_owner(msg, 1234) is False
    print("✓ check_owner_mismatch")


def test_module_constants():
    assert tg.TELEGRAM_MAX < 4096
    assert tg.TELEGRAM_MAX >= 3000
    print("✓ module_constants")


if __name__ == "__main__":
    test_module_constants()
    test_safe_send_short()
    test_safe_send_truncates()
    test_safe_send_empty()
    test_check_owner_match()
    test_check_owner_mismatch()
    print("\nAll telegram tests passed.")
