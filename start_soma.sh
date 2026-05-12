#!/bin/bash
# Startet den Soma-Telegram-Bot.
#
# Token kommt aus .env neben diesem Script — siehe .env.example.
# Ein Telegram-Token kann nur EINEN Bot-Prozess gleichzeitig pollen,
# also vorher andere Polling-Prozesse stoppen.
set -e
cd "$(dirname "$0")"
exec python3 telegram.py
