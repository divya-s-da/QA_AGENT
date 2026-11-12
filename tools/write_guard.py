"""Write guard: block state-changing HTTP requests in the shared browser.

Read-only is the default. A sentence in the prompt cannot stop a model from
clicking "Delete" on a production site; this can. The guard holds its own CDP
connection to the shared Chrome and routes every request in every context:
GET/HEAD/OPTIONS pass, GraphQL queries pass, a URL on the allowlist passes,
and everything else is aborted before it leaves the machine.

The connection must stay open - Chrome drops a client's request interception
when that client disconnects - so the guard lives in a daemon thread with its
own event loop. ensure_active() runs before every browser tool, so a dead
guard blocks the tool instead of letting writes through (fail closed).

Not covered: WebSocket frames and requests made by service workers. Treat a
site that writes over either as unsafe for read-only runs.

Configuration:
  QA_WRITE_MODE       "read-only" (default) or "allow". Used by the ADK web
                      path; the CLI pipeline sets the mode from the charter.
  QA_WRITE_ALLOWLIST  comma-separated fnmatch URL patterns that may write,
                      e.g. "*/api/auth/refresh,*/api/search"
"""

import asyncio
import fnmatch
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlparse

from .browser_server import CDP_URL, is_browser_alive

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_READY_TIMEOUT = 15
_MAX_LOG = 200

# Read .env here as well as in config.py. This module is imported by tools
# that do not import config, so without this the mode and the allowlist depend
# on import order - and an allowlist that silently stays empty looks exactly
# like a guard that works, right up to the moment a login is blocked.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:
    pass

# Three write modes:
#   read-only : block every non-GET (the default, safe for any site).
#   safe      : allow create-shaped writes (POST), but block DELETE, block
#               PUT/PATCH edits to existing data, and block destructive or
#               financial POSTs (delete/cancel/refund/checkout/...). Every
#               write that is let through is written to a ledger so the owner
#               can see exactly what the run created.
#   allow     : allow every write (only on a throwaway test environment).
_DESTRUCTIVE = re.compile(
    r"(delete|destroy|remove|cancel|deactiv|disable|purge|wipe|revoke|refund|chargeback|"
    r"payment|/pay(ment)?(/|$|\?)|checkout|billing|place[-_]?order|confirm[-_]?order|unsubscribe)",
    re.I,
)


def _norm_mode(v) -> str:
    v = (v or "").strip().lower()
    if v in ("allow", "safe", "read-only"):
        return v
    if v in ("allow-writes", "1", "true", "yes", "on"):
        return "allow"
    if v in ("safe-write", "safe-writes", "create-only"):
        return "safe"
    return "read-only"


_lock = threading.Lock()
_config = {
    "mode": _norm_mode(os.getenv("QA_WRITE_MODE", "read-only")),
    "allowlist": [p.strip() for p in os.getenv("QA_WRITE_ALLOWLIST", "").split(",") if p.strip()],
}
_login = {"host": None}
_blocked = []  # most recent blocked requests, oldest first
_writes = []   # most recent writes LET THROUGH (the ledger), oldest first
_guard = {"thread": None, "ready": threading.Event(), "alive": False, "error": ""}


def configure(allow_writes=None, allowlist=None, mode=None) -> None:
    """Set the write mode for this process (the CLI calls this from the charter).

    Pass `mode` ("read-only" | "safe" | "allow"), or the legacy `allow_writes`
    bool (True -> "allow", False -> "read-only"). `mode` wins when both are given.
    """
    with _lock:
        if mode is not None:
            _config["mode"] = _norm_mode(mode)
        elif allow_writes is not None:
            _config["mode"] = "allow" if allow_writes else "read-only"
        if allowlist is not None:
            _config["allowlist"] = [str(p).strip() for p in allowlist if str(p).strip()]


def write_mode() -> str:
    return _config.get("mode", "read-only")


def writes_allowed() -> bool:
    """True only in the fully-open 'allow' mode (used to skip the read-only guard)."""
    return write_mode() == "allow"


def safe_mode() -> bool:
    return write_mode() == "safe"


def mode_label() -> str:
    return {"allow": "allow-writes", "safe": "safe-write", "read-only": "read-only"}.get(write_mode(), "read-only")


def mode_help() -> str:
    """A one-line description of the current mode, for a tester's prompt."""
    m = write_mode()
    if m == "allow":
        return "Writes are ON. Create, edit, and delete are all allowed (test environment only)."
    if m == "safe":
        return ("Safe-write is ON. You MAY create new records (submit a create form). You may NOT delete or "
                "edit existing data, and payments/checkout are blocked; such requests are refused by a guard.")
    return "Read-only. Every create/edit/delete is blocked by a guard; report such a task as BLOCKED, not FAIL."


@contextmanager
def login_window(host: str):
    """Let writes to `host` (and its subdomains) through while logging in.

    Login is a POST, so a read-only run cannot authenticate without this.
    The window is scoped to the site's host and closes when the block exits.
    """
    with _lock:
        _login["host"] = (host or "").lower() or None
    try:
        yield
    finally:
        with _lock:
            _login["host"] = None


def _is_graphql_read(body) -> bool:
    """True only for a GraphQL body that is clearly queries and nothing else.

    Persisted queries (hash only, no query text) cannot be inspected, so they
    count as writes. A false "write" only blocks a read; the reverse is the
    failure this module exists to prevent.
    """
    if not body:
        return False
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return False
    ops = data if isinstance(data, list) else [data]
    if not ops:
        return False
    for op in ops:
        query = op.get("query") if isinstance(op, dict) else None
        if not isinstance(query, str):
            return False
        query = re.sub(r"#[^\n]*", "", query)
        if re.search(r"\b(mutation|subscription)\b", query):
            return False
        if not re.match(r"\s*(query\b|\{)", query):
            return False
    return True


def decide(method: str, url: str, body=None) -> str | None:
    """None when the request may go out, otherwise the reason it is blocked."""
    method = (method or "").upper()
    if method in _SAFE_METHODS:
        return None
    mode = write_mode()
    if mode == "allow":
        _record_write(method, url, "allow")
        return None
    with _lock:
        login_host = _login["host"]
        allowlist = list(_config["allowlist"])
    host = (urlparse(url).hostname or "").lower()
    if login_host and (host == login_host or host.endswith("." + login_host)):
        return None                                   # a login POST: opens a session, changes no data
    if any(fnmatch.fnmatch(url, pattern) for pattern in allowlist):
        _record_write(method, url, "allowlist")
        return None
    if method == "POST" and _is_graphql_read(body):
        return None                                   # a GraphQL query is a read
    if mode == "safe":
        path = urlparse(url).path or ""
        if method == "DELETE":
            return "DELETE blocked by safe-write guard (existing data is never deleted)"
        if method in ("PUT", "PATCH"):
            return f"{method} blocked by safe-write guard (existing data is never edited)"
        if _DESTRUCTIVE.search(path) or _DESTRUCTIVE.search(url):
            return f"{method} blocked by safe-write guard (looks destructive or financial)"
        _record_write(method, url, "safe-create")     # a create-shaped write: let it through, but remember it
        return None
    return f"{method} blocked by read-only write guard"


def _record_block(method: str, url: str, reason: str) -> None:
    with _lock:
        _blocked.append({
            "time": time.strftime("%H:%M:%S"),
            "method": method,
            "url": url[:300],
            "reason": reason,
        })
        del _blocked[:-_MAX_LOG]


def _record_write(method: str, url: str, kind: str) -> None:
    """Remember a write that was let through (the ledger). Login POSTs are not
    recorded here - they open a session and change no data."""
    with _lock:
        _writes.append({
            "time": time.strftime("%H:%M:%S"),
            "method": method,
            "url": url[:300],
            "kind": kind,
        })
        del _writes[:-_MAX_LOG]


def writes_made() -> list:
    """The ledger of writes this run let through (create-shaped POSTs, allowlist, or allow-mode)."""
    with _lock:
        return list(_writes)


async def _handle(route, request):
    method = request.method
    try:
        try:
            body = request.post_data
        except Exception:
            body = None  # binary body: cannot be a GraphQL query
        reason = decide(method, request.url, body)
    except Exception as e:
        reason = f"guard error ({type(e).__name__}) - blocked to stay safe"
    try:
        if reason is None:
            await route.continue_()
        else:
            _record_block(method, request.url, reason)
            await route.abort("blockedbyclient")
    except Exception:
        pass  # page closed mid-request


async def _guard_loop():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        gone = asyncio.Event()
        browser.on("disconnected", lambda *_: gone.set())
        routed = set()
        while not gone.is_set():
            # Poll for new contexts: pages inside a known context are routed
            # by Playwright before their first request, new contexts are not.
            for context in browser.contexts:
                if id(context) not in routed:
                    await context.route("**/*", _handle)
                    routed.add(id(context))
            _guard["alive"] = True
            _guard["ready"].set()
            try:
                await asyncio.wait_for(gone.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass


def _thread_main():
    try:
        asyncio.run(_guard_loop())
        _guard["error"] = "browser disconnected"
    except Exception as e:
        _guard["error"] = f"{type(e).__name__}: {e}"
    finally:
        _guard["alive"] = False
        _guard["ready"].set()  # wake a waiter so it sees the failure


def ensure_active() -> str | None:
    """Make sure the guard is routing requests. Returns an error message when
    the run is read-only and the guard cannot be confirmed, else None."""
    if writes_allowed():
        return None
    with _lock:
        thread = _guard["thread"]
        if _guard["alive"] and thread is not None and thread.is_alive():
            return None
        if not is_browser_alive():
            return f"BLOCKED: write guard inactive - shared browser at {CDP_URL} is not reachable."
        _guard["ready"].clear()
        _guard["alive"] = False
        _guard["error"] = ""
        thread = threading.Thread(target=_thread_main, name="qa-write-guard", daemon=True)
        _guard["thread"] = thread
        thread.start()
    _guard["ready"].wait(_READY_TIMEOUT)
    if _guard["alive"]:
        return None
    detail = _guard["error"] or f"not ready after {_READY_TIMEOUT}s"
    return f"BLOCKED: write guard could not start ({detail}). Read-only run cannot continue safely."


# Tools that never touch the browser keep working when the guard is down, so
# a session can still record BLOCKED evidence and build its report.
_EXEMPT_TOOLS = {
    "record_evidence", "coverage_report", "build_docx_report", "list_specs",
    "search_specs", "read_spec", "get_blocked_writes", "transfer_to_agent",
}


def tool_gate(tool_name: str) -> dict | None:
    """BeforeToolCallback helper: an error response that stops a browser tool
    while the read-only guard is down, else None."""
    if tool_name in _EXEMPT_TOOLS:
        return None
    error = ensure_active()
    return {"error": error} if error else None


def blocked_writes() -> list:
    with _lock:
        return list(_blocked)


def get_blocked_writes() -> str:
    """Return the write mode and the requests the read-only guard blocked.

    Call this when a create/edit/delete/submit action shows no response. If a
    blocked request matches that action, record the check as BLOCKED with
    reason 'write blocked by read-only guard' - never as FAIL, because the
    request never reached the site.
    """
    return json.dumps({"mode": mode_label(), "blocked": blocked_writes()[-20:]})
