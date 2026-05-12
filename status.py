"""Status-Dashboard CLI. `python status.py` → menschenlesbare Übersicht."""
import sys

from core import EVENT_PATH, MEMORY_PATH
from events import EventLog
from memory import MemoryStore
from self_model import describe


def main() -> int:
    store = MemoryStore(MEMORY_PATH)
    events = EventLog(EVENT_PATH)
    print(describe(store, events))
    return 0


if __name__ == "__main__":
    sys.exit(main())
