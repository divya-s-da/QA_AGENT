"""The human report: one question at the top - is it safe to demo?

Code builds every section from the task board; the model writes only a short summary, and code checks
that every number in it is a real count (else a plain sentence is used). Sections, top to bottom:
1 verdict line, 2 critical bugs (steps, screenshot, page), 3 not tested and why, 4 needs a human look,
5 guessed demo path, 6 coverage by page and team, 7 full detail.
Outputs: report.html and report.docx for people, report.txt for an AI that fixes bugs, report.json.
manual.html (on request) turns passed journeys into step-by-step guides; no extra test run.
"""

import html
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime

from . import llm

VISIBLE_BASIC = ("basic:load", "basic:resources", "basic:link", "design:rules")
BASIC_TITLES = {"basic:load": "Page does not load", "basic:errors": "JavaScript error", "basic:resources": "Broken file",
                "basic:title": "Missing page title", "basic:link": "Broken link", "basic:labels": "Form field without a label",
                "basic:headings": "Heading levels skip", "basic:overflow": "Page scrolls sideways"}
TEAM_NAMES = {"code": "basic checks (code)", "function": "function testers", "journey": "journey testers",
              "uiux": "UI and UX team", "checker": "checker"}


def _obs(t):
    return t["observed"] if isinstance(t.get("observed"), dict) else {}


def title(t) -> str:
    if t["kind"] in BASIC_TITLES:
        return f"{BASIC_TITLES[t['kind']]} on {t['page']}" + (" (logged in)" if t["user_state"] == "user" else "")
    return t["title"]


def _reason(t):
    return (t.get("reason") or t.get("judge_note") or "").strip()


def collect(board, sitemap, meta: dict, site_memory=None) -> dict:
    plan = board.get_meta("plan", {}) or meta.get("plan") or {}
    demo_pages = {p for j in plan.get("demo_journeys", []) for p in j.get("pages", [])} or {"/"}
    tasks = board.tasks()
    judged = [t for t in tasks if t["status"] == "judged" and t["kind"] != "basic:link-dup"]
    fails = [t for t in judged if t["verdict"] == "FAIL"]

    def critical(t):
        # what a client would see in the demo: a tester or journey failure on the demo path or a high-risk
        # feature, or a page / file / link that breaks on the demo path. Console errors and accessibility
        # findings on the demo path are real but not visible in a demo: they are "other failures".
        if t["team"] == "journey" or (t["risk"] >= 3 and t["team"] != "code"):
            return True
        if t["page"] not in demo_pages:
            return t["risk"] >= 3 and t["kind"] in ("basic:load", "design:rules")
        return t["team"] != "code" or t["kind"] in VISIBLE_BASIC
    crit = [t for t in fails if critical(t)]
    other = [t for t in fails if not critical(t)]
    human = [t for t in judged if t["verdict"] == "UNCERTAIN"]
    blocked = [t for t in judged if t["verdict"] == "BLOCKED"]
    nt = [t for t in tasks if t["status"] == "not_tested"]
    nt_demo = [t for t in nt if t["page"] in demo_pages and t["risk"] >= 2]
    if crit:
        verdict = {"key": "not_safe", "line": "NOT SAFE TO DEMO",
                   "why": f"{len(crit)} critical failure(s) on the demo path or on high-risk features."}
    elif other:
        pages = sorted({t["page"] for t in other})
        verdict = {"key": "avoid", "line": "SAFE TO DEMO IF YOU AVOID " + ", ".join(pages),
                   "why": f"{len(other)} failure(s), none on the demo path; keep the demo away from these pages."}
    elif nt_demo:
        verdict = {"key": "not_confirmed", "line": "NOT CONFIRMED",
                   "why": f"No failure found, but {len(nt_demo)} demo-path item(s) were not tested. Nothing is green without proof."}
    else:
        verdict = {"key": "safe", "line": "SAFE TO DEMO", "why": "No failure on the tested pages; the demo path was tested."}
    coverage = defaultdict(lambda: Counter())
    for t in tasks:
        k = t["verdict"] if t["status"] == "judged" else t["status"].upper()
        coverage[(t["page"], t["team"])][k] += 1
    new_count = recurring_count = 0
    if site_memory is not None:      # tag findings new vs seen-before, and update the store (once per run)
        s = site_memory.classify(crit + other + human)
        new_count, recurring_count = s["new"], s["recurring"]
    return {"meta": meta, "plan": plan, "verdict": verdict, "critical": crit, "other_fails": other, "human": human,
            "blocked": blocked, "not_tested": nt, "judged": judged, "coverage": coverage, "demo_pages": sorted(demo_pages),
            "counts": Counter(t["verdict"] for t in judged), "site": sitemap.site_url,
            "memory_on": site_memory is not None, "new_count": new_count, "recurring_count": recurring_count,
            "pages": sorted({p.path for p in sitemap.pages.values() if p.status and p.status < 400})}


def repro(t) -> list[str]:
    """Steps to reproduce: from the tester's recorded actions, else the task's own steps."""
    acts = _obs(t).get("actions") or []
    out = [f"Open {t['page']}" + (" while logged in as the test user" if t["user_state"] == "user" else "")]
    for a in acts[:14]:
        tool, name, inp = a.get("tool"), a.get("name") or a.get("element") or "", a.get("input")
        if tool == "fill":
            out.append(f"Type '{inp}' into '{name}'")
        elif tool == "click":
            out.append(f"Click '{name}'")
        elif tool == "select":
            out.append(f"Choose '{inp}' in '{name}'")
        elif tool in ("goto",):
            out.append(f"Open {inp}")
        elif tool in ("back", "reload", "press"):
            out.append({"back": "Press Back", "reload": "Reload the page", "press": f"Press {inp}"}[tool])
    if len(out) == 1:
        out += [str(s) for s in (t.get("steps") or [])[:6] if not str(s).lower().startswith("open ")]
    return out


def ai_summary(r: dict, chat=None) -> str:
    """A short summary by the reporter model. Numbers in it must be real counts, else a code sentence."""
    c = r["counts"]
    facts = {"verdict": r["verdict"]["line"], "critical_failures": len(r["critical"]), "other_failures": len(r["other_fails"]),
             "needs_human": len(r["human"]), "blocked": len(r["blocked"]), "not_tested": len(r["not_tested"]),
             "passed": c.get("PASS", 0), "pages": len(r["pages"]),
             "critical": [f"{t['page']}: {t['title']}" for t in r["critical"][:8]],
             "product": r["plan"].get("product_type", "")}
    plain = (f"{facts['verdict']}. {facts['critical_failures']} critical failure(s), {facts['other_failures']} other "
             f"failure(s), {facts['needs_human']} result(s) need a human look, {facts['not_tested']} item(s) not tested.")
    if chat is False or not (chat or llm.available()):      # False: a run without models
        return plain
    try:
        msg = (chat or llm.chat)("reporter", [
            {"role": "system", "content": "Write 2-4 plain sentences for a busy manager before a client demo. Use only "
             "the facts given. Do not add or drop any result. No lists, no markdown."},
            {"role": "user", "content": json.dumps(facts)}], None, max_tokens=1500)
        text = re.sub(r"\s+", " ", (msg.content or "")).strip()
    except Exception:
        return plain
    allowed = {str(v) for v in facts.values() if isinstance(v, int)}
    nums = set(re.findall(r"\b\d+\b", text))
    if not text or not nums <= (allowed | {"0"}):       # a number that is not a real count: use the code sentence
        return plain
    return text[:900]


# ---------------- writers ----------------
CSS = """
body{font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;color:#1d1a16;background:#f6f3ec;margin:0}
main{max-width:980px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:15px;letter-spacing:.08em;text-transform:uppercase;margin:34px 0 10px;
border-bottom:2px solid #1d1a16;padding-bottom:4px} .muted{color:#6b6458;font-size:13px}
.verdict{border:2px solid #1d1a16;border-radius:14px;padding:16px 20px;margin:18px 0;box-shadow:4px 4px 0 #1d1a16}
.verdict b{font-size:24px;letter-spacing:.02em}.not_safe{background:#fde2de}.avoid{background:#fff1cf}
.not_confirmed{background:#e8e6f6}.safe{background:#dff3e3}
.card{background:#fffdf8;border:1.5px solid #1d1a16;border-radius:10px;padding:12px 16px;margin:12px 0}
.card h3{margin:0 0 6px;font-size:15px}.card img{max-width:100%;max-height:380px;object-fit:cover;object-position:top;
border:1px solid #ccc;border-radius:6px;margin-top:8px;display:block}
table{border-collapse:collapse;width:100%;font-size:13.5px;background:#fffdf8}
td,th{border:1px solid #d8d2c4;padding:5px 7px;text-align:left;vertical-align:top} th{background:#efe9d9}
.v{font-weight:700;font-size:12px;border-radius:6px;padding:1px 6px;color:#fff}
.PASS{background:#2e9a4b}.FAIL{background:#d23a2f}.BLOCKED{background:#6f8299}.UNCERTAIN{background:#d48a12}
.NOT_TESTED{background:#a08060}.OPEN{background:#999}.CLAIMED{background:#999}.DONE{background:#999}
ol{margin:6px 0 0 18px;padding:0} code{background:#efe9d9;border-radius:4px;padding:0 3px;font-size:12.5px}
"""


def _rel(path, out_dir):
    try:
        return os.path.relpath(path, out_dir) if path else ""
    except ValueError:
        return path


def _new_tag(r: dict, t: dict) -> str:
    if not r.get("memory_on"):
        return ""
    if t.get("is_new"):
        return (" <span style='background:#1d7a3f;color:#fff;border-radius:5px;padding:1px 7px;"
                "font-size:12px;vertical-align:1px'>NEW</span>")
    return (" <span style='background:#e7ece9;color:#51635b;border-radius:5px;padding:1px 7px;"
            "font-size:12px;vertical-align:1px'>seen before</span>")


def _badge(v):
    v = v.replace(" ", "_")
    return f'<span class="v {html.escape(v)}">{html.escape(v.replace("_", " "))}</span>'


def write_html(r: dict, out_dir: str, summary: str) -> str:
    e = html.escape
    v = r["verdict"]
    m = r["meta"]
    parts = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
             f"<title>QA report: {e(r['site'])}</title><style>{CSS}</style></head><body><main>",
             f"<h1>QA report: {e(r['site'])}</h1><div class=muted>{e(m.get('finished_at', ''))} · "
             f"{e(r['plan'].get('product_type', ''))} · run time {e(str(m.get('minutes', '')))} min · "
             f"write mode {e(str(m.get('write_mode', '')))}</div>",
             f"<div class='verdict {v['key']}'><b>{e(v['line'])}</b><div>{e(v['why'])}</div><p>{e(summary)}</p></div>"]
    if r.get("memory_on"):
        parts.append(f"<div class=muted style='margin:-6px 0 12px'>Since the last run: "
                     f"<b style='color:#1d7a3f'>{r['new_count']} new</b> finding(s), {r['recurring_count']} seen before. "
                     f"New findings are listed first and marked <b style='color:#1d7a3f'>NEW</b>.</div>")
    crit_s = sorted(r["critical"], key=lambda t: not t.get("is_new", False))
    other_s = sorted(r["other_fails"], key=lambda t: not t.get("is_new", False))
    parts.append("<h2>1. Critical bugs</h2>" + ("" if crit_s else "<p>None.</p>"))
    first_other = other_s[0]["id"] if other_s else None
    for t in crit_s + other_s:
        if t["id"] == first_other:
            parts.append("<h2>Other failures (not on the demo path)</h2>")
        o = _obs(t)
        img = _rel(o.get("screenshot", ""), out_dir)
        parts.append(f"<div class=card><h3>{_badge('FAIL')} {e(title(t))}{_new_tag(r, t)}</h3><div class=muted>page <code>{e(t['page'])}</code> · "
                     f"{e(TEAM_NAMES.get(t['team'], t['team']))} · task {e(t['id'])}</div>"
                     f"<p><b>Expected:</b> {e(t['expected'] or '-')}</p><p><b>What happened:</b> {e(_reason(t)[:900])}</p>"
                     f"<b>Steps to reproduce</b><ol>{''.join(f'<li>{e(s)}</li>' for s in repro(t))}</ol>"
                     + (f"<a href='{e(img)}' target=_blank><img src='{e(img)}' alt='screenshot (click to open)'></a>" if img else "") + "</div>")
    parts.append("<h2>2. Not tested, and why</h2>")
    why = Counter(t["reason"] or "-" for t in r["not_tested"])
    parts.append("<p>None.</p>" if not why else "<table><tr><th>Items</th><th>Reason</th><th>Examples</th></tr>" + "".join(
        f"<tr><td>{n}</td><td>{e(reason)}</td><td>{e('; '.join(t['title'][:60] for t in r['not_tested'] if (t['reason'] or '-') == reason)[:300])}</td></tr>"
        for reason, n in why.most_common()) + "</table>")
    parts.append("<h2>3. Needs a human look</h2>" + ("<p>None.</p>" if not r["human"] else "".join(
        f"<div class=card><h3>{_badge('UNCERTAIN')} {e(title(t))}{_new_tag(r, t)}</h3><div class=muted>page <code>{e(t['page'])}</code></div>"
        f"<p>{e(_reason(t)[:700])}</p></div>" for t in r["human"])))
    parts.append("<h2>4. Guessed demo path</h2><p class=muted>The planner guessed these journeys from the menus, buttons"
                 + (" and the product documents" if r["plan"].get("from_docs") else "") + ". Correct them if the demo is different.</p>")
    js = r["plan"].get("demo_journeys", [])
    parts.append("<p>No plan (no model).</p>" if not js else "<ol>" + "".join(
        f"<li><b>{e(j['name'])}</b>: {e(' > '.join(j['pages']))}<br><span class=muted>{e('; '.join(j.get('steps', []))[:300])}</span></li>"
        for j in js) + "</ol>")
    parts.append("<h2>5. Coverage by page and team</h2><table><tr><th>Page</th><th>Team</th><th>Results</th></tr>" + "".join(
        f"<tr><td><code>{e(p)}</code></td><td>{e(TEAM_NAMES.get(team, team))}</td><td>{' '.join(_badge(k) + f' {n}' for k, n in c.most_common())}</td></tr>"
        for (p, team), c in sorted(r["coverage"].items())) + "</table>")
    parts.append("<h2>6. Full detail</h2><table><tr><th>Result</th><th>Page</th><th>Task</th><th>What happened</th></tr>" + "".join(
        f"<tr><td>{_badge(t['verdict'])}</td><td><code>{e(t['page'])}</code></td><td>{e(title(t)[:90])}</td><td>{e(_reason(t)[:400])}</td></tr>"
        for t in sorted(r["judged"], key=lambda t: ({'FAIL': 0, 'UNCERTAIN': 1, 'BLOCKED': 2, 'PASS': 3}.get(t['verdict'], 4), t['page']))) + "</table>")
    parts.append("</main></body></html>")
    path = os.path.join(out_dir, "report.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return path


def write_pdf(out_dir: str, html_path: str) -> str | None:
    """Print the HTML report to report.pdf with headless Chromium (Playwright). Best effort: if no browser is
    available, return None and keep the other files."""
    try:
        from .crawler import _default_browsers_path
        _default_browsers_path()
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    pdf = os.path.join(out_dir, "report.pdf")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                page = browser.new_page()
                page.goto("file://" + os.path.abspath(html_path), wait_until="load", timeout=20000)
                page.pdf(path=pdf, format="A4", print_background=True,
                         margin={"top": "12mm", "bottom": "14mm", "left": "10mm", "right": "10mm"})
            finally:
                browser.close()
        return pdf
    except Exception:
        return None


def write_txt(r: dict, out_dir: str, summary: str) -> str:
    lines = [f"QA REPORT {r['site']}", f"VERDICT: {r['verdict']['line']} - {r['verdict']['why']}", f"SUMMARY: {summary}"]
    if r.get("memory_on"):
        lines.append(f"SINCE LAST RUN: {r['new_count']} new, {r['recurring_count']} seen before (NEW ones marked and listed first)")
    lines.append("")
    findings = sorted(r["critical"] + r["other_fails"] + r["human"], key=lambda t: not t.get("is_new", False))
    for t in findings:
        o = _obs(t)
        new = " [NEW]" if r.get("memory_on") and t.get("is_new") else ""
        lines += [f"[{t['verdict']}]{new} {t['id']} {t['page']} ({t['user_state']}) - {title(t)}",
                  f"  expected: {t['expected']}", f"  observed: {_reason(t)[:1200]}",
                  "  steps: " + " | ".join(repro(t)),
                  "  server: " + "; ".join(f"{s.get('method', 'GET')} {s.get('path', s.get('url', ''))} -> {s.get('status')}"
                                           for s in (o.get("server_responses") or [])[-10:]),
                  f"  screenshot: {o.get('screenshot', '')}", ""]
    lines.append("NOT TESTED:")
    lines += [f"  {t['page']} - {t['title']}: {t['reason']}" for t in r["not_tested"][:300]]
    path = os.path.join(out_dir, "report.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def write_docx(r: dict, out_dir: str, summary: str) -> str | None:
    try:
        from docx import Document
        from docx.shared import Inches, Pt
    except ImportError:
        return None
    d = Document()
    d.styles["Normal"].font.size = Pt(10)
    d.add_heading(f"QA report: {r['site']}", 0)
    p = d.add_paragraph()
    p.add_run(r["verdict"]["line"]).bold = True
    d.add_paragraph(r["verdict"]["why"])
    d.add_paragraph(summary)
    d.add_heading("1. Critical bugs", 1)
    if not r["critical"]:
        d.add_paragraph("None.")
    for t in r["critical"] + r["other_fails"]:
        d.add_heading(f"FAIL: {title(t)[:120]}", 2)
        d.add_paragraph(f"Page {t['page']} - {TEAM_NAMES.get(t['team'], t['team'])} - task {t['id']}")
        d.add_paragraph(f"Expected: {t['expected'] or '-'}")
        d.add_paragraph(f"What happened: {_reason(t)[:900]}")
        for s in repro(t):
            d.add_paragraph(s, style="List Number")
        shot = _obs(t).get("screenshot")
        if shot and os.path.isfile(shot):
            try:
                d.add_picture(shot, width=Inches(5.5))
            except Exception:
                pass
    d.add_heading("2. Not tested, and why", 1)
    why = Counter(t["reason"] or "-" for t in r["not_tested"])
    for reason, n in why.most_common() or [("None.", 0)]:
        d.add_paragraph(f"{n} item(s): {reason}" if n else reason, style="List Bullet")
    d.add_heading("3. Needs a human look", 1)
    for t in r["human"] or []:
        d.add_paragraph(f"{t['page']} - {t['title']}: {_reason(t)[:500]}", style="List Bullet")
    if not r["human"]:
        d.add_paragraph("None.")
    d.add_heading("4. Guessed demo path", 1)
    for j in r["plan"].get("demo_journeys", []) or []:
        d.add_paragraph(f"{j['name']}: {' > '.join(j['pages'])}", style="List Number")
    d.add_heading("5. Coverage by page and team", 1)
    tbl = d.add_table(rows=1, cols=3)
    tbl.style = "Light Grid Accent 1"
    for i, h in enumerate(("Page", "Team", "Results")):
        tbl.rows[0].cells[i].text = h
    for (page, team), c in sorted(r["coverage"].items()):
        row = tbl.add_row().cells
        row[0].text, row[1].text, row[2].text = page, TEAM_NAMES.get(team, team), ", ".join(f"{k} {n}" for k, n in c.most_common())
    path = os.path.join(out_dir, "report.docx")
    d.save(path)
    return path


def write_manual(r: dict, out_dir: str) -> str | None:
    """User manual on request: the passed journeys as step-by-step guides, with their screenshots."""
    e = html.escape
    js = [t for t in r["judged"] if t["team"] == "journey" and t["verdict"] == "PASS"]
    parts = [f"<!doctype html><html lang=en><head><meta charset=utf-8><title>User guide: {e(r['site'])}</title>"
             f"<style>{CSS}</style></head><body><main><h1>User guide: {e(r['site'])}</h1>"
             "<p class=muted>Made from the journeys the agent completed and proved. No extra test run.</p>"]
    if not js:
        parts.append("<p>No journey passed in this run, so there is no guide yet.</p>")
    for t in js:
        img = _rel(_obs(t).get("screenshot", ""), out_dir)
        parts.append(f"<div class=card><h3>{e(t['title'].replace('Journey: ', ''))}</h3><ol>"
                     + "".join(f"<li>{e(s)}</li>" for s in repro(t)) + "</ol>"
                     + (f"<img src='{e(img)}' alt=''>" if img else "") + "</div>")
    parts.append("</main></body></html>")
    path = os.path.join(out_dir, "manual.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return path


def build(board, sitemap, meta: dict, out_dir: str, manual: bool = False, chat=None, site_memory=None,
          pdf: bool = True) -> dict:
    r = collect(board, sitemap, meta, site_memory=site_memory)
    summary = ai_summary(r, chat)
    files = {"html": write_html(r, out_dir, summary), "txt": write_txt(r, out_dir, summary)}
    if pdf:                                 # skipped on a stop: a browser may be gone and the 8 s window is tight
        p = write_pdf(out_dir, files["html"])
        if p:
            files["pdf"] = p
    docx = write_docx(r, out_dir, summary)
    if docx:
        files["docx"] = docx
    if manual:
        files["manual"] = write_manual(r, out_dir)
    data = {"verdict": r["verdict"], "summary": summary, "critical": [t["id"] for t in r["critical"]],
            "other_failures": [t["id"] for t in r["other_fails"]], "needs_human": [t["id"] for t in r["human"]],
            "not_tested": len(r["not_tested"]), "demo_journeys": r["plan"].get("demo_journeys", []),
            "memory_on": r.get("memory_on", False), "new_count": r.get("new_count", 0),
            "recurring_count": r.get("recurring_count", 0),
            "new_findings": [t["id"] for t in (r["critical"] + r["other_fails"] + r["human"]) if t.get("is_new")],
            "counts": dict(r["counts"]), "files": files, "made_at": datetime.now().isoformat(timespec="seconds")}
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, default=str)
    files["json"] = os.path.join(out_dir, "report.json")
    return data
