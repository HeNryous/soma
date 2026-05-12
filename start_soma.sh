#!/bin/bash
# Start the Soma Telegram bot.
#
# Token is read from .env next to this script — see .env.example.
# A Telegram token can only be polled by ONE bot process at a time,
# so stop any other polling process first.
#
# Uses .venv/ if install.sh has been run; falls back to system python3.
set -e
cd "$(dirname "$0")"

if [ -x ".venv/bin/python3" ]; then
    PY=".venv/bin/python3"
else
    PY="python3"
fi

exec "$PY" telegram.py
