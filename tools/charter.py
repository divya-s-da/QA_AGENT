"""Test charter — the intake contract that gates test planning.

Implements section 1 of the failure-inversion architecture: the charter is
the ONLY legitimate source of "expected behaviour". Findings whose
expectation is not traceable to the charter, a spec doc, or a named heuristic
are filed as observations, never as bugs.

Missing required fields fail closed: no defaults, no "I'll assume".
"""

import json
import os
from typing import Any

REQUIRED_FIELDS = ["product.name", "product.type"]
OPTIONAL_FIELDS = [
    "product.environments",
    "modalities",
    "personas",
    "critical_user_journeys",
    "device_matrix",
    "out_of_scope",
    "known_issues",
    "by_design",
    "severity_rubric",
    "budget",
    "destructive_actions_allowed",
]


def _get_path(obj, dotted: str):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def load_charter(path: str) -> dict:
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f"Charter file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
            data = yaml.safe_load(text)
        except Exception:
            raise ValueError("Charter must be valid JSON or YAML.")
    if not isinstance(data, dict):
        raise ValueError("Charter must be a mapping (dict).")
    return data


def validate_charter(data: dict) -> list[str]:
    missing = []
    for field in REQUIRED_FIELDS:
        if _get_path(data, field) is None:
            missing.append(field)
    if "known_issues" in data and not isinstance(data["known_issues"], list):
        missing.append("known_issues (must be a list)")
    if "by_design" in data and not isinstance(data["by_design"], list):
        missing.append("by_design (must be a list)")
    return missing


def charter_to_context(data: dict) -> str:
    """Render the charter into prompt-injectable plain text."""
    lines = ["TEST CHARTER (the only legitimate source of expected behaviour):"]
    product = data.get("product", {})
    lines.append(f"- product: {product.get('name', '?')} ({product.get('type', '?')})")
    envs = product.get("environments")
    if envs:
        lines.append(f"- environments: {json.dumps(envs)}")
    personas = data.get("personas", [])
    if personas:
        lines.append(f"- personas: {json.dumps(personas)}")
    cujs = data.get("critical_user_journeys", [])
    if cujs:
        lines.append(f"- critical_user_journeys: {json.dumps(cujs)}")
    devices = data.get("device_matrix")
    if devices:
        lines.append(f"- device_matrix: {json.dumps(devices)}")
    out = data.get("out_of_scope", [])
    if out:
        lines.append(f"- OUT_OF_SCOPE (do not test, do not report): {json.dumps(out)}")
    known = data.get("known_issues", [])
    if known:
        lines.append(f"- KNOWN_ISSUES (pre-declared; never report as new bugs): {json.dumps(known)}")
    by_design = data.get("by_design", [])
    if by_design:
        lines.append(f"- BY_DESIGN (behaviours that look broken but are intended): {json.dumps(by_design)}")
    budget = data.get("budget")
    if budget:
        lines.append(f"- budget: {json.dumps(budget)}")
    lines.append(
        "RULES: expected behaviour must be traceable to this charter or a named "
        "heuristic (WCAG, HTTP semantics). Any finding whose expectation is not "
        "traceable is an OBSERVATION, not a bug. Behaviours in OUT_OF_SCOPE, "
        "KNOWN_ISSUES or BY_DESIGN are never reported as defects."
    )
    return "\n".join(lines)
