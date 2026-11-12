"""Injected-bug benchmark (failure-inversion §13 meta-loop: test the tester).

Plants known bugs in a local test page, runs the agent against it, measures
recall (did it find the planted bugs) and precision (how many false positives).
Without this you are tuning prompts blind.
"""

import json
import os
import subprocess
import sys
import time

BENCH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "bench")


PLANTED_BUGS = [
    {"id": "BUG-FORM-01", "route": "/", "check": "broken form validation (missing required attribute)",
     "inject": "document.querySelectorAll('form input').forEach(i => i.removeAttribute('required'));"},
    {"id": "BUG-IMG-01", "route": "/", "check": "one broken image (src=404)",
     "inject": "document.body.insertAdjacentHTML('beforeend', '<img src=\"/definitely-missing.png\">');"},
    {"id": "BUG-TITLE-01", "route": "/", "check": "empty page title",
     "inject": "document.title = '';"},
]


def _serve_bench_page(port: int = 8765):
    """Serve a minimal HTML page we can inject bugs into."""
    html = """<!doctype html><html><head><title>Bench App</title></head><body>
    <h1>Bench</h1><form id="f"><input id="email" type="email"><button>Go</button></form>
    <img src="/ok.png"><a href="/about">About</a></body></html>"""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(html.encode())
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run_benchmark(site_url: str, runner_cmd: list[str], bug_ids: list[str] | None = None) -> dict:
    """Run the agent against a site, plant bugs via CDP injection, measure recall/precision.

    runner_cmd: e.g. ['python', '-m', 'react_qa_agent.run_pipeline', '--site-url', site, '--max-screens', '1']
    """
    os.makedirs(BENCH_DIR, exist_ok=True)
    targets = [b for b in PLANTED_BUGS if not bug_ids or b["id"] in bug_ids]

    proc = subprocess.Popen(runner_cmd, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(6)
    # Inject bugs into the running headless Chrome (CDP on 9222)
    from playwright.sync_api import sync_playwright
    from .browser_server import CDP_URL
    from .native_browser import _find_page
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            page = _find_page(browser)
            page.goto(site_url, wait_until="domcontentloaded", timeout=15000)
            for bug in targets:
                page.evaluate(bug["inject"])
    except Exception as e:
        print(f"[bench] injection failed: {e}")

    out, _ = proc.communicate(timeout=900)
    report_text = out[-20000:]

    found = [b for b in targets if b["check"].split(" ")[0].lower() in report_text.lower()
             or b["id"].lower() in report_text.lower()]
    recall = len(found) / max(len(targets), 1)
    # Precision: every FAIL finding in the report that is NOT a planted bug
    import re
    fail_lines = [l for l in report_text.splitlines() if "verdict: FAIL" in l or "FAIL" in l and "|" in l]
    false_pos = max(0, len(fail_lines) - len(found))
    precision = len(found) / max(len(fail_lines), 1)

    result = {
        "planted": len(targets),
        "found": len(found),
        "recall": round(recall, 2),
        "precision": round(precision, 2),
        "false_positive_count": false_pos,
        "found_bug_ids": [b["id"] for b in found],
    }
    os.makedirs(BENCH_DIR, exist_ok=True)
    with open(os.path.join(BENCH_DIR, "bench_result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    return result


if __name__ == "__main__":
    port = 8765
    srv = _serve_bench_page(port)
    site = f"http://127.0.0.1:{port}"
    print(run_benchmark(site, ["python", "-u", "-m", "react_qa_agent.run_pipeline",
                               "--site-url", site, "--max-screens", "1"]))
