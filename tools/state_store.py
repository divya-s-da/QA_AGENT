"""Write-ahead journal + resumable cursor (failure-inversion §6).

Every step appends a record to durable storage BEFORE the next step begins.
Crash -> replay journal -> resume at exact cursor. The journal is the memory;
the transcript is disposable.
"""

import json
import os
import time
from datetime import datetime

STORE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
JOURNAL_FILE = os.path.join(STORE_DIR, "qa_journal.jsonl")
CURSOR_FILE = os.path.join(STORE_DIR, "qa_cursor.json")


def _ensure_dir():
    os.makedirs(STORE_DIR, exist_ok=True)


def journal_step(entry: dict) -> None:
    """Append one journal record (write-ahead: called BEFORE next step runs)."""
    _ensure_dir()
    entry.setdefault("ts", datetime.now().isoformat())
    entry.setdefault("wall", time.time())
    with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def read_journal(limit: int = 0) -> list[dict]:
    if not os.path.isfile(JOURNAL_FILE):
        return []
    entries = []
    with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries[-limit:] if limit else entries


def save_cursor(state: dict) -> None:
    _ensure_dir()
    with open(CURSOR_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


def load_cursor() -> dict | None:
    if not os.path.isfile(CURSOR_FILE):
        return None
    try:
        with open(CURSOR_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def clear_run_state() -> None:
    for path in (JOURNAL_FILE, CURSOR_FILE):
        if os.path.isfile(path):
            os.remove(path)
