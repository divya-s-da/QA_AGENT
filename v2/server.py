"""A small job server for QA Agent v2 (standard library only), for hosting on a VM.

    V2_API_TOKEN=... python -m react_qa_agent.v2.server --port 8080 [--runs DIR] [--parallel 1]

API (every /api call needs the header  Authorization: Bearer <V2_API_TOKEN>):
  POST /api/runs        {"url", "email"?, "password"?, "accounts"?: [{"email", "password"}, ...] (one per tester),
                         "allow_writes"?, "minutes"?, "testers"?, "docs"?, "manual"?}
                        -> {"id"}. The login is written to the run folder with mode 600 and read only by code.
  GET  /api/runs        -> the runs, newest first
  GET  /api/runs/<id>   -> status (queued / running / done / failed), live counts from the task board,
                           the verdict when done, the last log lines
  GET  /runs/<id>/<file> -> report.html, report.docx, report.txt, report.json, screenshots (same token, or
                           ?token=... so a browser can open the report)
  GET  /                -> a plain start page (the real UI comes later)

Runs go in a queue; --parallel runs at most that many at once (each run starts several browsers).
"""

import argparse
import html
import ipaddress
import json
import os
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    from .accounts import Accounts
except ImportError:                     # running as a loose script
    from accounts import Accounts

UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
PKG = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ID = re.compile(r"^[a-z0-9]{12}$")
MIME = {".html": "text/html; charset=utf-8", ".json": "application/json", ".txt": "text/plain; charset=utf-8",
        ".png": "image/png", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".md": "text/markdown; charset=utf-8", ".log": "text/plain; charset=utf-8", ".pdf": "application/pdf"}


def ssrf_reason(url: str) -> str | None:
    """Block a target that resolves to an internal address (SSRF), unless
    V2_ALLOW_PRIVATE_HOSTS=1 (for local benchmarks/dev). Returns the reason or None."""
    if os.getenv("V2_ALLOW_PRIVATE_HOSTS") == "1":
        return None
    host = urlparse(url).hostname
    if not host:
        return "the URL has no host"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None                       # cannot resolve now: let the crawl fail naturally, do not block on a DNS hiccup
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return (f"the target host {host} resolves to an internal address ({ip}); "
                    "this is blocked. Set V2_ALLOW_PRIVATE_HOSTS=1 only for a trusted local test.")
    return None


class Jobs:
    def __init__(self, runs_dir: str, parallel: int = 1):
        self.dir, self.parallel = runs_dir, parallel
        os.makedirs(runs_dir, exist_ok=True)
        self.queue, self.running, self.lock = deque(), {}, threading.Lock()
        self._boot_reconcile()
        threading.Thread(target=self._loop, daemon=True).start()

    def _boot_reconcile(self):
        """On start, no run from a previous server is still ours. Mark any job left
        'running'/'queued' as stopped (so it is not reported alive forever), and
        delete any leftover secrets.env (a plaintext login from a crashed run)."""
        for d in os.listdir(self.dir):
            if not ID.match(d):
                continue
            sp = self._path(d, "secrets.env")
            if os.path.isfile(sp):
                try:
                    os.remove(sp)
                except OSError:
                    pass
            jp = self._path(d, "job.json")
            if not os.path.isfile(jp):
                continue
            try:
                with open(jp, encoding="utf-8") as f:
                    job = json.load(f)
            except (OSError, ValueError):
                continue
            if job.get("status") in ("running", "queued"):
                job["status"], job["ended"] = "stopped", time.time()
                try:
                    with open(jp, "w", encoding="utf-8") as f:
                        json.dump(job, f)
                except OSError:
                    pass

    def _path(self, rid, *p):
        return os.path.join(self.dir, rid, *p)

    def create(self, req: dict, owner: str | None = None) -> str:
        url = str(req.get("url", "")).strip()
        if not re.match(r"^https?://", url):
            raise ValueError("url must start with http:// or https://")
        reason = ssrf_reason(url)
        if reason:
            raise ValueError(reason)
        rid = secrets.token_hex(6)
        os.makedirs(self._path(rid), exist_ok=True)
        accs = [(req.get("email"), req.get("password"))] if req.get("email") and req.get("password") else []
        accs += [(a.get("email"), a.get("password")) for a in (req.get("accounts") or [])[:20]
                 if isinstance(a, dict) and a.get("email") and a.get("password")]
        if accs:
            sp = self._path(rid, "secrets.env")
            fd = os.open(sp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                for i, (e, pw) in enumerate(accs):
                    sfx = "" if i == 0 else f"_{i + 1}"
                    f.write(f"SITE_EMAIL{sfx}={str(e).strip()}\nSITE_PASSWORD{sfx}={str(pw).strip()}\n")
        if req.get("docs"):
            with open(self._path(rid, "docs.txt"), "w", encoding="utf-8") as f:
                f.write(str(req["docs"])[:200000])
        job = {"id": rid, "url": url, "status": "queued", "created": time.time(), "owner": owner,
               "allow_writes": bool(req.get("allow_writes")), "safe_writes": bool(req.get("safe_writes")),
               "scope_mode": str(req.get("scope_mode") or "site"),
               "minutes": float(req.get("minutes") or 60),
               "testers": int(req.get("testers", 3)), "manual": bool(req.get("manual")),
               "login": bool(accs), "accounts": len(accs)}
        self._save(job)
        with self.lock:
            self.queue.append(rid)
        return rid

    def _save(self, job):
        with open(self._path(job["id"], "job.json"), "w", encoding="utf-8") as f:
            json.dump(job, f)

    def note(self, rid: str, message: str) -> dict | None:
        """Store a live chat note from the owner. The message is appended to live_notes.txt (the running
        testers read it before each task) and to chat.jsonl (the UI shows the history)."""
        if not ID.match(rid or "") or not os.path.isfile(self._path(rid, "job.json")):
            return None
        message = str(message or "").strip()[:4000]
        if not message:
            return None
        ts = time.time()
        with open(self._path(rid, "live_notes.txt"), "a", encoding="utf-8") as f:
            f.write(message + "\n")
        reply = ("Noted. The testers will follow this for the tasks that come next. "
                 "It does not change tasks that already finished.")
        with open(self._path(rid, "chat.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"role": "user", "text": message, "ts": ts}) + "\n")
            f.write(json.dumps({"role": "agent", "text": reply, "ts": ts + 0.001}) + "\n")
        return {"reply": reply, "ts": ts}

    def _chat(self, rid: str) -> list:
        p = self._path(rid, "chat.jsonl")
        if not os.path.isfile(p):
            return []
        out = []
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        return out[-100:]

    def get(self, rid: str) -> dict | None:
        if not ID.match(rid or "") or not os.path.isfile(self._path(rid, "job.json")):
            return None
        with open(self._path(rid, "job.json"), encoding="utf-8") as f:
            job = json.load(f)
        db = self._path(rid, "board.db")
        if os.path.isfile(db):
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
                job["board"] = dict(con.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall())
                job["verdicts"] = dict(con.execute("SELECT verdict, COUNT(*) FROM tasks WHERE status='judged' GROUP BY verdict").fetchall())
                job["fails"] = [{"verdict": v, "title": (t or "")[:90], "page": p, "team": tm}
                                for v, t, p, tm in con.execute(
                                    "SELECT verdict, title, page, team FROM tasks WHERE status='judged' "
                                    "AND verdict IN ('FAIL','UNCERTAIN') ORDER BY CASE verdict WHEN 'FAIL' THEN 0 ELSE 1 END, risk DESC LIMIT 60").fetchall()]
                con.close()
            except sqlite3.Error:
                pass
        ev = self._path(rid, "evidence")
        if os.path.isdir(ev):
            shots = [f for f in os.listdir(ev) if f.endswith(".png")]
            if shots:
                job["latest_shot"] = max(shots, key=lambda f: os.path.getmtime(os.path.join(ev, f)))
        rep = self._path(rid, "report.json")
        if os.path.isfile(rep):
            with open(rep, encoding="utf-8") as f:
                r = json.load(f)
            job["verdict"] = r.get("verdict")
            job["summary"] = r.get("summary")
            job["memory_on"] = r.get("memory_on", False)
            job["new_count"] = r.get("new_count", 0)
            job["recurring_count"] = r.get("recurring_count", 0)
        log = self._path(rid, "run.log")
        if os.path.isfile(log):
            with open(log, encoding="utf-8", errors="replace") as f:
                job["log_tail"] = [l.rstrip() for l in f.readlines()[-40:] if "Warning" not in l]
        job["chat"] = self._chat(rid)
        return job

    def owner_of(self, rid: str) -> str | None:
        """Read just the owner of a run cheaply (no board/report/log work)."""
        try:
            with open(self._path(rid, "job.json"), encoding="utf-8") as f:
                return json.load(f).get("owner")
        except (OSError, ValueError):
            return None

    def all(self, owner: str | None = None) -> list:
        """All runs, newest first. owner=None means the operator (every run);
        otherwise only runs this owner owns (the cheap owner check skips the rest)."""
        out = []
        for d in os.listdir(self.dir):
            if not ID.match(d) or not os.path.isfile(self._path(d, "job.json")):
                continue
            if owner is not None and self.owner_of(d) != owner:
                continue
            j = self.get(d)
            if j:
                out.append(j)
        return sorted(out, key=lambda j: -j["created"])

    def _start(self, rid):
        job = self.get(rid)
        out = self._path(rid)
        cmd = [sys.executable, "-u", "-m", f"{PKG}.v2.run", "--url", job["url"], "--out", out,
               "--minutes", str(job["minutes"]), "--testers", str(job["testers"])]
        if job["login"]:
            cmd += ["--secrets", self._path(rid, "secrets.env")]
        if job["allow_writes"]:
            cmd.append("--allow-writes")
        elif job.get("safe_writes"):
            cmd.append("--safe-writes")
        if job.get("scope_mode") and job["scope_mode"] != "site":
            cmd += ["--scope-mode", job["scope_mode"]]
        if job.get("owner"):
            cmd += ["--owner", str(job["owner"])]
        if os.path.isfile(self._path(rid, "docs.txt")):
            cmd += ["--docs", self._path(rid, "docs.txt")]
        if job["manual"]:
            cmd.append("--manual")
        log = open(self._path(rid, "run.log"), "w", encoding="utf-8")
        # Cross-run memory persists next to the runs dir (on the mounted volume), so "new since last run"
        # survives a restart. The user can override with V2_MEMORY_DIR.
        run_env = dict(os.environ, PYTHONUNBUFFERED="1")
        run_env.setdefault("V2_MEMORY_DIR", os.path.join(os.path.dirname(os.path.abspath(self.dir)), "memory"))
        # start_new_session puts the run (and its browser subprocesses) in its own process group, so Stop can
        # kill the whole tree, not just the python parent.
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                             env=run_env, start_new_session=True)
        job.update(status="running", started=time.time(), pid=p.pid)
        self._save({k: v for k, v in job.items() if k in ("id", "url", "status", "created", "owner", "allow_writes", "safe_writes", "scope_mode", "minutes",
                                                          "testers", "manual", "login", "accounts", "started", "pid")})
        return p, log

    def _mark_stopped(self, rid):
        job = self.get(rid) or {"id": rid}
        base = {k: job[k] for k in ("id", "url", "created", "owner", "allow_writes", "safe_writes", "scope_mode", "minutes", "testers",
                                    "manual", "login", "accounts", "started") if k in job}
        base.update(status="stopped", ended=time.time())
        self._save(base)

    def stop(self, rid: str) -> bool:
        """Stop a queued or running job. The run's own files (report so far, board) are kept."""
        if not ID.match(rid or ""):
            return False
        with self.lock:
            if rid in self.queue:
                try:
                    self.queue.remove(rid)
                except ValueError:
                    pass
                self._mark_stopped(rid)
                return True
            pair = self.running.pop(rid, None)      # remove first, so _loop does not also reconcile it
        if not pair:
            return False
        p, log = pair
        try:
            pgid = os.getpgid(p.pid)
        except Exception:
            pgid = None
        try:
            open(self._path(rid, "STOP"), "w").close()      # ask the run to stop gracefully and write a final report
        except OSError:
            pass
        try:
            p.wait(timeout=30)                              # the run notices STOP, builds the report, and exits on its own
        except Exception:                                   # it did not exit in time: force the whole group
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGTERM)
                    try:
                        p.wait(timeout=6)
                    except Exception:
                        os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    pass
            try:
                p.kill()
            except Exception:
                pass
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)             # reap any browser subprocess the run left behind
            except Exception:
                pass
        try:
            log.close()
        except Exception:
            pass
        sp = self._path(rid, "secrets.env")
        if os.path.isfile(sp):
            try:
                os.remove(sp)                       # the login is not kept after the run
            except OSError:
                pass
        self._mark_stopped(rid)
        return True

    def _loop(self):
        while True:
            with self.lock:
                for rid, (p, log) in list(self.running.items()):
                    if p.poll() is not None:
                        log.close()
                        job = self.get(rid)
                        base = {k: job[k] for k in ("id", "url", "created", "owner", "allow_writes", "safe_writes", "scope_mode", "minutes", "testers",
                                                    "manual", "login", "accounts", "started") if k in job}
                        base.update(status="done" if p.returncode in (0, 2) else "failed", ended=time.time(),
                                    exit_code=p.returncode)
                        self._save(base)
                        sp = self._path(rid, "secrets.env")
                        if os.path.isfile(sp):
                            os.remove(sp)          # the login is not kept after the run
                        del self.running[rid]
                while self.queue and len(self.running) < self.parallel:
                    rid = self.queue.popleft()
                    try:
                        self.running[rid] = self._start(rid)
                    except Exception as e:
                        job = self.get(rid) or {"id": rid}
                        job.update(status="failed", error=str(e)[:300])
                        self._save(job)
            time.sleep(1.0)


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>QA Agent</title><style>body{font:15px/1.5 system-ui,sans-serif;max-width:860px;margin:30px auto;padding:0 16px;color:#1d1a16;background:#f6f3ec}
input,button{font:inherit;padding:6px 8px;margin:3px 0}input[type=text],input[type=password],input[type=url]{width:100%%}
table{border-collapse:collapse;width:100%%;background:#fffdf8}td,th{border:1px solid #d8d2c4;padding:5px 7px;text-align:left}
.card{background:#fffdf8;border:1.5px solid #1d1a16;border-radius:10px;padding:14px 16px;margin:14px 0}</style></head><body>
<h1>QA Agent</h1><div class=card><form id=f><label>Website URL<input type=url name=url required placeholder="https://app.example.com"></label>
<label>Test login (optional)<input type=text name=email placeholder="email or user name"></label>
<input type=password name=password placeholder="password">
<label><input type=checkbox name=allow_writes> Allow writes (orders, deletes, uploads) - only on a test environment</label><br>
<label>Minutes <input type=number name=minutes value=60 min=5 max=120 style="width:80px"></label>
<label><input type=checkbox name=manual> Also write a user manual</label><br><button>Start a run</button></form></div>
<div class=card><b>Runs</b><table id=t><tr><th>When</th><th>Site</th><th>Status</th><th>Verdict</th><th></th></tr></table></div>
<script>const T=%s;const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const H={'Authorization':'Bearer '+T,'Content-Type':'application/json'};
f.onsubmit=async e=>{e.preventDefault();const d=Object.fromEntries(new FormData(f));d.allow_writes=!!d.allow_writes;d.manual=!!d.manual;
const r=await fetch('/api/runs',{method:'POST',headers:H,body:JSON.stringify(d)});alert(r.ok?'Started':'Error: '+await r.text());load()};
async function load(){const r=await fetch('/api/runs',{headers:H});if(!r.ok)return;const js=await r.json();
t.innerHTML='<tr><th>When</th><th>Site</th><th>Status</th><th>Verdict</th><th></th></tr>'+js.map(j=>`<tr><td>${new Date(j.created*1000).toLocaleString()}</td>
<td>${esc(j.url)}</td><td>${esc(j.status)}${j.board?' '+esc(JSON.stringify(j.verdicts||{})):''}</td><td>${j.verdict?esc(j.verdict.line):''}</td>
<td>${j.status=='done'?`<a href="/runs/${j.id}/report.html?token=${T}" target=_blank>report</a>`:''}</td></tr>`).join('')}
load();setInterval(load,5000)</script></body></html>"""


def ui_page() -> str:
    """The product UI (v2/ui.html). It handles login itself (a session token in a
    header); the server injects only a 'served' marker, never a secret."""
    try:
        with open(UI_FILE, encoding="utf-8") as f:
            return f.read().replace("__SERVED__", "1").replace("__TOKEN__", "")
    except OSError:
        return "<!doctype html><title>QA Agent</title><p>UI file missing.</p>"


def make_handler(jobs: Jobs, token: str, accounts: Accounts):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _bearer(self, q=None) -> str:
            got = self.headers.get("Authorization", "")
            if got.startswith("Bearer "):
                return got[7:]
            return (q or {}).get("token", [""])[0]          # for <img>/<a> links that cannot send a header

        def _ident(self, q=None):
            """Return (user, is_operator). user is None when not authenticated.
            The operator token (V2_API_TOKEN) sees every run; a user session sees only its own."""
            tok = self._bearer(q)
            if tok and secrets.compare_digest(tok, token):
                return "operator", True
            return (accounts.user_for(tok) if tok else None), False

        def _owns(self, rid, user, op) -> bool:
            return op or jobs.owner_of(rid) == user

        def _send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else (json.dumps(body, default=str) if ctype == "application/json" else body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _json_body(self, limit=1_000_000):
            n = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(min(n, limit)) or b"{}")

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":                               # the page loads for anyone; it logs in client-side
                return self._send(200, ui_page(), "text/html; charset=utf-8")
            user, op = self._ident(q)
            if u.path == "/api/me":
                return self._send(200, {"user": user, "operator": op}) if user else self._send(401, {"error": "not logged in"})
            if not user:
                return self._send(401, {"error": "login required"})
            if u.path == "/api/runs":
                return self._send(200, jobs.all(owner=None if op else user))
            m = re.match(r"^/api/runs/([a-z0-9]+)$", u.path)
            if m:
                j = jobs.get(m.group(1))
                if not j or not self._owns(m.group(1), user, op):
                    return self._send(404, {"error": "no such run"})
                return self._send(200, j)
            m = re.match(r"^/runs/([a-z0-9]{12})/(.+)$", u.path)
            if m:
                rid = m.group(1)
                if not self._owns(rid, user, op):
                    return self._send(404, {"error": "not found"})
                base = os.path.realpath(jobs._path(rid))
                f = os.path.realpath(os.path.join(base, m.group(2)))
                if not f.startswith(base + os.sep) or os.path.basename(f) in ("secrets.env", "board.db") \
                        or not os.path.isfile(f):
                    return self._send(404, {"error": "not found"})
                with open(f, "rb") as fh:
                    return self._send(200, fh.read(), MIME.get(os.path.splitext(f)[1].lower(), "application/octet-stream"))
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            # login and register do not require an existing session
            if path in ("/api/login", "/api/register"):
                try:
                    req = self._json_body(100_000)
                except (ValueError, json.JSONDecodeError) as e:
                    return self._send(400, {"error": str(e)[:200]})
                if path == "/api/login":
                    tok = accounts.login(req.get("username", ""), req.get("password", ""))
                    return self._send(200, {"token": tok, "user": (req.get("username") or "").strip()}) if tok \
                        else self._send(401, {"error": "wrong username or password"})
                _, op = self._ident()
                if not (op or os.getenv("V2_OPEN_SIGNUP") == "1" or accounts.count() == 0):
                    return self._send(403, {"error": "registration is closed; ask the operator to create your account"})
                ok, msg = accounts.create_user(req.get("username", ""), req.get("password", ""))
                return self._send(200 if ok else 400, {"ok": ok, "message": msg})
            user, op = self._ident()
            if not user:
                return self._send(401, {"error": "login required"})
            if path == "/api/logout":
                accounts.logout(self._bearer())
                return self._send(200, {"ok": True})
            m = re.match(r"^/api/runs/([a-z0-9]{12})/stop$", path)
            if m:
                if not self._owns(m.group(1), user, op):
                    return self._send(404, {"error": "no such run"})
                return self._send(200, {"stopped": jobs.stop(m.group(1))})
            m = re.match(r"^/api/runs/([a-z0-9]{12})/note$", path)
            if m:
                if not self._owns(m.group(1), user, op):
                    return self._send(404, {"error": "no such run"})
                try:
                    req = self._json_body(100_000)
                except (ValueError, json.JSONDecodeError) as e:
                    return self._send(400, {"error": str(e)[:200]})
                r = jobs.note(m.group(1), req.get("message", ""))
                return self._send(200, r) if r else self._send(404, {"error": "no such run or empty note"})
            if path != "/api/runs":
                return self._send(404, {"error": "not found"})
            try:
                req = self._json_body(1_000_000)
                return self._send(201, {"id": jobs.create(req, owner=user)})
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)[:200]})
    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--runs", default=os.path.join(ROOT, PKG, "state", "v2_runs"))
    ap.add_argument("--parallel", type=int, default=1)
    a = ap.parse_args()
    token = os.getenv("V2_API_TOKEN", "")
    if len(token) < 16:
        sys.exit("set V2_API_TOKEN (16 characters or more) before you start the server")
    jobs = Jobs(a.runs, a.parallel)
    # accounts live NEXT TO the runs dir, so on a mounted volume (e.g. /data/runs -> /data/accounts.db) they
    # survive a container restart. Override with V2_ACCOUNTS_DB.
    accounts = Accounts(os.getenv("V2_ACCOUNTS_DB") or os.path.join(os.path.dirname(os.path.abspath(a.runs)), "accounts.db"))
    signup = "open" if os.getenv("V2_OPEN_SIGNUP") == "1" else ("bootstrap" if accounts.count() == 0 else "operator-only")
    print(f"QA Agent v2 server on http://{a.host}:{a.port}/  runs in {a.runs}", flush=True)
    print(f"  accounts: {accounts.count()} user(s); sign-up: {signup}. Operator token logs in as 'operator' (sees all).",
          flush=True)
    ThreadingHTTPServer((a.host, a.port), make_handler(jobs, token, accounts)).serve_forever()


if __name__ == "__main__":
    main()
