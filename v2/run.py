"""QA Agent v2: crawl, basic checks, task board, checker and function testers.

    python -m react_qa_agent.v2.run --url https://app.example [--login] [--out DIR]
                                    [--results-json FILE] [--allow-writes] [--testers N]

Step 2 (plain code): crawl, basic checks, task board, checker.
Step 3 (--testers N, default 3): N function testers work in parallel from the
board, each with its own browser and login; the checker sends gaps back at
most --rounds times; at the time limit the rest is NOT TESTED with a reason.
With --testers 0 no model is called. With --login the test account comes
from SITE_EMAIL / SITE_PASSWORD (environment or .env); the values are never printed.

Exit codes: 0 the run finished; 2 the first page did not load (the run stops
early, so CI shows red); 1 a crash.
"""

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime

try:
    from ..tools import write_guard
except ImportError:
    from tools import write_guard
from .board import TaskBoard
from .checker import Checker
from .crawler import Crawler
from .memory import SiteMemory
from .results import results_dict, save_results, summary_md
from . import llm

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(url: str, out: str, *, login: bool = False, allow_writes: bool = False, safe_writes: bool = False,
        scope_mode: str = "site", owner: str | None = None, max_pages: int = 150,
        budget_min: float = 60.0, crawl_min: float = 5.0, rounds: int = 2, headless: bool = True,
        results_json: str | None = None, testers: int = 0, chat=None, judge_chat=None, planner_chat=None,
        journey_testers: int = 1, ui_testers: int = 1, docs: str | None = None, manual: bool = False,
        files: str | None = None, report_every: float = 0,
        report_chat=None, log=print) -> tuple[int, dict]:
    t0 = time.monotonic()
    deadline = time.time() + budget_min * 60 - 90          # keep 90 s for the checker and the report
    os.makedirs(out, exist_ok=True)
    # Stop handling: when the server stops a run (SIGTERM), finish the current work and still write a report,
    # so the UI's "the report so far is kept" is true. The handler only flips a flag; the loop does the rest.
    stop_requested = threading.Event()
    try:
        signal.signal(signal.SIGTERM, lambda *_: stop_requested.set())
        signal.signal(signal.SIGINT, lambda *_: stop_requested.set())
    except (ValueError, OSError):
        pass                                               # not the main thread (e.g. under tests): skip
    # The server also drops a STOP file in the run folder before it signals. A file check does not depend on the
    # signal (some libraries replace the handler), so the loop notices a stop even if the signal is swallowed.
    stop_file = os.path.join(out, "STOP")

    def _stop():
        return stop_requested.is_set() or os.path.exists(stop_file)
    write_guard.configure(mode=("allow" if allow_writes else "safe" if safe_writes else "read-only"))
    writes_for_planning = allow_writes or safe_writes   # safe mode still plans create tasks; the guard blocks deletes
    email = password = None
    if login:
        try:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(HERE, ".env"))
        except Exception:
            pass
        email, password = os.getenv("SITE_EMAIL") or None, os.getenv("SITE_PASSWORD") or None
        if not (email and password):
            log("  --login given, but SITE_EMAIL / SITE_PASSWORD are not set: crawling logged out only")
    # one board per run: an old board in the same folder would mix two runs
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(os.path.join(out, "board.db" + ext)):
            os.remove(os.path.join(out, "board.db" + ext))
    board = TaskBoard(os.path.join(out, "board.db"))
    notes_text = _read_docs(docs, log) if docs else ""
    login_chat = planner_chat or chat or (llm.chat if llm.available() else None)
    from urllib.parse import urlparse as _urlparse
    _spath = (_urlparse(url).path or "/").rstrip("/") or "/"
    _scope = _spath if (scope_mode in ("section", "page") and _spath != "/") else None
    _page_only = scope_mode == "page"
    if scope_mode != "site":
        log(f"  scope: {scope_mode} ({_scope or _spath}) - testing only this " + ("page" if _page_only else "section"))
    crawler = Crawler(url, out, board, email=email, password=password, max_pages=max_pages,
                      budget_s=min(crawl_min, budget_min) * 60, headless=headless, log=log,
                      notes=notes_text, login_chat=login_chat, scope=_scope, page_only=_page_only)
    sitemap = crawler.run()

    first = next(iter(sitemap.pages.values()), None)
    aborted = first is None or first.status == 0 or first.status >= 500

    checker = Checker(board, sitemap, max_rounds=rounds)
    workers, judges = [], []
    plan, planner = {}, None

    design = None
    if not aborted:
        from .design import DesignChecker
        design = DesignChecker(url, board, sitemap, out, user_storage=crawler.user_storage, headless=headless,
                               log=log, budget_s=min(4.0, budget_min) * 60)
        threading.Thread(target=design.run, name="design", daemon=True).start()
        log("  design checks started (plain code, no model slot)")
    if testers > 0 and not aborted and (chat or llm.available()):
        if planner_chat or chat is None:
            from .planner import Planner
            planner = Planner(board, sitemap, chat=planner_chat, log=log, docs=notes_text,
                              have_login=bool(email and password), two_accounts=len(accounts()) >= 2,
                              allow_writes=writes_for_planning)
            threading.Thread(target=planner.run, name="planner", daemon=True).start()   # testers start meanwhile
        teams = {"function": testers, "journey": journey_testers, "uiux": ui_testers}
        file_list = sorted(os.path.join(r, f) for r, _, fs in os.walk(files) for f in fs) if files and os.path.isdir(files) \
            else ([files] if files and os.path.isfile(files) else [])
        if file_list:
            log(f"  test files for upload fields: {len(file_list)}")
        workers, stop = _start_testers(teams, board, sitemap, email, password, deadline, out, log, chat, headless,
                                       file_list, notes=notes_text)
        from .judge import Judge
        judges = [Judge(board, stop, chat=judge_chat or chat, log=log, index=i, count=2) for i in range(2)]
        for j in judges:
            j.start()
        log(f"  2 judges started, model {llm.model_for('judge')} (sees evidence, never the tester's verdict)")
        from .planner import route_follow_up
        report_every_s = report_every * 60 if report_every and report_every > 0 else 0
        next_report = time.time() + report_every_s if report_every_s else 0
        if report_every_s:
            log(f"  interim reports every {report_every:g} min -> {os.path.join(out, 'interim')}/")
        while True:
            board.expire_claims()
            if _stop():
                log("  stop requested: finishing current work and writing the report")
                break
            for t in board.follow_ups():
                tid = route_follow_up(board, t)
                log(f"  follow-up from {t['id']} -> {tid}: {t['follow_up'][:80]}")
            if next_report and time.time() >= next_report:
                _interim_report(board, sitemap, planner, out, t0, log)
                next_report += report_every_s
            if time.time() > deadline:
                log("  time limit: open work goes to NOT TESTED")
                break
            if board.idle() and (planner is None or planner.done.is_set()):
                r = checker.run_round()
                if r["added"]:
                    log(f"  checker round {r['round']}: {len(r['added'])} task(s) pinned for the gaps")
                if r["final"]:
                    if r["closed"]:
                        log(f"  checker: round limit, {r['closed']} item(s) NOT TESTED")
                    break
            if not any(w.is_alive() for w in workers):
                log("  every tester stopped")
                break
            time.sleep(1.0)
        stop.set()
        # on a stop, do not wait long for each thread: the board serializes access, so the report is safe to
        # build while a straggler winds down. (1 s x a few threads stays well inside the server's kill window.)
        for w in workers:
            w.join(timeout=1 if _stop() else 200)
        for j in judges:
            j.join(timeout=1 if _stop() else 300)
        closed = checker.finish("stopped by the user" if _stop() else
                                (f"time limit ({budget_min:g} minutes)" if time.time() > deadline else "no tester could finish it"))
    else:
        if testers > 0 and not aborted:
            log("  no model key (AIROUTER_API_KEY / AI_ROUTER_API): the testers do not start")
        r = checker.run_round()
        log(f"  checker round {r['round']}: {len(r['added'])} map item(s) have no result")
        closed = checker.finish("no tester ran (run with --testers N and a model key)") if r["added"] else 0
    if closed:
        log(f"  {closed} item(s) closed as NOT TESTED")

    if design is not None and not _stop() and not design.done.wait(timeout=max(5.0, deadline - time.time() + 60)):
        log("  design checks did not finish in time")

    meta = {"engine": "v2 crawl + basic checks" + (f" + {len(workers)} function testers" if workers else ""), "minutes": round((time.monotonic() - t0) / 60, 2),
            "first_check_s": crawler.first_check_s, "login": crawler.login_result,
            "write_mode": write_guard.mode_label(), "checker_rounds": checker.round,
            "testers": len(workers), "plan": planner.plan if planner else plan, "judged": sum(j.judged for j in judges), "model_usage": dict(llm.USAGE),
            "blocked_writes": crawler.blocked, "broken_targets": crawler.broken, "aborted": aborted,
            "writes_made": write_guard.writes_made(), "stopped": _stop(),
            "finished_at": datetime.now().isoformat(timespec="seconds") + (" (stopped by the user)" if _stop() else "")}
    try:
        with open(os.path.join(out, "writes.json"), "w", encoding="utf-8") as _wf:
            json.dump(meta["writes_made"], _wf, indent=2)
    except Exception:
        pass
    sitemap.save(os.path.join(out, "site_map.json"))
    data = results_dict(board, sitemap, meta)
    save_results(results_json or os.path.join(out, "results.json"), data)
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(summary_md(data, sitemap))
    try:
        from .report import build
        stopped = _stop()
        rep = build(board, sitemap, meta, out, manual=(manual and not stopped),
                    chat=False if stopped else ((report_chat or chat) if workers else False),
                    site_memory=SiteMemory(url, owner=owner), pdf=not stopped)
        data["report"] = rep
        save_results(results_json or os.path.join(out, "results.json"), data)
        log(f"  REPORT: {rep['verdict']['line']} - {rep['files']['html']}")
        if rep.get("memory_on"):
            log(f"  SINCE LAST RUN: {rep.get('new_count', 0)} new finding(s), {rep.get('recurring_count', 0)} seen before")
    except Exception as e:
        log(f"  report failed: {type(e).__name__}: {e}")
    board.close()
    return (2 if (aborted or _stop()) else 0), data


def _interim_report(board, sitemap, planner, out: str, t0: float, log) -> None:
    """Write a report of the board so far, then the run continues. Code only (chat=False), so it steals no
    model slot and is fast. Each report goes in its own folder so earlier ones are kept."""
    from .report import build
    mins = round((time.monotonic() - t0) / 60, 1)
    sub = os.path.join(out, "interim", f"{int(round(mins))}min")
    os.makedirs(sub, exist_ok=True)
    meta = {"plan": planner.plan if planner else {}, "minutes": mins, "write_mode": write_guard.mode_label(),
            "finished_at": datetime.now().isoformat(timespec="seconds") + f" (interim, {mins:g} min)"}
    try:
        rep = build(board, sitemap, meta, sub, manual=False, chat=False)
        c = rep.get("counts", {})
        log(f"  INTERIM REPORT at {mins:g} min: {rep['verdict']['line']} "
            f"(PASS {c.get('PASS', 0)} FAIL {c.get('FAIL', 0)} UNCERTAIN {c.get('UNCERTAIN', 0)}) -> {os.path.join(sub, 'report.html')}")
    except Exception as e:
        log(f"  interim report failed: {type(e).__name__}: {e}")


def _start_testers(teams: dict, board, sitemap, email, password, deadline, out, log, chat, headless, files=None, notes=""):
    import threading
    from .tester import Tester
    stop, lock = threading.Event(), threading.Lock()
    workers = []
    accs = accounts() or ([(email, password)] if email and password else [(None, None)])
    if len(accs) > 1:
        log(f"  {len(accs)} test accounts: each tester gets its own when there are enough")
    for team, n in teams.items():
        for i in range(max(0, n)):
            acc = accs[len(workers) % len(accs)]
            workers.append(Tester(f"{team}-{i + 1}", team, board, sitemap, lock, email=acc[0], password=acc[1],
                                  deadline=deadline, stop=stop, out_dir=out, log=log, chat=chat, headless=headless,
                                  judge_mode="judge", files=files, notes=notes))
        if n > 0:
            log(f"  {n} {team} tester(s), model {llm.model_for(team)}")
    for w in workers:
        w.start()
    return workers, stop


def accounts() -> list[tuple[str, str]]:
    """Test accounts: SITE_EMAIL/SITE_PASSWORD, then SITE_EMAIL_2/SITE_PASSWORD_2, ... One per tester when
    there are enough, so two testers never change the same account's data (cart, orders, profile)."""
    out = []
    for suffix in [""] + [f"_{i}" for i in range(2, 21)]:
        e, p = os.getenv(f"SITE_EMAIL{suffix}"), os.getenv(f"SITE_PASSWORD{suffix}")
        if e and p:
            out.append((e, p))
    return out


def _load_secrets(path: str) -> None:
    """SITE_EMAIL / SITE_PASSWORD from a KEY=VALUE file. Values go to the environment only, never to a log."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if re.match(r"^SITE_(EMAIL|PASSWORD)(_\d+)?$|^SITE_PASSWORD_FALLBACK$", k.strip()):
                    os.environ[k.strip()] = v.strip().strip('"').strip("'")


def _read_docs(path: str | None, log) -> str:
    """Product documents (optional input): a text, markdown or HTML file, or a folder of them."""
    if not path:
        return ""
    import re
    files = [path] if os.path.isfile(path) else [os.path.join(r, f) for r, _, fs in os.walk(path) for f in fs] \
        if os.path.isdir(path) else []
    out = []
    for f in files:
        if not re.search(r"\.(txt|md|html?|rst)$", f, re.I):
            continue
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                t = fh.read()
        except OSError:
            continue
        out.append(re.sub(r"<[^>]+>", " ", t) if f.lower().endswith((".html", ".htm")) else t)
    text = "\n\n".join(out)
    log(f"  product documents: {len(text)} characters from {len(out)} file(s)" if text else
        f"  product documents: nothing readable at {path}")
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", help="output folder (default: state/v2/<timestamp>)")
    ap.add_argument("--results-json", help="also write the results JSON here")
    ap.add_argument("--login", action="store_true", help="log in with SITE_EMAIL / SITE_PASSWORD and crawl again")
    ap.add_argument("--allow-writes", action="store_true", help="switch off the write guard (test environment only)")
    ap.add_argument("--safe-writes", action="store_true", help="safe-write mode: allow create, block delete/edit/payment")
    ap.add_argument("--scope-mode", choices=["site", "section", "page"], default="site",
                    help="site (whole site, default), section (the URL's path and under it), or page (only the start page)")
    ap.add_argument("--owner", help="the account that owns this run (per-user cross-run memory)")
    ap.add_argument("--max-pages", type=int, default=150)
    ap.add_argument("--crawl-minutes", type=float, default=5.0)
    ap.add_argument("--rounds", type=int, default=2, help="checker round limit")
    ap.add_argument("--testers", type=int, default=3, help="parallel function testers (0 = no model calls)")
    ap.add_argument("--journey-testers", type=int, default=1)
    ap.add_argument("--ui-testers", type=int, default=1)
    ap.add_argument("--docs", help="product documents: a .txt/.md/.html file or a folder (optional)")
    ap.add_argument("--manual", action="store_true", help="also write a user manual from the passed journeys")
    ap.add_argument("--files", help="test files for upload fields: a file or a folder (optional)")
    ap.add_argument("--secrets", help="a KEY=VALUE file with SITE_EMAIL and SITE_PASSWORD, and optionally "
                    "SITE_EMAIL_2 / SITE_PASSWORD_2 ... (one account per tester); read by code only")
    ap.add_argument("--minutes", type=float, default=60.0, help="time limit of the whole run (the brief's target is 25)")
    ap.add_argument("--report-every", type=float, default=0, help="write an interim report every N minutes; the run continues")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    if a.secrets:
        _load_secrets(a.secrets)
        a.login = True
    out = a.out or os.path.join(HERE, "state", "v2", datetime.now().strftime("%Y%m%d_%H%M%S"))
    try:
        code, data = run(a.url, out, login=a.login, allow_writes=a.allow_writes, safe_writes=a.safe_writes,
                         scope_mode=a.scope_mode, owner=a.owner, max_pages=a.max_pages,
                         crawl_min=a.crawl_minutes, rounds=a.rounds, headless=not a.headed,
                         results_json=a.results_json, testers=a.testers, budget_min=a.minutes,
                         journey_testers=a.journey_testers, ui_testers=a.ui_testers, docs=a.docs, manual=a.manual,
                         files=a.files, report_every=a.report_every)
    except Exception as e:
        print(f"[v2] crashed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"[v2] report: {os.path.join(out, 'report.html')}")
    if code == 2:
        print("[v2] ended early (stopped by the user, or the first page did not load)", file=sys.stderr)
    # Exit now. A ThreadPoolExecutor (planner) registers an atexit join that can block process exit for a long
    # time when a daemon worker is stuck in a model call. The report and results are already written, so exit
    # hard to avoid that hang (important so the server's Stop returns promptly).
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    main()
