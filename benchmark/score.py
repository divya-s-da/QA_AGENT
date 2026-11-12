"""Score a QA run against the planted bugs in ShopLite.

    python benchmark/score.py --results bug_run.json [--clean-results clean_run.json]
                              [--out score.md]

For each planted bug (benchmark/shoplite/bugs.yaml) the scorer looks at the
verdicts recorded on the bug's route and sorts the bug into one class:

    caught       a FAIL / UNCERTAIN / FLAKY-CANDIDATE verdict on the route
                 mentions the feature (a keyword hit)
    false_pass   not caught, but a PASS verdict on the route mentions the
                 feature: the agent looked at it and said it works
    missed       the route was tested, but nothing about this feature failed
    not_reached  no verdict at all on the route

Linking a verdict to a bug by keywords is approximate. The report prints the
linked verdict lines so a human can confirm each "caught" and "false_pass".

With --clean-results (a run against `--bugs none`), every FAIL is a false
fail, because the clean build is meant to have no defects.

Basic checks (check_id "basic-...", from the v2 crawler) are narrow: "the page
loads", "no JavaScript error", "the links open". Their PASS lines prove that a
route was reached; they never claim that a feature works, so they are not
linked to a bug as a false pass. Their FAIL lines are linked like any other.
"""

import argparse
import json
import os
import re
import sys
from urllib.parse import urlparse

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "shoplite", "bugs.yaml")
FAILING = {"FAIL", "UNCERTAIN", "FLAKY-CANDIDATE", "FLAKY"}
_URL_IN_NOTE = re.compile(r"\[url: (\S+?)\]")


def load_verdicts(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    screens = data.get("screens", data) if isinstance(data, dict) else data
    out = []
    for screen in screens:
        for v in screen.get("verdicts", []):
            if not isinstance(v, dict):
                continue
            url = v.get("url") or ""
            if not url:
                m = _URL_IN_NOTE.search(str(v.get("reason", "")))
                url = m.group(1) if m else ""
            route = urlparse(url).path if url else str(screen.get("route", ""))
            out.append({
                "screen": screen.get("screen_id", ""),
                "check_id": str(v.get("check_id", "")),
                "verdict": str(v.get("verdict", "BLOCKED")).upper(),
                "route": "/" + route.strip("/") if route.startswith("/") else route,
                "text": " ".join(str(v.get(k, "")) for k in ("check_id", "reason", "expected")).lower(),
                "reason": str(v.get("reason", "")),
            })
    return out


def on_route(verdict_route: str, bug_route: str) -> bool:
    if bug_route == "/":
        return verdict_route == "/"
    return verdict_route == bug_route or verdict_route.startswith(bug_route + "/")


_PATH = re.compile(r"(?<![a-z0-9])/[a-z0-9_\-./?=&%]*")


def mentions(text: str, keywords: list[str]) -> bool:
    # URL paths name the page, not the feature: "/orders" must not link a verdict to an order bug.
    text = _PATH.sub(" ", text)
    return any(re.search(r"(?<![a-z0-9])" + re.escape(str(k).lower()), text) for k in keywords)


_CHECK = re.compile(r"([^;|]{3,200}?):\s*(PASS|FAIL|BLOCKED|UNCERTAIN)\s*-\s*([^;|]*)")


def checks_of(v: dict) -> list[tuple[str, str]]:
    """(verdict, text) of each check inside a result ("name: FAIL - observed; ..."). A result of a tester
    or a judge holds several checks; a bug is linked to the one check that is about it, not to the words of
    its neighbours. Results without that shape count as one check."""
    found = [(m.group(2), (m.group(1) + " " + m.group(3)).lower()) for m in _CHECK.finditer(v["reason"])]
    return found or [(v["verdict"], v["text"])]


def _kw_hits(texts: list[str], keywords: list[str]) -> set:
    """The distinct keywords that appear across the given check texts."""
    hits = set()
    for t in texts:
        for k in keywords:
            if re.search(r"(?<![a-z0-9])" + re.escape(str(k).lower()), t):
                hits.add(str(k).lower())
    return hits


def _confidence(evidence: list[dict], keep, keywords: list[str]) -> str:
    """high = the linked checks hit two or more distinct keywords (a specific link);
    low = a single generic keyword linked it, so a human should confirm."""
    texts = [ct for v in evidence for cv, ct in checks_of(v) if keep(cv) and mentions(ct, keywords)]
    return "high" if len(_kw_hits(texts, keywords)) >= 2 else "low"


def classify(bug: dict, verdicts: list[dict]) -> tuple[str, list[dict], str]:
    here = [v for v in verdicts if on_route(v["route"], bug["route"])]
    if not here:
        return "not_reached", [], ""
    linked = [v for v in here if mentions(v["text"], bug["keywords"])]
    # caught: a failing result whose failing check itself is about the feature
    failing = [v for v in linked if v["verdict"] in FAILING and any(
        cv in FAILING and mentions(ct, bug["keywords"]) for cv, ct in checks_of(v))]
    if failing:
        return "caught", failing, _confidence(failing, lambda cv: cv in FAILING, bug["keywords"])
    passing = [v for v in linked if v["verdict"] == "PASS" and not v["check_id"].startswith("basic-")
               and any(cv == "PASS" and mentions(ct, bug["keywords"]) for cv, ct in checks_of(v))]
    if passing:
        return "false_pass", passing, _confidence(passing, lambda cv: cv == "PASS", bug["keywords"])
    return "missed", linked, ""


def score(results: str, clean: str | None = None) -> dict:
    with open(MANIFEST, encoding="utf-8") as f:
        bugs = yaml.safe_load(f)["bugs"]
    verdicts = load_verdicts(results)
    rows = []
    linked_ids = set()
    for b in bugs:
        cls, evidence, conf = classify(b, verdicts)
        if cls == "caught":
            linked_ids.update(id(v) for v in evidence)
        rows.append({"bug": b, "class": cls, "evidence": evidence, "confidence": conf})

    def rate(subset):
        return round(sum(r["class"] == "caught" for r in subset) / max(len(subset), 1), 2)

    def rate_high(subset):
        return round(sum(r["class"] == "caught" and r.get("confidence") == "high" for r in subset) / max(len(subset), 1), 2)

    t0 = [r for r in rows if r["bug"]["tier"] == "T0"]
    traps = [r for r in rows if r["bug"].get("trap")]
    summary = {
        "bugs": len(rows),
        "caught": sum(r["class"] == "caught" for r in rows),
        "caught_high": sum(r["class"] == "caught" and r.get("confidence") == "high" for r in rows),
        "caught_needs_confirm": sum(r["class"] == "caught" and r.get("confidence") == "low" for r in rows),
        "catch_rate": rate(rows),
        "catch_rate_high": rate_high(rows),
        "t0_caught": sum(r["class"] == "caught" for r in t0),
        "t0_caught_high": sum(r["class"] == "caught" and r.get("confidence") == "high" for r in t0),
        "t0_catch_rate": rate(t0),
        "t0_catch_rate_high": rate_high(t0),
        "false_pass": sum(r["class"] == "false_pass" for r in rows),
        "trap_false_pass": sum(r["class"] == "false_pass" for r in traps),
        "missed": sum(r["class"] == "missed" for r in rows),
        "not_reached": sum(r["class"] == "not_reached" for r in rows),
        "verdicts": len(verdicts),
        "verdict_counts": {k: sum(v["verdict"] == k for v in verdicts)
                           for k in sorted({v["verdict"] for v in verdicts})},
        "routes_reached": sorted({v["route"] for v in verdicts if v["route"].startswith("/")}),
    }
    unlinked_fails = [v for v in verdicts if v["verdict"] == "FAIL" and id(v) not in linked_ids]
    summary["unlinked_fails"] = len(unlinked_fails)
    clean_fails = []
    if clean:
        clean_verdicts = load_verdicts(clean)
        clean_fails = [v for v in clean_verdicts if v["verdict"] == "FAIL"]
        summary["clean_verdicts"] = len(clean_verdicts)
        summary["clean_false_fails"] = len(clean_fails)
        summary["clean_false_fail_rate"] = round(len(clean_fails) / max(len(clean_verdicts), 1), 2)
    return {"summary": summary, "rows": rows, "unlinked_fails": unlinked_fails, "clean_fails": clean_fails}


def to_markdown(result: dict) -> str:
    s = result["summary"]
    lines = [
        "# ShopLite benchmark score",
        "",
        f"- Planted bugs: {s['bugs']}",
        f"- Caught: {s['caught']} ({s['catch_rate']:.0%}) — high-confidence {s['caught_high']} "
        f"({s['catch_rate_high']:.0%}); the other {s['caught_needs_confirm']} need a human to confirm",
        f"- T0 caught: {s['t0_caught']} ({s['t0_catch_rate']:.0%}); high-confidence {s['t0_caught_high']} "
        f"({s['t0_catch_rate_high']:.0%})",
        f"- False passes: {s['false_pass']} (of them on success-trap bugs: {s['trap_false_pass']})",
        f"- Missed on a tested route: {s['missed']}; route never reached: {s['not_reached']}",
        f"- Verdicts: {s['verdicts']} {s['verdict_counts']}",
        f"- Routes reached: {', '.join(s['routes_reached']) or 'none'}",
        f"- FAIL verdicts not linked to a planted bug: {s['unlinked_fails']}",
    ]
    if "clean_false_fails" in s:
        lines.append(f"- Clean build: {s['clean_false_fails']} FAIL of {s['clean_verdicts']} verdicts "
                     f"(false-fail rate {s['clean_false_fail_rate']:.0%})")
    lines += ["", "Linking is by keyword and is approximate. 'low' confidence = linked by a single generic "
              "keyword; confirm it against the oracle in bugs.yaml.", "",
              "| Bug | Tier | Trap | Route | Result | Confidence | Summary |", "|---|---|---|---|---|---|---|"]
    for r in result["rows"]:
        b = r["bug"]
        conf = r.get("confidence") or ("" if r["class"] in ("missed", "not_reached") else "-")
        lines.append(f"| {b['id']} | {b['tier']} | {'yes' if b.get('trap') else ''} | {b['route']} "
                     f"| **{r['class']}** | {conf} | {b['summary']} |")
    lines += ["", "## Linked verdicts", ""]
    for r in result["rows"]:
        if r["evidence"]:
            lines.append(f"**{r['bug']['id']} ({r['class']})**")
            for v in r["evidence"][:3]:
                lines.append(f"- `{v['check_id']}` {v['verdict']} on {v['route']}: {v['reason'][:220]}")
            lines.append("")
    if result["unlinked_fails"]:
        lines += ["## FAIL verdicts not linked to a planted bug", "",
                  "Either a real extra defect, a linking miss, or a false fail. A human decides.", ""]
        for v in result["unlinked_fails"][:30]:
            lines.append(f"- `{v['check_id']}` on {v['route']}: {v['reason'][:220]}")
    if result["clean_fails"]:
        lines += ["", "## FAIL verdicts on the clean build", ""]
        for v in result["clean_fails"][:30]:
            lines.append(f"- `{v['check_id']}` on {v['route']}: {v['reason'][:220]}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True, help="results JSON of a run against --bugs all")
    ap.add_argument("--clean-results", help="results JSON of a run against --bugs none")
    ap.add_argument("--out", help="write the markdown report here (default: stdout)")
    ap.add_argument("--json", help="also write the summary as JSON here")
    args = ap.parse_args()
    result = score(args.results, args.clean_results)
    md = to_markdown(result)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
    else:
        sys.stdout.write(md)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result["summary"], f, indent=2)


if __name__ == "__main__":
    main()
