"""Run the QA pipeline against ShopLite and score the result.

    python benchmark/run_benchmark.py --bugs all  --label baseline
    python benchmark/run_benchmark.py --bugs none --label baseline-clean
    python benchmark/run_benchmark.py --bugs all --label verify -- --verify --map-site
    python benchmark/run_benchmark.py --engine v2 --bugs all --label v2-step2

Arguments after `--` go to run_pipeline unchanged. The test account (bob, a
regular user) is passed to the pipeline as SITE_EMAIL / SITE_PASSWORD in the
environment, so the real .env is not touched.

Output goes to benchmark/results/<label>_<timestamp>.{json,log,md}.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
RESULTS = os.path.join(HERE, "results")


def wait_up(url, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bugs", default="all")
    ap.add_argument("--label", default="run")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--max-screens", type=int, default=10)
    ap.add_argument("--engine", choices=["pipeline", "v2"], default="pipeline",
                    help="pipeline = run_pipeline.py (today's agent); v2 = QA Agent v2 (react_qa_agent.v2.run)")
    ap.add_argument("pipeline_args", nargs="*", help="extra run_pipeline arguments (after --)")
    args = ap.parse_args()

    os.makedirs(RESULTS, exist_ok=True)
    stem = os.path.join(RESULTS, f"{args.label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    site = f"http://127.0.0.1:{args.port}"

    app = subprocess.Popen([sys.executable, os.path.join(HERE, "shoplite", "app.py"),
                            "--port", str(args.port), "--bugs", args.bugs])
    try:
        if not wait_up(site + "/"):
            raise SystemExit("ShopLite did not start")
        env = dict(os.environ, SITE_EMAIL="bob@shoplite.test", SITE_PASSWORD="bob-pass-123",
                   SITE_PASSWORD_FALLBACK="", PYTHONUNBUFFERED="1")
        if args.engine == "v2":      # one account per tester: carol, dave, erin, frank (bob has the seeded order)
            for i, n in enumerate(("carol", "dave", "erin", "frank"), start=2):
                env[f"SITE_EMAIL_{i}"], env[f"SITE_PASSWORD_{i}"] = f"{n}@shoplite.test", f"{n}-pass-123"
        if args.engine == "v2":
            # The ShopLite charter allows writes (destructive_actions_allowed: true), as for the pipeline.
            cmd = [sys.executable, "-u", "-m", f"{os.path.basename(REPO)}.v2.run", "--url", site, "--login",
                   "--allow-writes", "--out", stem + "_v2", "--results-json", stem + ".json", *args.pipeline_args]
        else:
            cmd = [sys.executable, "-u", "-m", f"{os.path.basename(REPO)}.run_pipeline",
                   "--site-url", site, "--charter", os.path.join(HERE, "shoplite", "charter.yaml"),
                   "--max-screens", str(args.max_screens), "--results-json", stem + ".json",
                   *args.pipeline_args]
        print("running:", " ".join(cmd), flush=True)
        started = time.monotonic()
        with open(stem + ".log", "w", encoding="utf-8") as log:
            code = subprocess.call(cmd, cwd=os.path.dirname(REPO), env=env, stdout=log, stderr=subprocess.STDOUT)
        minutes = (time.monotonic() - started) / 60
        print(f"{args.engine} exit code {code} after {minutes:.1f} min; log {stem}.log", flush=True)
    finally:
        app.terminate()
        app.wait(timeout=10)

    if not os.path.isfile(stem + ".json"):
        raise SystemExit(f"no results JSON was written; see {stem}.log")
    if args.bugs == "none":
        print(f"clean run results: {stem}.json (score it with --clean-results)")
        return
    sys.path.insert(0, HERE)
    import score
    result = score.score(stem + ".json")
    with open(stem + ".md", "w", encoding="utf-8") as f:
        f.write(score.to_markdown(result))
    print(json.dumps(result["summary"], indent=2))
    print(f"score report: {stem}.md")


if __name__ == "__main__":
    main()
