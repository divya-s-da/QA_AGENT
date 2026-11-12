"""DOCX report generator tool.

Converts the QA report text into a professionally formatted .docx file.
Supports embedding screenshots from the testing run.
"""

import os
import json
from datetime import datetime

from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

from .screenshot_tool import evidence_dir

# Verdict presentation. FAIL is shown as BROKEN (plainer for a reader skimming
# the report) and every verdict gets a colour so a reader can scan a table
# without reading each row.
_PASS = RGBColor(0x1E, 0x7A, 0x3C)
_FAIL = RGBColor(0xC0, 0x39, 0x2B)
_BLOCKED = RGBColor(0xB7, 0x79, 0x0B)
_MUTED = RGBColor(0x6B, 0x6B, 0x6B)
_HEADER_BG = "D9E2F3"

# The report agent emits these four fixed labels (prompts/report.py). Rendering
# them as real headings makes the narrative navigable in Word's outline pane
# instead of four indistinguishable paragraphs.
_NARRATIVE_SECTIONS = {
    "BLOCKERS": "Blockers",
    "TOP_BREAKAGES": "Top breakages",
    "OBSERVATIONS_NEEDING_CONFIRMATION": "Observations needing confirmation",
    "FIX_THIS_FIRST": "Fix this first",
}


def _norm(verdict):
    return str(verdict or "").strip().upper()


def _verdict_label(verdict):
    v = _norm(verdict)
    return "BROKEN" if v == "FAIL" else (v or "?")


def _verdict_color(verdict):
    v = _norm(verdict)
    if v == "PASS":
        return _PASS
    if v == "FAIL":
        return _FAIL
    if v == "BLOCKED":
        return _BLOCKED
    return _MUTED


def _shade(cell, hex_fill):
    """Solid background fill for a table cell (python-docx has no API for this)."""
    el = OxmlElement("w:shd")
    el.set(qn("w:val"), "clear")
    el.set(qn("w:fill"), hex_fill)
    cell._tc.get_or_add_tcPr().append(el)


def _repeat_header(row):
    """Mark a row as a header so it repeats across page breaks."""
    el = OxmlElement("w:tblHeader")
    el.set(qn("w:val"), "true")
    row._tr.get_or_add_trPr().append(el)


def _set_cell(cell, text, bold=False, color=None, size=9):
    cell.text = ""
    run = cell.paragraphs[0].add_run(str(text))
    run.bold = bold
    run.font.size = Pt(size)
    if color is not None:
        run.font.color.rgb = color


def _new_table(doc, headers, widths=None):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(headers):
        _set_cell(table.rows[0].cells[i], h, bold=True)
        _shade(table.rows[0].cells[i], _HEADER_BG)
    _repeat_header(table.rows[0])
    if widths:
        _apply_widths(table, widths)
    return table


def _apply_widths(table, widths):
    """python-docx needs the width set on every cell, not just the column."""
    for row in table.rows:
        for cell, w in zip(row.cells, widths):
            cell.width = Inches(w)


def _tally(screen_results):
    """Count verdicts across all screens. Purely derived -- never invented."""
    passed = failed = blocked = other = 0
    per_screen = []
    for r in screen_results:
        if not isinstance(r, dict):
            continue
        p = f = b = 0
        for v in r.get("verdicts", []) or []:
            if not isinstance(v, dict):
                continue
            verdict = _norm(v.get("verdict"))
            if verdict == "PASS":
                p += 1
            elif verdict == "FAIL":
                f += 1
            elif verdict == "BLOCKED":
                b += 1
            else:
                other += 1
        passed += p
        failed += f
        blocked += b
        per_screen.append({
            "screen_id": r.get("screen_id", "?"),
            "route": r.get("route", ""),
            "overall": r.get("overall", "?"),
            "passed": p, "failed": f, "blocked": b,
        })
    return {
        "passed": passed, "failed": failed, "blocked": blocked, "other": other,
        "total": passed + failed + blocked + other,
        "screens": per_screen,
    }


def _add_executive_summary(doc, stats, site_url):
    """Scorecard + per-screen roll-up.

    This is the part a reader looks at first, so it goes before the narrative
    and is built only from recorded verdicts.
    """
    doc.add_heading("Executive summary", level=1)

    if stats["total"] == 0:
        doc.add_paragraph("No check results were recorded for this run.")
        return

    testable = stats["passed"] + stats["failed"]
    rate = f"{round(100 * stats['passed'] / testable)}%" if testable else "n/a"

    headline = doc.add_paragraph()
    run = headline.add_run(
        f"{stats['failed']} broken, {stats['blocked']} blocked, "
        f"{stats['passed']} passed across {len(stats['screens'])} screen(s)."
    )
    run.bold = True
    run.font.size = Pt(12)
    run.font.color.rgb = _FAIL if stats["failed"] else (_BLOCKED if stats["blocked"] else _PASS)

    rows = [
        ("Screens tested", str(len(stats["screens"])), None),
        ("Checks run", str(stats["total"]), None),
        ("Passed", str(stats["passed"]), _PASS),
        ("Broken", str(stats["failed"]), _FAIL),
        ("Blocked (could not be tested)", str(stats["blocked"]), _BLOCKED),
        ("Pass rate (of testable checks)", rate, None),
    ]
    table = _new_table(doc, ["Metric", "Value"], widths=[3.6, 1.6])
    for label, value, color in rows:
        cells = table.add_row().cells
        _set_cell(cells[0], label)
        _set_cell(cells[1], value, bold=True, color=color)
    _apply_widths(table, [3.6, 1.6])

    doc.add_paragraph()
    doc.add_heading("Results by screen", level=2)
    widths = [1.5, 1.9, 0.8, 0.8, 0.8, 1.0]
    table = _new_table(doc, ["Screen", "Route", "Passed", "Broken", "Blocked", "Overall"], widths)
    for s in stats["screens"]:
        cells = table.add_row().cells
        _set_cell(cells[0], s["screen_id"], bold=True)
        _set_cell(cells[1], s["route"] or "-")
        _set_cell(cells[2], s["passed"], color=_PASS if s["passed"] else _MUTED)
        _set_cell(cells[3], s["failed"], color=_FAIL if s["failed"] else _MUTED)
        _set_cell(cells[4], s["blocked"], color=_BLOCKED if s["blocked"] else _MUTED)
        _set_cell(cells[5], _verdict_label(s["overall"]), bold=True,
                  color=_verdict_color(s["overall"]))
    _apply_widths(table, widths)


def generate_docx_report(report_text, all_screen_results_json="[]", site_url="", screenshots_dir=None, output_dir=None):
    """Generate a formatted .docx file from report text with embedded screenshots.

    Args:
        report_text: The full markdown/text report from the report agent.
        all_screen_results_json: JSON string of all screen results with verdicts.
        site_url: The tested site URL.
        screenshots_dir: Directory containing screenshot PNGs (defaults to the current run/session evidence folder).
        output_dir: Directory to save the .docx file (defaults to pipeline root).

    Returns:
        str: Path to the generated .docx file.
    """
    if screenshots_dir is None:
        screenshots_dir = evidence_dir()
    if output_dir is None:
        output_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    try:
        screen_results = json.loads(all_screen_results_json) if all_screen_results_json else []
    except (json.JSONDecodeError, TypeError):
        screen_results = []
    # json.loads("null") -> None, and a malformed run could hand back a dict or
    # a bare number. Normalise once here so _tally and _add_results_section can
    # both assume a list.
    if not isinstance(screen_results, list):
        screen_results = []

    doc = Document()
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Calibri"
    font.size = Pt(11)

    title = doc.add_heading("React QA Test Report", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    timestamp_paragraph = doc.add_paragraph()
    timestamp_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = timestamp_paragraph.add_run(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(128, 128, 128)

    if site_url:
        url_paragraph = doc.add_paragraph()
        url_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        url_run = url_paragraph.add_run(f"Site: {site_url}")
        url_run.font.size = Pt(10)
        url_run.font.color.rgb = RGBColor(100, 100, 100)

    stats = _tally(screen_results)
    _add_executive_summary(doc, stats, site_url)

    doc.add_page_break()
    doc.add_heading("Analysis", level=1)

    lines = report_text.split("\n")
    in_code_block = False
    code_lines = []

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code_block:
                if code_lines:
                    code_text = "\n".join(code_lines)
                    p = doc.add_paragraph()
                    run = p.add_run(code_text)
                    run.font.name = "Consolas"
                    run.font.size = Pt(9)
                code_lines = []
            in_code_block = not in_code_block
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        if not stripped:
            continue

        # The report agent emits fixed ALL-CAPS labels; promote them to
        # headings so the narrative is navigable. Handles both a bare
        # "BLOCKERS:" line and an inline "TOP_BREAKAGES: none found."
        label, _, remainder = stripped.partition(":")
        if label in _NARRATIVE_SECTIONS:
            doc.add_heading(_NARRATIVE_SECTIONS[label], level=2)
            if remainder.strip():
                doc.add_paragraph(_clean_markdown(remainder.strip()))
            continue

        if stripped.startswith("# "):
            doc.add_heading(stripped[2:].strip(), level=1)
        elif stripped.startswith("## "):
            doc.add_heading(stripped[3:].strip(), level=2)
        elif stripped.startswith("### "):
            doc.add_heading(stripped[4:].strip(), level=3)
        elif stripped in ("---", "***", "___"):
            doc.add_paragraph("_" * 50)
        elif stripped.startswith("- ") or stripped.startswith("* "):
            text = stripped[2:].strip()
            text = _clean_markdown(text)
            doc.add_paragraph(text, style="List Bullet")
        elif len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in (".", ")"):
            text = stripped[2:].strip()
            text = _clean_markdown(text)
            doc.add_paragraph(text, style="List Number")
        elif "|" in stripped and stripped.startswith("|"):
            text = _clean_markdown(stripped)
            text = text.replace("|", "  •  ")
            p = doc.add_paragraph(text)
            for run in p.runs:
                run.font.size = Pt(9)
        else:
            text = _clean_markdown(stripped)
            p = doc.add_paragraph(text)
            _apply_inline_formatting(p, stripped)

    doc.add_page_break()
    doc.add_heading("Detailed results", level=1)
    _add_results_section(doc, screen_results)
    doc.add_page_break()
    doc.add_heading("Screenshots", level=1)
    _add_screenshots_section(doc, screen_results, screenshots_dir)

    os.makedirs(output_dir, exist_ok=True)
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"qa_report_{timestamp_str}.docx"
    filepath = os.path.join(output_dir, filename)
    doc.save(filepath)

    return filepath


def _add_results_section(doc, screen_results):
    """Per-screen verdict table — always built from the results JSON (deterministic)."""
    if not screen_results:
        doc.add_paragraph("No check results recorded for any screen.")
        return
    for r in screen_results:
        if not isinstance(r, dict):
            continue
        sid = r.get("screen_id", "?")
        overall = r.get("overall", "?")
        route = r.get("route", "")
        heading = doc.add_heading(f"{sid}", level=2)
        for run in heading.runs:
            run.font.color.rgb = _verdict_color(overall)

        meta = doc.add_paragraph()
        meta_run = meta.add_run(
            f"Route: {route or '-'}    |    Overall: {_verdict_label(overall)}"
        )
        meta_run.font.size = Pt(9)
        meta_run.font.color.rgb = _MUTED

        verdicts = r.get("verdicts", [])
        if not verdicts:
            doc.add_paragraph("No verdicts recorded for this screen.")
            continue

        # Broken first, then blocked, then passed: the reader cares about
        # failures, and they should not have to hunt for them in a long table.
        order = {"FAIL": 0, "BLOCKED": 1, "PASS": 2}
        verdicts = sorted(
            [v for v in verdicts if isinstance(v, dict)],
            key=lambda v: order.get(_norm(v.get("verdict")), 3),
        )

        widths = [1.3, 0.9, 3.1, 1.5]
        table = _new_table(doc, ["Check", "Verdict", "What was observed", "Screenshot"], widths)
        for v in verdicts:
            verdict = v.get("verdict", "?")
            row = table.add_row().cells
            _set_cell(row[0], v.get("check_id", "?"))
            _set_cell(row[1], _verdict_label(verdict), bold=True, color=_verdict_color(verdict))
            _set_cell(row[2], (v.get("reason") or "")[:400] or "-")
            _set_cell(row[3], os.path.basename(v.get("screenshot_path", "")) or "-", size=8)
        _apply_widths(table, widths)
        doc.add_paragraph()


def _add_screenshots_section(doc, screen_results, screenshots_dir):
    """Embed every screenshot from this test's evidence folder.

    The folder is isolated per run/session (evidence_dir()), so every PNG in it
    belongs to THIS test — embedding them all is safe and keeps the report rich
    even when the model only attached a few screenshot_paths to verdicts.
    """
    if not os.path.isdir(screenshots_dir):
        doc.add_paragraph("No screenshots directory found — screenshots were not saved during testing.")
        return

    # Lookup: screenshot basename -> (screen_id, verdict) so each image gets a caption
    by_path = {}
    for r in screen_results:
        if not isinstance(r, dict):
            continue
        for v in r.get("verdicts", []):
            if isinstance(v, dict) and v.get("screenshot_path"):
                by_path[os.path.basename(v["screenshot_path"])] = (r.get("screen_id", "?"), v)

    screenshot_files = sorted(
        [f for f in os.listdir(screenshots_dir) if f.endswith(".png")],
        key=lambda f: os.path.getmtime(os.path.join(screenshots_dir, f)),
    )

    if not screenshot_files:
        doc.add_paragraph("No screenshots were captured for this test.")
        return

    groups = {}
    for fname in screenshot_files:
        sid = by_path[fname][0] if fname in by_path else "session"
        groups.setdefault(sid, []).append(fname)

    TITLES = {"login": "Login", "register": "Registration"}
    for sid, files in sorted(groups.items()):
        doc.add_heading(TITLES.get(sid, f"Screen: {sid}"), level=2)

        for fname in files:
            fpath = os.path.join(screenshots_dir, fname)
            if fname in by_path:
                _, v = by_path[fname]
                label = "BROKEN" if v.get("verdict") == "FAIL" else v.get("verdict", "?")
                reason = (v.get("reason") or "").strip()[:300]
                caption = f"{v.get('check_id', '?')} — {label} — {reason}"
            else:
                caption = fname[:-4]
            p = doc.add_paragraph()
            p_run = p.add_run(caption)
            p_run.font.size = Pt(9)
            p_run.bold = True
            try:
                doc.add_picture(fpath, width=Inches(5.5))
            except Exception:
                doc.add_paragraph(f"[Could not embed: {fname}]")


def _clean_markdown(text):
    import re
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    return text


def _apply_inline_formatting(paragraph, text):
    import re
    paragraph.clear()
    parts = re.split(r"(\*\*.*?\*\*|\*.*?\*|`.*?`)", text)
    for part in parts:
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            run.bold = True
        elif part.startswith("*") and part.endswith("*"):
            run = paragraph.add_run(part[1:-1])
            run.italic = True
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            run.font.name = "Consolas"
            run.font.size = Pt(9)
        else:
            paragraph.add_run(part)

