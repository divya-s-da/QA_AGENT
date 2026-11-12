"""Target-site enforcement: the user requires the navigator to test ONLY the
site it was given and never wander the internet. The first real navigation a
session makes locks that host; any later navigation elsewhere is rejected."""

import threading
from urllib.parse import urlparse

_LOCK = {}  # session_id -> allowed host (lowercase, www. stripped)


def _host_of(url):
    try:
        u = urlparse(url if "://" in url else "//" + url)
        host = (u.hostname or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def lock_host(session_id, url):
    """Remember the target host for this session (first real navigation wins)."""
    host = _host_of(url)
    if host:
        _LOCK.setdefault(session_id, host)
    return _LOCK.get(session_id)


def allowed_host(session_id):
    return _LOCK.get(session_id)


def is_allowed(session_id, url):
    """True when url is on the session's locked host (or its subdomains)."""
    if not url or url.startswith(("about:", "data:", "javascript:", "chrome://", "file://")):
        return True
    host = _host_of(url)
    if not host:
        return True
    allowed = _LOCK.get(session_id)
    if not allowed:
        return True  # not locked yet — the first navigation defines the site
    return host == allowed or host.endswith("." + allowed)


def _check_live_page(session_id):
    from playwright.sync_api import sync_playwright
    from .browser_server import CDP_URL
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        for context in browser.contexts:
            for page in context.pages:
                if page.url and page.url != "about:blank":
                    if not is_allowed(session_id, page.url):
                        return True, page.url
                    return False, page.url
    return False, ""


def current_off_target(session_id):
    """(off_target, current_url) for the live page. Runs in a worker thread so
    the sync Playwright API is never called inside the asyncio loop (ADK web
    rejects sync Playwright in async context). No lock or an unreachable
    browser means the agent is assumed on target."""
    if not _LOCK.get(session_id):
        return False, ""
    result = {}

    def _run():
        try:
            result["v"] = _check_live_page(session_id)
        except Exception:
            result["v"] = (False, "")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=10)
    return result.get("v", (False, ""))
