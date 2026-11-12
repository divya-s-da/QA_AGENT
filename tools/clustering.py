"""Defect clustering (failure-inversion §12).

A confirmed bug in area A triggers a BUDGETED deep-dive on A's siblings and
shared components. Bugs cluster — exploit it, with a cap so it doesn't become
tunnel vision.
"""

import json
import os

CLUSTER_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "cluster.json"
)


def _load_cluster() -> dict:
    if not os.path.isfile(CLUSTER_FILE):
        return {"dives": []}
    try:
        with open(CLUSTER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"dives": []}


def _save_cluster(data: dict) -> None:
    os.makedirs(os.path.dirname(CLUSTER_FILE), exist_ok=True)
    with open(CLUSTER_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def sibling_routes(route: str, all_routes: list[str], max_siblings: int = 2) -> list[str]:
    """Pick sibling routes of a confirmed-bug route: same top-level segment."""
    top = route.strip("/").split("/")[0] if route.strip("/") else ""
    if not top:
        return []
    siblings = [r for r in all_routes if r != route and r.strip("/").split("/")[0] == top]
    return siblings[:max_siblings]


def register_confirmed_bug(check_id: str, route: str) -> None:
    data = _load_cluster()
    data["dives"].append({"check_id": check_id, "route": route})
    _save_cluster(data)


def dive_budget_used() -> int:
    return len(_load_cluster()["dives"])


def reset_cluster() -> None:
    _save_cluster({"dives": []})
