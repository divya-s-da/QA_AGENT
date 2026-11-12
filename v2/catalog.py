"""QA Agent v2: the critical-bug catalog (a checklist, no model).

docs/V2_BUG_CATALOG.yaml lists the bugs that each kind of web product usually has: 27 site types and a
set of generic bugs that apply to every site. Each bug has an area, a severity (T0 worst), a symptom, the
black-box steps to find it, and the correct behaviour.

This module does two plain-code jobs:
1. detect_types(): read the site map (routes, titles, text, control names) and say what kind of product
   this is, by the signal words of each type.
2. select(): pick the applicable critical bugs (T0 and T1) that a tester can check with the browser, so
   the testers always check the known-dangerous features, not only the ones the model thought of.

The planner adds these as tester tasks. The task gives the tester the steps and the correct result from
the catalog; the judge then decides from the evidence, the same as for any task. The code-only bugs in the
catalog (headers, cookie flags, exposed files, leaked keys) are not seeded here; they are a separate job.
"""

from __future__ import annotations

import os
import re

import yaml

CATALOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "V2_BUG_CATALOG.yaml")
SEVERITY_RISK = {"T0": 3, "T1": 2, "T2": 1}
# areas that usually span several pages (a flow): the journey team runs them; the rest go to the function team.
FLOW_AREAS = {"money", "session", "auth", "access"}
# the signature bugs the testers miss on their own: a success trap, a live session after log out, a normal
# user reaching admin, a price trusted from the client. Always seeded first when they apply to the site.
PINNED = ("GEN-FORM-001", "GEN-AUTH-001", "GEN-ACCESS-003", "GEN-MONEY-002")
_CACHE: dict | None = None


def load(path: str = CATALOG_PATH) -> dict:
    global _CACHE
    if _CACHE is None:
        with open(path, encoding="utf-8") as f:
            _CACHE = yaml.safe_load(f) or {}
    return _CACHE


def _corpus(sitemap) -> str:
    """Everything the site shows, as one lower-case string: routes, titles, page text, control names."""
    parts = []
    for p in sitemap.pages.values():
        parts.append(p.route)
        parts.append(p.title or "")
        parts.append((p.text or "")[:800])
    for e in sitemap.elements.values():
        parts.append(f"{e.name} {e.kind}")
    return " ".join(parts).lower()


def detect_types(sitemap, catalog: dict | None = None, top: int = 3) -> list[str]:
    """The site type ids that best fit the site, most signals first. Always keeps at least the best one."""
    catalog = catalog or load()
    text = _corpus(sitemap)
    scored = []
    for st in catalog.get("site_types", []):
        hits = 0
        for sig in st.get("signals", []):
            s = str(sig).lower().strip()
            if not s:
                continue
            # a whole-word match for short signals, a substring match for phrases
            if (" " in s and s in text) or (" " not in s and re.search(r"\b" + re.escape(s) + r"\b", text)):
                hits += 1
        if hits:
            scored.append((hits, st["id"]))
    scored.sort(reverse=True)
    if not scored:
        return []
    # keep the strong matches only: a weak second or third type (a few shared words) adds off-target bugs
    best = scored[0][0]
    strong = [i for h, i in scored if h >= max(2, best * 0.6)]
    return (strong or [scored[0][1]])[:top]


def select(sitemap, *, have_login: bool, two_accounts: bool, allow_writes: bool,
           catalog: dict | None = None, limit: int = 10) -> tuple[list[str], list[dict]]:
    """Return (detected type ids, chosen bug tasks). Each task: title, how, expected, area, team, risk, severity.
    Only check:task bugs of severity T0/T1 that this run can actually test are kept."""
    catalog = catalog or load()
    types = detect_types(sitemap, catalog)
    type_set = set(types)
    chosen = []
    for b in catalog.get("bugs", []):
        if b.get("check") != "task" or b.get("severity") not in ("T0", "T1"):
            continue
        applies = b.get("applies") or []
        if "all" not in applies and not (set(applies) & type_set):
            continue
        if b.get("needs_two_accounts") and not two_accounts:
            continue
        if b.get("needs_write") and not allow_writes:
            continue
        area = b.get("area", "")
        if area in ("auth", "session", "access") and not have_login:
            continue                                        # these need a logged-in account to check
        chosen.append({
            "id": b["id"], "title": str(b.get("title", "check"))[:140], "how": str(b.get("how", "")),
            "expected": str(b.get("expected", "")), "symptom": str(b.get("symptom", "")),
            "area": area, "severity": b.get("severity", "T1"),
            "risk": SEVERITY_RISK.get(b.get("severity", "T1"), 2),
            "team": "journey" if area in FLOW_AREAS else "function",
        })
    # The signature bugs of this product: a success trap, a still-valid session after log out, access by a
    # normal user to admin, a price trusted from the client. These are the ones the testers miss on their own,
    # so always seed them when they apply, before anything else fills the limit.
    def rank(t):
        return (t["severity"] != "T0", not t["id"].startswith("GEN-"))
    by_id = {t["id"]: t for t in chosen}
    pinned = [by_id[i] for i in PINNED if i in by_id]
    rest = sorted((t for t in chosen if t["id"] not in {p["id"] for p in pinned}), key=rank)
    # after the pinned signature bugs, keep a balance of generic and type-specific up to the limit
    generic = [t for t in rest if t["id"].startswith("GEN-")]
    specific = [t for t in rest if not t["id"].startswith("GEN-")]
    room = max(0, limit - len(pinned))
    fill = (generic[: max(3, room // 2)] + specific)[:room]
    out = pinned + sorted(fill, key=rank)
    return types, out[:limit]
