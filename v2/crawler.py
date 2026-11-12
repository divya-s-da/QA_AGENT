"""The crawler: plain code that maps a site and runs the basic checks.

It opens its own browser (not the shared CDP browser), follows same-origin
links breadth first, and records every page, interactive element and link in
a SiteMap. Each page gets the basic checks as soon as it loads, so a dead first
page is found in the first seconds:

  load       the page loads: no HTTP error, no error page (stack trace, SQL error)
  errors     no uncaught JavaScript error and no console error
  resources  no same-origin image, script, style or font fails to load
  title      the page has a <title>
  links      after the crawl: every same-origin link has a target that opens

It never submits a form or clicks a button. Every request goes through the
write guard (tools/write_guard.decide), so a read-only crawl cannot change
data, and links that look like they change data (log out, delete) are not
followed. With a test login it crawls twice: logged out, then logged in.

Not covered yet: links made only by JavaScript click handlers (single-page
apps), pages behind SSO / CAPTCHA / OTP, and file downloads.
"""

import itertools
import os
import re
import threading
import time
from collections import deque
from urllib.parse import urljoin, urlparse

try:
    from ..tools import write_guard
except ImportError:          # run from the repo root (tests): `tools` is top level
    from tools import write_guard
from .board import TaskBoard
from .sitemap import Page, SiteMap, route_of, strip_fragment

ERROR_PAGE = re.compile(r"Traceback \(most recent call last\)|Internal Server Error|Stack trace:|"
                        r"\bat [\w.$<>]+ \([^)]*:\d+:\d+\)|SQLSTATE|psycopg2|OperationalError|"
                        r"(postgres(ql)?|mysql|mongodb)://\S+|ORA-\d{5}", re.I)
NO_FOLLOW = re.compile(r"log[-_ ]?out|sign[-_ ]?out|logoff|delete|remove|destroy|unsubscribe|deactivate|reset", re.I)
DOWNLOAD = re.compile(r"\.(pdf|zip|gz|tar|rar|7z|exe|dmg|msi|csv|xlsx?|docx?|pptx?|mp4|mp3|mov|txt|json|xml|"
                      r"jpe?g|png|gif|svg|webp|ico|bin|apk|iso)$", re.I)
NOT_FOUND = re.compile(r"\b(404|not found|page (does not|doesn't|could not be found|cannot be found)|no such page)\b", re.I)
CLICK_LINK = 10          # at most this many script-only links are clicked on one page to find routes
LOGIN_PATHS = ("/login", "/signin", "/sign-in", "/log-in", "/account/login", "/users/sign_in", "/auth/login")
RESOURCE_TYPES = {"image", "script", "stylesheet", "font", "media"}

# Measured page hygiene (no model): form fields without a name, skipped heading levels, sideways scroll.
HYGIENE_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const unlabeled = [];
  document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]), select, textarea')
    .forEach(el => { if (!vis(el)) return;
      const named = (el.getAttribute('aria-label') || '').trim() || el.getAttribute('aria-labelledby') || (el.title || '').trim()
        || (el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]')) || el.closest('label')
        || (el.getAttribute('placeholder') || '').trim();
      if (!named) unlabeled.push(el.getAttribute('name') || el.id || el.type || el.tagName.toLowerCase()); });
  const hs = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(vis).map(h => +h.tagName[1]);
  const skips = [];
  const minor = [];
  for (let i = 1; i < hs.length; i++) {
    if (hs[i] > hs[i - 1] + 2) skips.push('h' + hs[i - 1] + ' then h' + hs[i]);
    else if (hs[i] === hs[i - 1] + 2) minor.push('h' + hs[i - 1] + ' then h' + hs[i]); }
  return { unlabeled, skips, minor, overflow: document.documentElement.scrollWidth - window.innerWidth,
           width: document.documentElement.scrollWidth };
}
"""

EXTRACT_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const labelOf = el => { if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) return clean(l.innerText); } const p = el.closest('label'); return p ? clean(p.innerText) : ''; };
  const nameOf = el => clean(el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('title')
      || el.getAttribute('alt') || el.getAttribute('placeholder') || '');
  const path = el => { if (el.id) return '#' + CSS.escape(el.id); const parts = [];
    for (let n = el; n && n.nodeType === 1 && n !== document.body; n = n.parentElement) {
      if (n.id) { parts.unshift('#' + CSS.escape(n.id)); break; }
      let k = 1; for (let s = n.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === n.tagName) k++;
      parts.unshift(n.tagName.toLowerCase() + ':nth-of-type(' + k + ')'); }
    return parts.join(' > '); };
  const forms = [...document.forms];
  const out = [];
  const add = (el, kind, extra) => out.push(Object.assign({ kind, name: nameOf(el), label: labelOf(el),
      selector: path(el), form: el.form ? forms.indexOf(el.form) : (el.closest('form') ? forms.indexOf(el.closest('form')) : -1),
      visible: vis(el) }, extra || {}));
  forms.forEach((f, i) => out.push({ kind: 'form', name: clean(f.getAttribute('aria-label') || f.id || f.getAttribute('name') || ''),
      field: f.getAttribute('action') || '', type: (f.getAttribute('method') || 'get').toLowerCase(),
      selector: path(f), form: i, visible: vis(f), label: '' }));
  document.querySelectorAll('a').forEach(a => add(a, 'link', { href: a.hasAttribute('href') ? a.getAttribute('href') : null,
      abs: a.href || '' }));
  document.querySelectorAll('button, input[type=submit], input[type=button], input[type=reset], [role=button]')
    .forEach(b => { if (b.tagName === 'A') return; add(b, 'button', { type: b.getAttribute('type') || '', field: b.getAttribute('name') || '' }); });
  document.querySelectorAll('input:not([type=submit]):not([type=button]):not([type=reset]):not([type=hidden])').forEach(i => {
    const t = (i.getAttribute('type') || 'text').toLowerCase();
    add(i, t === 'checkbox' ? 'checkbox' : t === 'radio' ? 'radio' : 'input', { type: t, field: i.getAttribute('name') || i.id || '' }); });
  document.querySelectorAll('select').forEach(s => add(s, 'select', { field: s.getAttribute('name') || s.id || '',
      options: [...s.options].slice(0, 12).map(o => clean(o.label || o.text)) }));
  document.querySelectorAll('textarea').forEach(t => add(t, 'textarea', { field: t.getAttribute('name') || t.id || '' }));
  document.querySelectorAll('[role=tab]').forEach(t => add(t, 'tab', {}));
  return { elements: out.filter(e => e.visible || e.kind === 'form'), text: document.body ? document.body.innerText.slice(0, 20000) : '' };
}
"""


def _default_browsers_path() -> None:
    """Importing the react_qa_agent package loads the ADK agent, and its MCP setup
    (tools/mcp_runtime.py) points XDG_CACHE_HOME at /tmp/qa-mcp-cache. On Linux
    Playwright then looks for its browsers there. Point it back at the normal folder."""
    import sys
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    if sys.platform.startswith("linux") and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and "qa-mcp-cache" in xdg:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.expanduser("~/.cache/ms-playwright")


def _host(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


class Crawler:
    def __init__(self, site_url: str, out_dir: str, board: TaskBoard, *, email: str | None = None,
                 password: str | None = None, max_pages: int = 150, max_depth: int = 8, per_path: int = 3,
                 budget_s: float = 300.0, headless: bool = True, log=print, workers: int = 4,
                 notes: str = "", login_chat=None, scope: str | None = None, page_only: bool = False):
        self.site = site_url.rstrip("/") + "/"
        self.host = _host(site_url)
        # scope: only crawl/test pages whose path is this prefix or under it (None = whole site).
        # page_only: do not follow any link, so only the start page is tested.
        sc = (scope or "").strip().rstrip("/").lower()
        self.scope = sc if sc and sc != "" else None
        self.page_only = bool(page_only)
        self.out = out_dir
        self.board = board
        self.email, self.password = email, password
        self.notes, self.login_chat = notes or "", login_chat
        self.max_pages, self.max_depth, self.per_path = max_pages, max_depth, per_path
        self.budget_s, self.headless, self.log = budget_s, headless, log
        self.workers = max(1, workers)
        self.map = SiteMap(site_url=site_url)
        self.status: dict[str, int] = {}          # absolute URL (no fragment) -> HTTP status
        self.broken: dict[str, dict] = {}         # route -> {status, from}
        self.seen_issue: dict[tuple, str] = {}    # site-wide issue signature -> first route
        self.blocked: list[dict] = []
        self.t0 = time.monotonic()
        self.first_check_s: float | None = None
        self.login_result = "no login given"
        self._tl = threading.local()                 # each crawl worker has its own page buffers
        self.lock = threading.RLock()                # map, board bookkeeping and counters
        self._shots = itertools.count(1)
        self._in_login = False
        self.spa = False
        self.user_storage = None                        # set after login, for the design checker and probes
        self.clicked: dict[tuple, str | None] = {}     # (page route, link text) -> route it opened, or None
        os.makedirs(os.path.join(out_dir, "screens"), exist_ok=True)

    @property
    def _buf(self):
        if not hasattr(self._tl, "buf"):
            self._tl.buf = {"console": [], "pageerror": [], "resources": [], "doc": None}
        return self._tl.buf

    @_buf.setter
    def _buf(self, v):
        self._tl.buf = v

    # ---------- helpers ----------
    def _same_site(self, url: str) -> bool:
        u = urlparse(url)
        return u.scheme in ("http", "https") and _host(url) == self.host

    def _in_scope(self, url: str) -> bool:
        """True when the URL is inside the run's scope (whole site when no scope is set)."""
        if not self.scope:
            return True
        p = (urlparse(url).path or "/").rstrip("/").lower() or "/"
        return p == self.scope or p.startswith(self.scope + "/")

    def _elapsed(self) -> float:
        return time.monotonic() - self.t0

    def _guard(self, route, request):
        try:
            body = request.post_data                     # raises on a non-UTF-8 (binary/upload) body
        except Exception:
            body = None
        reason = write_guard.decide(request.method, request.url, body)
        if not reason and self._in_login and request.method not in ("GET", "HEAD", "OPTIONS") and self._same_site(request.url):
            path = urlparse(request.url).path
            if path not in self.map.login_posts:
                self.map.login_posts.append(path)
        if reason:
            self.blocked.append({"method": request.method, "url": request.url, "reason": reason})
            return route.abort()
        return route.continue_()

    def _attach(self, page):
        def on_console(msg):
            if msg.type == "error" and not msg.text.startswith("Failed to load resource"):
                self._buf["console"].append(msg.text[:300])

        def on_response(resp):
            req = resp.request
            if req.resource_type in RESOURCE_TYPES and resp.status >= 400 and self._same_site(resp.url):
                self._buf["resources"].append({"url": resp.url, "status": resp.status, "type": req.resource_type})

        def on_failed(req):
            if req.resource_type in RESOURCE_TYPES and self._same_site(req.url) \
                    and not any(b["url"] == req.url for b in self.blocked):
                self._buf["resources"].append({"url": req.url, "status": 0, "type": req.resource_type,
                                               "error": (req.failure or "")[:120]})
        page.on("console", on_console)
        page.on("pageerror", lambda e: self._buf["pageerror"].append(str(e)[:300]))
        page.on("response", on_response)
        page.on("requestfailed", on_failed)

    def _result(self, check, page: Page, verdict, reason, expected, observed, element_id=None, risk=1):
        self.board.add_result("code", f"{check} {page.route} ({page.auth})", verdict, observed, reason=reason,
                              expected=expected, page=page.path, element_id=element_id, user_state=page.auth,
                              kind=f"basic:{check}", risk=risk, steps=[f"open {page.url}"])
        with self.lock:
            first = self.first_check_s is None
            if first:
                self.first_check_s = round(self._elapsed(), 2)
        if first:
            self.log(f"  first page checked after {self.first_check_s}s: {page.route} {verdict}")

    def _site_wide(self, check: str, sig: str, route: str) -> str | None:
        """Report one site-wide issue once: None the first time, else the first route."""
        key = (check, sig)
        with self.lock:
            if key in self.seen_issue:
                return self.seen_issue[key]
            self.seen_issue[key] = route
            return None

    # ---------- one page ----------
    def _visit(self, page, url: str, auth: str, depth: int, found_from: str) -> tuple[Page | None, list[str]]:
        self._buf = {"console": [], "pageerror": [], "resources": [], "doc": None}
        route, path = route_of(url)
        started = time.monotonic()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_load_state("load", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(250)
        except Exception as e:
            if "Download is starting" in str(e):          # the link serves a file: not a page, not a failure
                self.status[url] = 200
                return None, []
            p = self.map.add_page(Page(route=route, path=path, url=url, auth=auth, status=0, depth=depth,
                                       found_from=found_from))
            self.status[url] = 0
            shot = self._shot(page, p)
            self._result("load", p, "FAIL", f"The page did not load: {str(e).splitlines()[0][:160]}", "The page loads.",
                         {"screenshot": shot or "none", "url": url, "server_responses": []}, risk=2)
            return p, []
        status = resp.status if resp else 0
        final = strip_fragment(page.url)
        self.status[url] = status
        f_route, f_path = route_of(final)
        spa_note = ""
        if status in (404, 410):
            # Single-page apps on static hosts answer 404 for client routes and still render the page.
            # A 404 that renders a real page (no "not found" text, several controls) counts as a page.
            try:
                probe = page.evaluate("() => ({ text: (document.body ? document.body.innerText : '').slice(0, 3000), "
                                      "n: document.querySelectorAll('a, button, input, select, textarea').length })")
            except Exception:
                probe = {"text": "", "n": 0}
            if NOT_FOUND.search(probe["text"]) or probe["n"] < 3:
                self.broken[route] = {"status": status, "from": found_from}
                return None, []
            self.spa = True
            spa_note = f" (the server answered {status}, but the app rendered the page: a single-page-app route)"
            status = 200
            self.status[url] = 200
        p = self.map.add_page(Page(route=route, path=path, url=url, auth=auth, status=status, final_url=final,
                                   depth=depth, found_from=found_from, load_ms=int((time.monotonic() - started) * 1000)))
        try:
            p.title = page.title()
            data = page.evaluate(EXTRACT_JS)
        except Exception as e:
            data = {"elements": [], "text": ""}
            self.map.notes.append(f"{route}: could not read the page ({str(e)[:80]})")
        shot = self._shot(page, p)
        doc = [{"url": final, "status": status}]
        proof = lambda **extra: {"screenshot": shot, "url": final, "server_responses": doc, **extra}  # noqa: E731

        # login required, or access denied for this user: not a defect of this page
        if f_path != path and any(f_path.startswith(lp) for lp in LOGIN_PATHS):
            self._result("load", p, "BLOCKED", f"{route} sends a logged-out user to {f_path} (login required).",
                         "The page loads.", proof())
            return p, [final]
        if status in (401, 403):
            self._result("load", p, "BLOCKED", f"{route} answers {status}: access denied for the test user "
                         f"({auth}). Correct if this user must not see it.", "The page loads.", proof())
            return p, []

        with self.lock:
            self.map.add_elements(p, data.get("elements", []))
        text = data.get("text", "")
        m = ERROR_PAGE.search(text)
        if status >= 500 or m:
            what = f"HTTP {status}" if status >= 500 else "an error page"
            shown = f"; the page shows '{m.group(0)[:80]}'" if m else ""
            self._result("load", p, "FAIL", f"{route} returns {what}{shown}.", "The page loads with no error page.",
                         proof(), risk=3 if m else 2)
        else:
            self._result("load", p, "PASS", f"{route} loads (HTTP {status}){spa_note}.", "The page loads with no error page.", proof())

        errs = [("uncaught JavaScript error: " + e) for e in self._buf["pageerror"]] + \
               [("console error: " + e) for e in self._buf["console"]]
        new = [e for e in errs if self._site_wide("errors", e, route) is None]
        if new:
            self._result("errors", p, "FAIL", f"{route}: {len(new)} JavaScript error(s) on load: " + " | ".join(new[:3]),
                         "No uncaught JavaScript error or console error.", proof(console=new), risk=2)
        else:
            self._result("errors", p, "PASS", f"{route}: no JavaScript error on load"
                         + (" (site-wide errors reported once)" if errs else "") + ".",
                         "No uncaught JavaScript error or console error.", proof())

        bad = [r for r in self._buf["resources"] if self._site_wide("resources", r["url"], route) is None]
        if bad:
            lines = [f"{r['type']} {urlparse(r['url']).path} ({r['status'] or r.get('error', 'failed')})" for r in bad]
            self._result("resources", p, "FAIL", f"{route}: {len(bad)} file(s) failed to load: broken " + ", ".join(lines[:4]),
                         "Every image, script, style and font loads.",
                         proof(server_responses=doc + [{"url": r["url"], "status": r["status"]} for r in bad]))
        else:
            self._result("resources", p, "PASS", f"{route}: all images, scripts and styles load.",
                         "Every image, script, style and font loads.", proof())

        if not (p.title or "").strip() and self._site_wide("title", path, route) is not None:
            pass          # the same empty title was reported for this path in the other login state
        elif not (p.title or "").strip():
            self._result("title", p, "FAIL", f"{route} has an empty <title>, so the browser tab shows no page name.",
                         "The page has a title.", proof())
        else:
            self._result("title", p, "PASS", f"{route} has the title '{p.title[:60]}'.", "The page has a title.", proof())

        # measured hygiene: field names, heading order, sideways scroll
        try:
            h = page.evaluate(HYGIENE_JS)
        except Exception:
            h = {"unlabeled": [], "skips": [], "overflow": 0, "width": 0}
        new_fields = [f for f in h["unlabeled"] if self._site_wide("labels", f, route) is None]
        if new_fields:
            self._result("labels", p, "FAIL", f"{route}: {len(new_fields)} form field(s) have no label or accessible name "
                         f"(unlabeled input: {', '.join(new_fields[:5])}); a screen reader cannot say what to type.",
                         "Every form field has a label.", proof(), risk=1)
        else:
            self._result("labels", p, "PASS", f"{route}: every visible form field has a name.", "Every form field has a label.", proof())
        if h["skips"] and self._site_wide("headings", path, route) is None:
            self._result("headings", p, "FAIL", f"{route}: the heading levels skip a level ({', '.join(h['skips'][:3])}); "
                         "the page hierarchy is broken for screen readers.", "Headings go down one level at a time.", proof(), risk=1)
        elif not h["skips"]:
            note = f" (minor: {', '.join(h.get('minor', [])[:2])} - a one-level skip, common and not a WCAG failure)" if h.get("minor") else ""
            self._result("headings", p, "PASS", f"{route}: heading levels are in order{note}.", "Headings go down one level at a time.", proof())
        if h["overflow"] > 8 and self._site_wide("overflow", path, route) is None:
            self._result("overflow", p, "FAIL", f"{route}: the page scrolls horizontally at 1280px: the content is "
                         f"{h['width']}px wide.", "No horizontal scroll on a desktop screen.", proof(), risk=1)
        elif h["overflow"] <= 8:
            self._result("overflow", p, "PASS", f"{route}: no horizontal scroll at 1280px.", "No horizontal scroll on a desktop screen.", proof())
        p.text = (data.get("text") or "")[:1500]

        # links to follow
        nxt = []
        for e in data.get("elements", []):
            if e.get("kind") != "link" or not e.get("abs"):
                continue
            target = strip_fragment(e["abs"])
            if not self._same_site(target) or not self._in_scope(target) or DOWNLOAD.search(urlparse(target).path):
                continue
            if NO_FOLLOW.search(target) or NO_FOLLOW.search(e.get("name") or ""):
                continue
            nxt.append(target)
        if not self.page_only:
            nxt += [t for t in self._click_discover(page, url, route, data.get("elements", [])) if self._in_scope(t)]
        return p, (nxt if not self.page_only else [])

    def _click_discover(self, page, url: str, route: str, elements: list) -> list[str]:
        """Script-only links (href '#', 'javascript:' or none) can lead to pages that no address names.
        Click a few of them (read-only: the write guard still blocks every write), note where they lead,
        and come back. Buttons are never clicked here: they act, links navigate."""
        cands = [e for e in elements if e.get("kind") == "link" and e.get("visible") and e.get("selector")
                 and (e.get("href") in (None, "", "#") or str(e.get("href")).startswith(("javascript:", "#")) and
                      not str(e.get("href")).startswith(("#/", "#!/")))
                 and not NO_FOLLOW.search(e.get("name") or "")][:CLICK_LINK]
        found = []
        for e in cands:
            try:
                page.locator(e["selector"]).first.click(timeout=3000)
                page.wait_for_timeout(700)
                now = strip_fragment(page.url)
                opened = route_of(now)[0] if route_of(now)[0] != route and self._same_site(now) else None
                self.clicked.setdefault((route, e.get("name") or ""), opened)
                if route_of(now)[0] != route and self._same_site(now):
                    found.append(now)
                    self.map.edges.append({"from": route, "to": route_of(now)[0], "auth": "click", "element": e.get("name", "")})
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(300)
            except Exception:
                try:
                    if strip_fragment(page.url) != strip_fragment(url):
                        page.goto(url, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    break
        return found

    def _shot(self, page, p: Page) -> str:
        name = f"{next(self._shots):03d}_{p.auth}_{re.sub(r'[^a-zA-Z0-9]+', '_', p.route).strip('_') or 'home'}.png"
        path = os.path.join(self.out, "screens", name[:120])
        try:
            page.screenshot(path=path, timeout=10000)
            p.screenshot = path
        except Exception:
            return ""
        return path

    # ---------- the crawl ----------
    def _stop_crawl(self) -> bool:
        return self._elapsed() > self.budget_s or len(self.map.pages) >= self.max_pages

    def _bfs(self, seeds: list[str], auth: str, storage=None) -> list[str]:
        """Breadth first over same-site links with `self.workers` browsers at once. Each worker has its own
        browser (Playwright's sync API is one per thread); a logged-in crawl gives each the login cookies."""
        from playwright.sync_api import sync_playwright
        q = deque((u, 0, "") for u in seeds)
        queued = set(seeds)
        per_path: dict[str, int] = {}
        state = {"busy": 0, "visited": 0}
        cond = threading.Condition(self.lock)

        def worker():
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=self.headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
                ctx = browser.new_context(viewport={"width": 1280, "height": 800}, ignore_https_errors=True,
                                          storage_state=storage)
                ctx.route("**/*", self._guard)
                page = ctx.new_page()
                self._attach(page)
                try:
                    while True:
                        with cond:
                            while not q and state["busy"] and not self._stop_crawl():
                                cond.wait(0.5)
                            if not q or self._stop_crawl():
                                cond.notify_all()
                                return
                            url, depth, src = q.popleft()
                            route, path = route_of(url)
                            if self.map.page(route, auth) or route in self.broken or per_path.get(path, 0) >= self.per_path:
                                continue
                            per_path[path] = per_path.get(path, 0) + 1
                            state["busy"] += 1
                        p, nxt = None, []
                        try:
                            p, nxt = self._visit(page, url, auth, depth, src)
                        except Exception as e:
                            self.map.notes.append(f"{route}: crawl error {type(e).__name__}")
                        finally:
                            with cond:
                                state["busy"] -= 1
                                state["visited"] += 1
                                if p is not None and depth < self.max_depth:
                                    for t in nxt:
                                        self.map.edges.append({"from": route, "to": route_of(t)[0], "auth": auth})
                                        if t not in queued:
                                            queued.add(t)
                                            q.append((t, depth + 1, route))
                                cond.notify_all()
                finally:
                    browser.close()

        threads = [threading.Thread(target=worker, name=f"crawl-{i + 1}", daemon=True) for i in range(self.workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        left = [u for u, _, _ in q if not self.map.page(route_of(u)[0], auth) and route_of(u)[0] not in self.broken]
        self.log(f"  {auth} crawl: {state['visited']} pages visited with {self.workers} browsers, {len(left)} left in the queue")
        return left

    def _login(self, page) -> bool:
        try:
            from ..tools.site_login import _PASSWORD_FIELD, _attempt, dismiss_overlays, guided_login, REGISTER_URL
        except ImportError:
            from tools.site_login import _PASSWORD_FIELD, _attempt, dismiss_overlays, guided_login, REGISTER_URL
        # Try real login paths first. NEVER a register / sign-up page: filling and submitting it creates an account.
        login_paths = [urljoin(self.site, lp.lstrip("/")) for lp in LOGIN_PATHS]
        pw_pages = [p.url for p in self.map.pages.values()
                    if any(self.map.elements[e].type == "password" for e in p.elements if e in self.map.elements)
                    and not REGISTER_URL.search(p.url)]
        cands = login_paths + [u for u in pw_pages if u not in login_paths]
        guided = self.login_chat is not None
        for url in cands:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                continue
            dismiss_overlays(page)                             # a cookie or consent overlay can hide the form
            try:                                               # a client-rendered (SPA) form appears after load
                page.wait_for_selector(_PASSWORD_FIELD, timeout=4000)
            except Exception:
                pass
            has_form = page.locator(_PASSWORD_FIELD).count() > 0
            if not has_form and not guided:
                continue                                       # no form and no model to find one: try the next page
            with write_guard.login_window(self.host):
                self._in_login = True
                how = ""
                try:
                    ok = _attempt(page, self.email, self.password) if has_form else False
                    if not ok and guided:                      # the standard form failed or is non-standard: use the model
                        ok = guided_login(page, self.email, self.password, self.notes, self.login_chat, log=self.log)
                        how = " (guided)" if ok else how
                except Exception as e:
                    self._in_login = False
                    self.login_result = f"LOGIN FAILED: could not use the form on {route_of(url)[0]} ({type(e).__name__})"
                    return False
                self._in_login = False
            if ok:
                self.login_result = f"LOGGED IN on {route_of(url)[0]}{how}"
                return True
            if has_form:
                self.login_result = f"LOGIN FAILED: the form on {route_of(url)[0]} is still shown"
                return False
        self.login_result = "LOGIN UNSUPPORTED: no login form found"
        return False

    def _check_links(self, ctx):
        """One result per link element: its target opens. Same-origin only."""
        for el in list(self.map.elements.values()):
            if el.kind != "link":
                continue
            p = self.map.page(el.page, el.auth)
            if p is None:
                continue
            shot = p.screenshot or "none"
            proof = {"screenshot": shot, "url": p.final_url or p.url}
            href = el.href
            label = el.name or el.label or "(no text)"
            click = self.clicked.get((el.page, el.name or ""), "not clicked")
            if (href is None or not href.strip() or href.strip() == "#") and click not in ("not clicked", None):
                self.board.add_result("code", f"link '{label}' on {el.page}", "PASS", {**proof, "server_responses": []},
                                      reason=f"Link '{label}' on {el.page} has no address but opens {click} when clicked "
                                             f"(script link).", expected="The link opens a page.", page=el.path,
                                      element_id=el.id, user_state=el.auth, kind="basic:link")
                continue
            if href is None or href.strip() == "#":
                # no address, or "#": often a menu or dialog toggle, not a page link. If a click did not open a
                # page it stays a gap for the testers instead of a FAIL (a toggle is not a broken link).
                continue
            if not href.strip():          # href="" is an authoring mistake: the link reloads the same page
                first = self._site_wide("links", f"empty|{label}", el.page)
                if first is None:
                    self.board.add_result("code", f"link '{label}' on {el.page}", "FAIL",
                                          {**proof, "server_responses": []},
                                          reason=f"Link '{label}' on {el.page} has an empty href, so it goes nowhere"
                                                 + (" (clicking it opened no page)." if click is None else "."),
                                          expected="The link has a target.", page=el.path, element_id=el.id,
                                          user_state=el.auth, kind="basic:link")
                else:
                    self.board.add_result("code", f"link '{label}' on {el.page}", "FAIL", {**proof, "server_responses": []},
                                          reason=f"Same empty link as on {first} (site-wide; reported once).",
                                          expected="The link has a target.", page=el.path, element_id=el.id,
                                          user_state=el.auth, kind="basic:link-dup")
                continue
            target = strip_fragment(urljoin(p.final_url or p.url, href))
            if href.strip().startswith(("javascript:", "mailto:", "tel:")) or not self._same_site(target) \
                    or DOWNLOAD.search(urlparse(target).path) or NO_FOLLOW.search(target) or NO_FOLLOW.search(label):
                continue          # not checked: stays a gap for the checker
            t_route = route_of(target)[0]
            status = self.status.get(target)
            if status is None and t_route in self.broken:
                status = self.broken[t_route]["status"]
            if status is None:
                try:
                    status = ctx.request.get(target, timeout=15000, max_redirects=5).status
                except Exception:
                    status = 0
                if status in (404, 410) and self.spa:
                    status = self._spa_status(ctx, target, status)
                self.status[target] = status
            resp = [{"url": target, "status": status}]
            if status in (404, 410) or status == 0:
                first = self._site_wide("links", f"broken|{label}|{t_route}", el.page)
                what = f"returns {status} Not Found" if status else "does not answer"
                reason = (f"Link '{label}' on {el.page} goes to {t_route}, which {what}." if first is None
                          else f"Same broken link as on {first} (site-wide; reported once).")
                self.board.add_result("code", f"link '{label}' on {el.page}", "FAIL", {**proof, "server_responses": resp},
                                      reason=reason, expected=f"The link '{label}' opens a page.", page=el.path,
                                      element_id=el.id, user_state=el.auth,
                                      kind="basic:link" if first is None else "basic:link-dup", risk=2)
            else:
                self.board.add_result("code", f"link '{label}' on {el.page}", "PASS", {**proof, "server_responses": resp},
                                      reason=f"Link '{label}' on {el.page} opens {t_route} (HTTP {status}).",
                                      expected=f"The link '{label}' opens a page.", page=el.path, element_id=el.id,
                                      user_state=el.auth, kind="basic:link")

    def _spa_status(self, ctx, target: str, status: int) -> int:
        """On a single-page app a 404 from the server is not proof: open the target and look at what renders."""
        pg = ctx.new_page()
        try:
            pg.goto(target, wait_until="domcontentloaded", timeout=15000)
            pg.wait_for_timeout(500)
            probe = pg.evaluate("() => ({ text: (document.body ? document.body.innerText : '').slice(0, 3000), "
                                "n: document.querySelectorAll('a, button, input, select, textarea').length })")
            return status if NOT_FOUND.search(probe["text"]) or probe["n"] < 3 else 200
        except Exception:
            return status
        finally:
            pg.close()

    def run(self) -> SiteMap:
        from playwright.sync_api import sync_playwright
        _default_browsers_path()
        self.log(f"Crawling {self.site} (write mode: {write_guard.mode_label()})")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(viewport={"width": 1280, "height": 800}, ignore_https_errors=True)
            ctx.route("**/*", self._guard)
            page = ctx.new_page()
            self._attach(page)
            left = self._bfs([self.site], "anon")
            if self.email and self.password and self._elapsed() < self.budget_s:
                ok = self._login(page) or self._login(page)      # one retry: a busy machine can time out
                self.log(f"  {self.login_result}")
                if ok:
                    self.user_storage = ctx.storage_state()       # the design checker and probes reuse this login
                    seeds = [strip_fragment(page.url)] + [p.url for p in list(self.map.pages.values()) if p.auth == "anon"]
                    left += self._bfs(seeds, "user", storage=self.user_storage)
            elif self.email and self.password:
                self.login_result = "LOGIN NOT TRIED: crawl time limit reached"
            self._check_links(ctx)
            browser.close()
        # pages found but not visited (time or page limit): they stay on the map without a result
        for u in dict.fromkeys(left):
            route, path = route_of(u)
            if not any(p.route == route for p in self.map.pages.values()):
                self.map.add_page(Page(route=route, path=path, url=u, auth="anon", status=0))
                self.map.notes.append(f"{route}: found but not visited (crawl limit)")
        if self.blocked:
            self.map.notes.append(f"write guard blocked {len(self.blocked)} request(s) during the crawl")
        self.map.notes.append(self.login_result)
        return self.map
