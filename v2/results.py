"""Export the task board: results JSON (scorer and CI) and a short markdown summary.

The results JSON uses the format that benchmark/score.py reads:
{"site_url", "screens": [{"screen_id", "route", "verdicts": [{check_id, verdict, reason, url, expected}]}]}.
PASS link results are summed into one line per page, so a page with 40 good
links does not bury its one broken link. NOT_TESTED items are listed apart,
with their reasons; they are not verdicts.
"""

import json
from collections import Counter, defaultdict


def results_dict(board, sitemap, meta: dict) -> dict:
    screens: dict[tuple, dict] = {}
    link_pass = defaultdict(int)
    not_tested = []
    for t in board.tasks():
        if t["status"] == "not_tested":
            not_tested.append({"task": t["id"], "team": t["team"], "page": t["page"], "user_state": t["user_state"],
                               "element_id": t["element_id"], "title": t["title"], "reason": t["reason"]})
            continue
        if t["status"] != "judged":
            continue
        key = (t["page"], t["user_state"])
        s = screens.setdefault(key, {"screen_id": f"{t['page']} ({t['user_state']})", "route": t["page"],
                                     "user_state": t["user_state"], "verdicts": []})
        obs = t["observed"] if isinstance(t["observed"], dict) else {}
        if t["kind"] == "basic:link" and t["verdict"] == "PASS":
            link_pass[key] += 1
            continue
        if t["kind"] == "basic:link-dup":
            continue
        s["verdicts"].append({"check_id": t["kind"].replace("basic:", "basic-") if t["kind"].startswith("basic:")
                              else t["id"], "kind": t["kind"], "task": t["id"], "team": t["team"], "verdict": t["verdict"],
                              "reason": t["reason"] or t["judge_note"] or "", "expected": t["expected"],
                              "url": obs.get("url", ""), "screenshot": obs.get("screenshot", ""),
                              "element_id": t["element_id"]})
    for key, n in link_pass.items():
        screens[key]["verdicts"].append({"check_id": "basic-links", "verdict": "PASS",
                                         "reason": f"{n} link(s) on {key[0]} open a page.",
                                         "expected": "Every link opens a page.", "url": "", "element_id": None})
    return {**meta, "site_url": sitemap.site_url, "screens": list(screens.values()), "not_tested": not_tested,
            "counts": board.counts()}


def save_results(path: str, data: dict) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, default=str)
    return path


def summary_md(data: dict, sitemap) -> str:
    c = data["counts"]
    v = Counter(x["verdict"] for s in data["screens"] for x in s["verdicts"])
    pages = sorted({p.path for p in sitemap.pages.values() if p.status})
    lines = [f"# Crawl and basic checks: {data['site_url']}", "",
             f"- Engine: {data.get('engine')}; finished in {data.get('minutes')} min; "
             f"first page checked after {data.get('first_check_s')} s",
             f"- Login: {data.get('login')}; write mode: {data.get('write_mode')}",
             f"- Map: {len(pages)} page paths, {len(sitemap.elements)} interactive elements, "
             f"{len(sitemap.edges)} links between pages",
             f"- Results (site-wide issues once, link passes summed per page): {v.get('PASS', 0)} PASS, {v.get('FAIL', 0)} FAIL, {v.get('BLOCKED', 0)} BLOCKED, "
             f"{v.get('UNCERTAIN', 0)} UNCERTAIN; {c.get('not_tested', 0)} NOT TESTED",
             f"- Checker rounds: {data.get('checker_rounds')}; testers: {data.get('testers', 0)}"]
    for team, u in (data.get("model_usage") or {}).items():
        lines.append(f"- Model use, {team}: {u['model']}, {u['calls']} calls, {u['prompt_tokens']} prompt + "
                     f"{u['completion_tokens']} completion tokens, {u['seconds']} s")
    lines.append("")
    fails = [(s["route"], s["user_state"], x) for s in data["screens"] for x in s["verdicts"] if x["verdict"] == "FAIL"]
    lines += ["## FAIL", ""] + ([f"- `{x['check_id']}` on {r} ({u}): {x['reason']}" for r, u, x in fails] or ["- none"])
    blocked = [(s["route"], s["user_state"], x) for s in data["screens"] for x in s["verdicts"] if x["verdict"] == "BLOCKED"]
    lines += ["", "## BLOCKED", ""] + ([f"- {r} ({u}): {x['reason']}" for r, u, x in blocked] or ["- none"])
    why = Counter(t["reason"] for t in data["not_tested"])
    lines += ["", "## NOT TESTED", ""] + ([f"- {n} item(s): {r}" for r, n in why.most_common()] or ["- none"])
    if sitemap.notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in sitemap.notes]
    return "\n".join(lines) + "\n"
