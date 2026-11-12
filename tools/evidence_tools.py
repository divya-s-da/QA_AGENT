"""Evidence recording tool.

The navigator model is unreliable at emitting a final JSON evidence block.
Instead it reliably calls tools. record_evidence() gives the model a direct,
deterministic way to persist each check's evidence into session state, which
the pipeline reads after the agent finishes.
"""

import json
import os
import re
from typing import Any
from urllib.parse import urlparse

from google.adk.tools.tool_context import ToolContext


def record_evidence(
    tool_context: ToolContext,
    check_id: str,
    status: str,
    note: str = "",
    screenshot_path: str = "",
    expected: str = "",
    expected_source: str = "",
) -> str:
    """Record the test result for one check into session state.

    Args:
        check_id: The check_id from the contract being tested.
        status: One of PASS, FAIL, BLOCKED.
        note: Short human-readable note about what was observed.
        screenshot_path: Path of the screenshot captured for this check ("" if none).
        expected: The pre-declared expected behaviour this check verifies
            (from the contract's expected_result). Written BEFORE the action —
            the agent must not invent it after observing.
        expected_source: Where the expectation comes from (charter field,
            spec doc, WCAG rule, HTTP semantics, contract id). A finding
            without a traceable expected_source is an observation, not a bug.
    """
    status = str(status).strip().upper()
    if status not in ("PASS", "FAIL", "BLOCKED"):
        status = "BLOCKED"
    evidence = tool_context.state.get("test_evidence") or []
    if not isinstance(evidence, list):
        evidence = []
    # Auto-attach the current page URL so the fixing AI always knows where
    # the finding happened, even if the model forgot to write it in the note.
    page_url = ""
    try:
        from .native_browser import get_current_url
        url = get_current_url()
        if url and not url.startswith("ERROR"):
            page_url = url
            note = f"[url: {url}] {note}".strip()
    except Exception:
        pass
    evidence.append({
        "check_id": check_id,
        "status": status,
        "note": note,
        "screenshot_path": screenshot_path,
        "expected": expected,
        "expected_source": expected_source,
        "url": page_url,
    })
    tool_context.state["test_evidence"] = evidence
    return f"Recorded evidence for check '{check_id}' with status {status}."


def record_verdict(
    tool_context: ToolContext,
    check_id: str,
    verdict: str,
    reason: str = "",
    screen_id: str = "",
) -> str:
    """Record the judge's verdict for one check into session state.

    Args:
        check_id: The check_id from the contract being judged.
        verdict: One of PASS, FAIL, BLOCKED.
        reason: Explanation of why the check passed or failed.
        screen_id: The screen_id the check belongs to ("" if unknown).
    """
    verdict = str(verdict).strip().upper()
    if verdict not in ("PASS", "FAIL", "BLOCKED"):
        verdict = "BLOCKED"
    verdicts = tool_context.state.get("judge_verdicts") or []
    if not isinstance(verdicts, list):
        verdicts = []
    verdicts.append({
        "check_id": check_id,
        "verdict": verdict,
        "reason": reason,
        "screen_id": screen_id,
    })
    tool_context.state["judge_verdicts"] = verdicts
    return f"Recorded verdict for check '{check_id}' with status {verdict}."


_INVENTORY_ROUTE = re.compile(r"(?<!\w)/([A-Za-z0-9][\w\-/]*)")
_INVENTORY_URL = re.compile(r"(?:https?|ftp)://[^\s\]\[()<>\"']+")


def _routes_from_inventory(evidence: list[dict]) -> list[str]:
    """Pull '/route' tokens out of the recorded 'inventory' evidence row."""
    for item in evidence:
        if isinstance(item, dict) and item.get("check_id") == "inventory":
            note = str(item.get("note", ""))
            # Do not treat the path part of a URL as a site route. Inventory
            # notes commonly mention the target URL, external links, and local
            # screenshot paths; the old regex incorrectly turned those into
            # routes such as '/example' and '/home/divya'.
            note = _INVENTORY_URL.sub(" ", note)
            home_path = os.path.expanduser("~").rstrip("/")
            if home_path.startswith("/"):
                note = re.sub(
                    re.escape(home_path) + r"/[^\s\]\[()<>\"']+",
                    " ",
                    note,
                )
            found = _INVENTORY_ROUTE.findall(note)
            routes = []
            for r in found:
                norm = "/" + r.strip("/").rstrip(".,;:)")
                if norm not in routes:
                    routes.append(norm)
            if routes:
                return routes
    return []


def _route_of(url: str) -> str:
    try:
        path = urlparse(url).path
    except Exception:
        return ""
    path = "/" + path.strip("/")
    return path


def coverage_report(
    tool_context: ToolContext,
    planned_routes: str = "",
) -> str:
    """Audit which inventory routes got real interaction evidence. Use this
    BEFORE declaring a session complete: it is the coverage contract.

    Cross-references the recorded 'inventory' evidence (plus any routes you
    list) against every evidence row that carries a page URL, and reports
    which routes have ZERO checks. Routes whose only evidence is an inventory
    row or a pure render check still count as untested for INTERACTION.

    Args:
        planned_routes: Optional JSON array or newline-separated routes
            (e.g. '["/personal", "/coaching"]') to audit in addition to
            whatever the inventory check recorded.
    """
    evidence = tool_context.state.get("test_evidence") or []
    if not isinstance(evidence, list):
        evidence = []

    routes = _routes_from_inventory(evidence)
    extra: list[str] = []
    if planned_routes.strip():
        try:
            data = json.loads(planned_routes)
            if isinstance(data, list):
                extra = [str(x) for x in data]
        except Exception:
            extra = planned_routes.replace(",", "\n").splitlines()
    for r in extra:
        raw = r.strip()
        norm = "/" if raw in ("", "/") else "/" + raw.strip("/")
        if norm not in routes:
            routes.append(norm)

    if not routes:
        return json.dumps({
            "status": "error",
            "detail": "No routes found: record the 'inventory' evidence row "
                      "first (or pass planned_routes).",
        })

    per_route: dict[str, list[str]] = {r: [] for r in routes}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("check_id", ""))
        if cid == "inventory":
            continue
        url = str(item.get("url", "")) or ""
        if not url:
            m = re.search(r"\[url: (\S+)", str(item.get("note", "")))
            if m:
                url = m.group(1).rstrip(".,;:)]}")
        route = _route_of(url)
        if route and route in per_route:
            per_route[route].append(cid)

    covered = {r: c for r, c in per_route.items() if c}
    untested = [r for r, c in per_route.items() if not c]
    render_only = [
        r for r, c in covered.items()
        if all(x.startswith("render") or x.endswith("_render") or x.startswith("invariant") for x in c)
    ]

    return json.dumps({
        "status": "ok",
        "routes_total": len(routes),
        "routes_covered": len(covered),
        "untested_routes": untested,
        "render_only_routes": render_only,
        "checks_per_route": {r: sorted(set(c)) for r, c in covered.items()},
        "verdict": (
            "COMPLETE" if not untested and not render_only
            else "INCOMPLETE — test every route listed in untested_routes and "
                 "add interaction checks to every render_only route before finishing."
        ),
    })
