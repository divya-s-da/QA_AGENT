"""Persistent learning store (failure-inversion §13).

User corrections and confirmed false positives are written back and loaded on
every future run, so the agent stops repeating the same mistakes. This is the
difference between a stateless agent and one that actually improves.
"""

import json
import os
from datetime import datetime

STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "learning_store.json"
)


def _load() -> dict:
    if not os.path.isfile(STORE_PATH):
        return {"by_design": [], "known_issues": [], "false_positives": [], "user_corrections": []}
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"by_design": [], "known_issues": [], "false_positives": [], "user_corrections": []}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_learning_context() -> str:
    """Render accumulated learning into prompt-injectable text."""
    data = _load()
    parts = []
    if data.get("known_issues"):
        parts.append(f"KNOWN_ISSUES (pre-declared, never report as new bugs): {json.dumps(data['known_issues'])}")
    if data.get("by_design"):
        parts.append(f"BY_DESIGN (intended behaviour, never report as bugs): {json.dumps(data['by_design'])}")
    if data.get("false_positives"):
        parts.append(f"FALSE_POSITIVES (already disproven, do not re-report): {json.dumps(data['false_positives'])}")
    if data.get("user_corrections"):
        parts.append(f"USER_CORRECTIONS (learning from past reviews): {json.dumps(data['user_corrections'])}")
    return "\n".join(parts)


def record_false_positive(check_id: str, note: str) -> None:
    data = _load()
    data["false_positives"].append({"check_id": check_id, "note": note, "ts": datetime.now().isoformat()})
    _save(data)


def record_user_correction(category: str, text: str) -> None:
    data = _load()
    if category not in data:
        data[category] = []
    data[category].append({"text": text, "ts": datetime.now().isoformat()})
    _save(data)
