"""Tester workers: each one is a thread with its own browser, its own login and
a short, fresh model context for every task.

A tester claims one task from the board, opens the task's page and lets the
model act through a few tools (click, fill, select, check, press, goto,
observe, finish). Code, not the model, keeps the rules:

- Element IDs: an action must name an element that is on the site map AND on
  the page now. Elements that appear at run time (a dialog, a toast button) are
  added to the map only after code finds them in the page.
- Loop guard: the same action 3 times, more than MAX_STEPS steps, or more than
  the task time limit -> the task is released (back on the board once).
- Proof: code takes the screenshot, the landing URL and every server response
  of the task; the model cannot supply or skip them.
- Facts by code: a PASS with a server error (5xx) during the task becomes FAIL;
  a PASS or FAIL with a write that the guard blocked becomes BLOCKED.
- Write guard: every request of the tester's browser goes through
  write_guard.decide; only the tester's own login form may write.

Until the judge exists (build step 6), the result is judged by code rules only,
and a tester confidence under the board's threshold goes to a human (UNCERTAIN).
"""

import base64
import json
import os
import re
import threading
import time
from urllib.parse import urlparse

try:
    from ..tools import write_guard
except ImportError:
    from tools import write_guard
from . import llm
from .crawler import EXTRACT_JS, LOGIN_PATHS, _default_browsers_path, _host
from .sitemap import Page, route_of, strip_fragment

MAX_STEPS = 18
LOGOUT = re.compile(r"log[-_ ]?out|sign[-_ ]?out|logoff|delete[-_ ]?account", re.I)
TASK_SECONDS = 150
SEVERITY = {"FAIL": 3, "UNCERTAIN": 2, "BLOCKED": 1, "PASS": 0}
# A task whose expected result is one of these actions must persist: a success message with no successful
# server write (a non-GET with a 2xx status) is a success trap. "add" is left out (add-to-cart is often client only).
ACTION_VERB = re.compile(r"\b(create|save|send|submit|place|order|delete|remove|update|register|"
                         r"check\s?out|pay|purchase|upload)\b", re.I)

TOOLS = [
    {"type": "function", "function": {"name": "click", "description": "Click an element.",
     "parameters": {"type": "object", "properties": {"element_id": {"type": "string"}}, "required": ["element_id"]}}},
    {"type": "function", "function": {"name": "fill", "description": "Clear a text field and type text into it.",
     "parameters": {"type": "object", "properties": {"element_id": {"type": "string"}, "text": {"type": "string"}},
                    "required": ["element_id", "text"]}}},
    {"type": "function", "function": {"name": "select", "description": "Choose an option of a select box by its label.",
     "parameters": {"type": "object", "properties": {"element_id": {"type": "string"}, "option": {"type": "string"}},
                    "required": ["element_id", "option"]}}},
    {"type": "function", "function": {"name": "check", "description": "Tick (on=true) or untick a checkbox or radio.",
     "parameters": {"type": "object", "properties": {"element_id": {"type": "string"}, "on": {"type": "boolean"}},
                    "required": ["element_id", "on"]}}},
    {"type": "function", "function": {"name": "press", "description": "Press a key in the focused field, e.g. Enter.",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}}},
    {"type": "function", "function": {"name": "goto", "description": "Open a page of the site map by its path, e.g. /cart.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "observe", "description": "Read the page again: URL, messages, elements, text.",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "back", "description": "The browser's Back button.",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "reload", "description": "Reload the page (to check that data persists).",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "finish", "description": "Report the result of each check you made. Call once, at the end.",
     "parameters": {"type": "object", "properties": {
         "checks": {"type": "array", "items": {"type": "object", "properties": {
             "name": {"type": "string", "description": "what was checked, e.g. 'empty email is refused'"},
             "verdict": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED", "UNCERTAIN"]},
             "observed": {"type": "string", "description": "exactly what the page and the server showed"}},
             "required": ["name", "verdict", "observed"]}},
         "confidence": {"type": "number", "description": "0..1, how sure you are"},
         "follow_up": {"type": "string", "description": "optional note for another team, e.g. 'button works but the layout is broken'"}},
         "required": ["checks", "confidence"]}}},
]

UPLOAD_TOOL = {"type": "function", "function": {"name": "upload",
    "description": "Put one of the owner's test files into a file field.",
    "parameters": {"type": "object", "properties": {"element_id": {"type": "string"}, "file": {"type": "string"}},
                   "required": ["element_id", "file"]}}}

LOOK_TOOL = {"type": "function", "function": {"name": "look",
    "description": "See the page as a user does, with measured facts: sideways scroll, overlapping text, tiny text, broken images.",
    "parameters": {"type": "object", "properties": {"viewport": {"type": "string", "enum": ["desktop", "phone"]}},
                   "required": ["viewport"]}}}

TEAM_CFG = {
    "function": {"steps": 18, "seconds": 240, "rules": ""},
    "journey": {"steps": 30, "seconds": 600, "rules": """
YOU TEST A WHOLE JOURNEY across pages, like a real user. Follow the journey's steps in order; move with the
page's own links and buttons (use goto only to start, or when a step needs a page that no link reaches).
Check that data carries across pages (an item added to the cart is in the cart, a placed order is in the
orders list with the right total). Then try one disruption: back in the middle of the flow, or reload.
Report one check for each step of the journey, in order."""},
    "uiux": {"steps": 12, "seconds": 300, "rules": """
YOU CHECK HOW THE PAGE LOOKS AND FEELS, not the business logic. Call look with viewport "desktop", then with
"phone". Report: text or controls that overlap or are cut off, unreadable text, broken images, a main action
that is hard to find, sideways scrolling on the phone, confusing steps. Use the MEASURED facts; report only
what the screenshot or the measurements show. Do not judge taste or colour preferences."""},
}

MEASURE_JS = r"""() => {
  const vis = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && r.bottom > 0 && r.top < innerHeight * 3; };
  const els = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,a,button,label,li,span,td,th,input,select,textarea')]
    .filter(e => vis(e) && (e.innerText || e.value || '').trim()).slice(0, 300);
  const boxes = els.map(e => [e, e.getBoundingClientRect()]);
  const overlaps = [];
  for (let i = 0; i < boxes.length && overlaps.length < 6; i++) for (let j = i + 1; j < boxes.length; j++) {
    const [a, ra] = boxes[i], [b, rb] = boxes[j];
    if (a.contains(b) || b.contains(a)) continue;
    const w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left), h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
    if (w > 4 && h > 4 && w * h > 0.4 * Math.min(ra.width * ra.height, rb.width * rb.height))
      overlaps.push(((a.innerText || a.value || '').trim().slice(0, 30)) + ' / ' + ((b.innerText || b.value || '').trim().slice(0, 30))); }
  const tiny = els.filter(e => parseFloat(getComputedStyle(e).fontSize) < 11).length;
  const broken = [...document.images].filter(i => i.complete && i.naturalWidth === 0 && vis(i)).map(i => i.getAttribute('src')).slice(0, 5);
  return { width: innerWidth, sideways: document.documentElement.scrollWidth - innerWidth, overlaps, tiny_text: tiny, broken_images: broken };
}"""

SYSTEM = """You are a careful QA tester. You test ONE small task on a web page through tools, then call finish.
Rules:
- Act only on element IDs from the ELEMENTS list. Never guess an ID. Call observe after the page changes to get new IDs.
- Make up to 4 checks: the normal use first, then the edge cases most likely to break it (empty, wrong format,
  very long text, double click). Use made-up test data (test@example.com), never real personal data.
- A check PASSES only if the page or the server response shows the expected result. "No error seen" is not a pass:
  look for the change that should happen (a message, a new row, a new total, a new page).
- The screen can lie: compare the message with the server responses you are shown (method, path, status).
- Check numbers you can compute: a total must equal price x quantity (minus any discount), a count must match the
  rows shown, a sort must really be in order. A wrong number is a FAIL even when no error is shown.
- A blocked form submit can show only the browser's own message (BROWSER VALIDATION in MESSAGES): that is the
  form refusing the input, which is usually correct.
- After anything that saves data (a profile, a form, a cart), open the page again with goto and confirm the
  change is still there. A "Saved" message alone is not a PASS.
- FAIL: write exactly what you saw (text, URL, status). BLOCKED: you could not do the check (and why).
  UNCERTAIN: you are not sure what the right behaviour is.
- A tool ERROR or REJECTED is a problem of the test, not of the product: try another way, or report the check
  BLOCKED with the error. Never report FAIL because a tool failed.
- For a select box, pass one of the listed options exactly.
- The page is already open and its elements are listed below: start acting at once.
- Each action result shows what is NEW ON THE PAGE and the server responses: you do not need observe after
  every action. Call observe only when you need the element IDs of a page that changed.
- You may call several tools in one turn when they do not depend on each other (fill two fields, then click).
- Be brief. Do not explain your plan; act."""

ALERT_JS = r"""() => {
  const out = [...document.querySelectorAll('[role=alert], [role=status], [aria-live], .toast, .alert, .error, .success, .notice, dialog[open]')]
    .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
    .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 6);
  // The browser's own form validation shows a bubble that is not in the page text: report it.
  for (const f of document.querySelectorAll('input, select, textarea')) {
    if (f.willValidate && !f.checkValidity() && f.validationMessage && (f.value !== '' || f.dataset.qaTouched))
      out.push('BROWSER VALIDATION on ' + (f.name || f.id || f.type) + ': ' + f.validationMessage);
  }
  return out.slice(0, 8);
}"""


class Tester(threading.Thread):
    def __init__(self, name: str, team: str, board, sitemap, map_lock: threading.Lock, *, email=None, password=None,
                 deadline: float, stop: threading.Event, out_dir: str, log=print, chat=None, headless=True,
                 judge_mode: str = "code", files: list | None = None, notes: str = ""):
        super().__init__(name=name, daemon=True)
        self.team, self.board, self.map, self.map_lock = team, board, sitemap, map_lock
        self.email, self.password = email, password
        self.deadline, self.stop, self.out, self.log = deadline, stop, out_dir, log
        self.chat = chat or llm.chat
        self.headless = headless
        self.judge_mode = judge_mode      # "code": judge at once by code rules; "judge": leave it for the judge
        self.cfg = TEAM_CFG.get(team, TEAM_CFG["function"])
        self.files = {os.path.basename(f): f for f in (files or [])}      # name -> path; the model sees names only
        self.notes = notes or ""
        self.tools = TOOLS + ([LOOK_TOOL] if team == "uiux" else []) + ([UPLOAD_TOOL] if self.files else [])
        self._image = None
        self.host = _host(sitemap.site_url)
        self.done = 0
        self._ctx = {}
        self._logging_in = set()
        self._net: list[dict] = []
        self._blocked: list[dict] = []
        self._console: list[str] = []
        self._clicked_logout = False
        self._actions: list[dict] = []
        self._alerts: list[str] = []
        os.makedirs(os.path.join(out_dir, "evidence"), exist_ok=True)

    def _live_notes(self) -> str:
        """Read the owner's live chat notes for this run (added through the UI while the run is active).
        The tester reads the file fresh before each task, so new guidance changes the tests that follow."""
        try:
            with open(os.path.join(self.out, "live_notes.txt"), encoding="utf-8", errors="replace") as f:
                return f.read().strip()[:4000]
        except OSError:
            return ""

    # ---------- browser ----------
    def _route(self, auth):
        def handler(route, request):
            try:
                body = request.post_data                 # raises on a non-UTF-8 (binary/upload) body
            except Exception:
                body = None
            reason = write_guard.decide(request.method, request.url, body)
            same = _host(request.url) == self.host
            # The login form may write to its own endpoint: a login opens a session, it changes no data.
            # The paths come from the crawler's own login (map.login_posts), not from the model.
            login_post = same and urlparse(request.url).path in self.map.login_posts
            if reason and not ((auth in self._logging_in or login_post) and same):
                self._blocked.append({"method": request.method, "path": urlparse(request.url).path})
                return route.abort()
            return route.continue_()
        return handler

    def _browser_ctx(self, browser, auth: str):
        if auth not in self._ctx:
            ctx = browser.new_context(viewport={"width": 1280, "height": 800}, ignore_https_errors=True)
            ctx.route("**/*", self._route(auth))
            self._ctx[auth] = ctx
            if auth == "user" and not self._login(ctx):
                return None
        return self._ctx[auth]

    def _login(self, ctx) -> bool:
        try:
            from ..tools.site_login import _PASSWORD_FIELD, _attempt, dismiss_overlays, guided_login, REGISTER_URL
        except ImportError:
            from tools.site_login import _PASSWORD_FIELD, _attempt, dismiss_overlays, guided_login, REGISTER_URL
        if not (self.email and self.password):
            return False
        page = ctx.new_page()
        guided = self.chat is not None
        try:
            login_paths = [self.map.site_url.rstrip("/") + lp for lp in LOGIN_PATHS]
            pw_pages = [p.url for p in self.map.pages.values() if p.auth == "anon"
                        and any((self.map.elements.get(e) and self.map.elements[e].type == "password") for e in p.elements)
                        and not REGISTER_URL.search(p.url)]
            cands = login_paths + [u for u in pw_pages if u not in login_paths]
            for url in cands:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    continue
                dismiss_overlays(page)
                try:                                           # a client-rendered (SPA) form appears after load
                    page.wait_for_selector(_PASSWORD_FIELD, timeout=4000)
                except Exception:
                    pass
                has_form = page.locator(_PASSWORD_FIELD).count() > 0
                if not has_form and not guided:
                    continue
                self._logging_in.add("user")
                try:
                    if has_form and _attempt(page, self.email, self.password):
                        return True
                    if guided and guided_login(page, self.email, self.password, self.notes, self.chat, log=self.log):
                        return True
                    if has_form:
                        return False
                except Exception:
                    return False
                finally:
                    self._logging_in.discard("user")
            return False
        finally:
            page.close()

    def _watch(self, page):
        def on_response(resp):
            req = resp.request
            if req.method != "GET" or req.resource_type in ("document", "fetch", "xhr") or resp.status >= 400:
                if self._same(resp.url):
                    self._net.append({"method": req.method, "path": urlparse(resp.url).path, "status": resp.status,
                                      "type": req.resource_type})
        page.on("response", on_response)
        page.on("console", lambda m: m.type == "error" and self._console.append(m.text[:200]))
        page.on("pageerror", lambda e: self._console.append(("uncaught: " + str(e))[:200]))

    def _same(self, url: str) -> bool:
        return _host(url) == self.host

    # ---------- the page as the model sees it ----------
    def _scan(self, page, auth: str) -> dict:
        """Live elements of the page, with map IDs. New ones go on the map (they exist now)."""
        try:
            data = page.evaluate(EXTRACT_JS)
        except Exception:
            data = {"elements": [], "text": ""}
        route, path = route_of(strip_fragment(page.url))
        tmp = Page(route=route, path=path, url=page.url, auth=auth, status=200)
        with self.map_lock:
            known = self.map.page(route, auth) or next((p for p in self.map.pages.values()
                                                        if p.path == path and p.auth == auth), None)
            before = set(self.map.elements)
            els = self.map.add_elements(tmp, data.get("elements", []))
            for e in els:
                if e.id not in before:
                    self.map.notes.append(f"runtime element {e.id} ({e.kind} '{e.name}') found on {path} by {self.name}")
            if known is not None:
                for e in els:
                    if e.id not in known.elements:
                        known.elements.append(e.id)
        sel = {e.id: e.selector for e in els}
        lines = []
        for e in els[:90]:
            if e.kind == "form":
                continue
            bits = [e.id, e.kind]
            if e.type and e.kind in ("input", "button"):
                bits.append(f"type={e.type}")
            txt = e.name or e.label or e.field
            if txt:
                bits.append(f'"{txt[:50]}"')
            if e.label and e.label != e.name:
                bits.append(f'label="{e.label[:40]}"')
            if e.kind == "link" and e.href is not None:
                bits.append(f"-> {e.href[:60]}")
            if e.kind == "select" and e.options:
                bits.append("options: " + " / ".join(o[:30] for o in e.options[:12]))
            lines.append(" | ".join(bits))
        try:
            alerts = page.evaluate(ALERT_JS)
        except Exception:
            alerts = []
        return {"url": page.url, "path": path, "title": _safe(lambda: page.title(), ""), "elements": lines,
                "selectors": sel, "alerts": alerts, "text": (data.get("text") or "")[:2200]}

    def _render(self, obs: dict, net_from: int, cons_from: int, full: bool) -> str:
        net = [f"{n['method']} {n['path']} -> {n['status']}" for n in self._net[net_from:]][-12:]
        out = [f"URL: {obs['url']}", f"TITLE: {obs['title']}"]
        if obs["alerts"]:
            out.append("MESSAGES ON SCREEN: " + " || ".join(a[:160] for a in obs["alerts"]))
        out.append("SERVER RESPONSES SINCE LAST STEP: " + ("; ".join(net) if net else "none"))
        blocked = [f"{b['method']} {b['path']}" for b in self._blocked]
        if blocked:
            out.append("WRITES BLOCKED BY THE READ-ONLY GUARD: " + "; ".join(blocked[-5:]))
        if self._console[cons_from:]:
            out.append("CONSOLE ERRORS: " + " | ".join(self._console[cons_from:][-3:]))
        if full:
            out.append("ELEMENTS:\n" + "\n".join(obs["elements"]))
            out.append("PAGE TEXT:\n" + obs["text"])
        return "\n".join(out)

    # ---------- one task ----------
    def _act(self, page, obs, name, args) -> str:
        if name == "goto":
            path = "/" + str(args.get("path", "")).lstrip("/")
            if not any(p.path == path.split("?")[0] for p in self.map.pages.values()):
                return f"REJECTED: {path} is not a page on the site map."
            page.goto(self.map.site_url.rstrip("/") + path, wait_until="domcontentloaded", timeout=20000)
            return "opened"
        if name == "press":
            page.keyboard.press(str(args.get("key", "Enter"))[:20])
            return "pressed"
        if name == "observe":
            return "the page now"
        if name == "back":
            page.go_back(wait_until="domcontentloaded", timeout=15000)
            return "went back"
        if name == "reload":
            page.reload(wait_until="domcontentloaded", timeout=20000)
            return "reloaded"
        if name == "look":
            vp = "phone" if str(args.get("viewport", "desktop")) == "phone" else "desktop"
            page.set_viewport_size({"width": 390, "height": 844} if vp == "phone" else {"width": 1280, "height": 800})
            page.wait_for_timeout(500)
            m = page.evaluate(MEASURE_JS)
            shot = os.path.join(self.out, "evidence", f"{self._task_id}_{vp}.png")
            page.screenshot(path=shot, timeout=10000)
            self._image = (vp, shot)
            return (f"{vp} view {m['width']}px wide. MEASURED: sideways scroll {max(0, m['sideways'])}px; "
                    f"overlapping text: {'; '.join(m['overlaps']) or 'none'}; tiny text (<11px): {m['tiny_text']}; "
                    f"broken images: {', '.join(m['broken_images']) or 'none'}. The screenshot follows.")
        eid = str(args.get("element_id", ""))
        sel = obs["selectors"].get(eid)
        if sel is None:
            on_map = self.map.element(eid) is not None
            return (f"REJECTED: {eid} is " + ("on the map but not on this page now; call observe." if on_map
                                              else "not on the site map. Use only IDs from ELEMENTS."))
        if name in ("click", "check", "select"):
            try:
                n = page.locator(sel).count()
            except Exception:
                n = 1
            if n > 1:
                return (f"REJECTED: {eid} now matches {n} elements, so the target is ambiguous. Call observe to "
                        "refresh the IDs, then act on the exact one. Do not guess which one.")
        loc = page.locator(sel).first
        if name == "upload":
            f = self.files.get(str(args.get("file", "")))
            if f is None:
                return "REJECTED: unknown file. Files: " + ", ".join(self.files) if self.files else "REJECTED: no files given"
            loc.set_input_files(f, timeout=8000)
            return f"file {os.path.basename(f)} put in the field"
        if name == "click":
            el = self.map.element(eid)
            if el is not None and LOGOUT.search(f"{el.name} {el.href or ''}"):
                self._clicked_logout = True
            loc.click(timeout=8000)
            try:           # after a click (often a submit), empty required fields report their browser message too
                page.evaluate("() => document.querySelectorAll('input, select, textarea').forEach(f => f.dataset.qaTouched = '1')")
            except Exception:
                pass
        elif name == "fill":
            loc.fill(str(args.get("text", ""))[:2000], timeout=8000)
        elif name == "select":
            loc.select_option(label=str(args.get("option", "")), timeout=8000)
        elif name == "check":
            loc.set_checked(bool(args.get("on", True)), timeout=8000)
        else:
            return f"REJECTED: unknown tool {name}"
        return "done"

    def _settle(self, page):
        # Pages that poll the server never go network-idle, so this wait often runs to its limit: keep it
        # short. A result that comes later is still seen with observe.
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:
            pass
        page.wait_for_timeout(300)

    def run_task(self, browser, task: dict) -> None:
        auth = task["user_state"]
        ctx = self._browser_ctx(browser, auth)
        if ctx is None:
            self._submit(task, None, [{"name": "log in", "verdict": "BLOCKED",
                                       "observed": "the tester could not log in with the test account"}], 1.0, None)
            return
        self._net, self._blocked, self._console, self._actions, self._alerts = [], [], [], [], []
        self._task_id = task["id"]
        page = ctx.new_page()
        self._watch(page)
        started = time.monotonic()
        try:
            el = self.map.element(task["element_id"]) if task["element_id"] else None
            start = self._start_url(task, el)
            page.goto(start, wait_until="domcontentloaded", timeout=20000)
            self._settle(page)
            if auth == "user" and any(urlparse(page.url).path.startswith(lp) for lp in LOGIN_PATHS) \
                    and not any(urlparse(start).path.startswith(lp) for lp in LOGIN_PATHS):
                self._ctx.pop("user", None)
                ctx.close()
                ctx = self._browser_ctx(browser, auth)
                if ctx is None:
                    raise _Stop("the test account is logged out and the login failed")
                page = ctx.new_page()
                self._watch(page)
                page.goto(start, wait_until="domcontentloaded", timeout=20000)
                self._settle(page)
            obs = self._scan(page, auth)
            target = ""
            if el is not None:
                target = f"TARGET ELEMENT: {el.id} ({el.kind} '{el.name or el.label or el.field}')"
                if el.id not in obs["selectors"]:
                    target += " - it is not on the page now; call observe after any step that should show it"
            steps = task["steps"] if isinstance(task["steps"], list) else []
            live = self._live_notes()
            msgs = [{"role": "system", "content": SYSTEM + self.cfg["rules"]},
                    {"role": "user", "content": f"TASK: {task['title']}\nEXPECTED: {task['expected']}\n"
                     + (f"COVERS: {', '.join(steps)}\n" if steps else "") + f"{target}\n"
                     f"LOGIN STATE: {'logged in as the test user' if auth == 'user' else 'logged out'}\n"
                     f"WRITE MODE: {write_guard.mode_label()} - {write_guard.mode_help()}\n"
                     + (f"OWNER LIVE NOTES (follow these instructions): {live}\n" if live else "")
                     + (f"TEST FILES YOU MAY UPLOAD: {', '.join(self.files)}\n" if self.files else
                        "TEST FILES: none given - an upload check is BLOCKED (no file given)\n")
                     + "\n" + self._render(obs, 0, 0, True)}]
            seen: dict[str, tuple] = {}
            for step in range(self.cfg["steps"]):
                if time.monotonic() - started > self.cfg["seconds"]:
                    raise _Stop(f"task time limit ({self.cfg['seconds']}s)")
                _compact(msgs)
                msg = self.chat(self.team, msgs, self.tools, timeout=90 if self.team == "uiux" else 45)
                calls = getattr(msg, "tool_calls", None) or []
                msgs.append(_as_dict(msg))
                if not calls:
                    msgs.append({"role": "user", "content": "Use a tool. Call finish when the checks are done."})
                    continue
                for c in calls:
                    name = c.function.name
                    try:
                        args = json.loads(c.function.arguments or "{}")
                    except ValueError:
                        args = {}
                    if name == "finish":
                        self._save_transcript(task, msgs, "finish", args)
                        self._submit(task, page, args.get("checks") or [], args.get("confidence", 0.5),
                                     args.get("follow_up"))
                        return
                    # a repeat counts only if the page did not change since the same action last ran
                    page_sig = (obs["url"], tuple(obs["alerts"]), len(obs["elements"]), hash(obs["text"]), len(self._net))
                    sig = name + json.dumps(args, sort_keys=True)
                    if seen.get(sig, (None, 0))[0] == page_sig:
                        seen[sig] = (page_sig, seen[sig][1] + 1)
                    else:
                        seen[sig] = (page_sig, 1)
                    if seen[sig][1] >= 3:
                        raise _Stop(f"loop guard: '{name}' with the same input 3 times on an unchanged page")
                    warn = ("\nNOTE: you did exactly this before and the page did not change. Do not repeat it. "
                            "If it should change something, that is a FAIL: record it and call finish."
                            if seen[sig][1] == 2 else "")
                    net0, cons0 = len(self._net), len(self._console)
                    before = set(l.strip() for l in obs["text"].splitlines() if l.strip())
                    prev_url, prev_alerts = obs["url"], obs["alerts"]
                    try:
                        res = self._act(page, obs, name, args)
                    except Exception as e:
                        res = f"ERROR: {type(e).__name__}: {str(e).splitlines()[0][:160]}"
                    if not res.startswith("REJECTED"):
                        self._settle(page)
                    obs = self._scan(page, auth)
                    el_a = self.map.element(str(args.get("element_id", ""))) if args.get("element_id") else None
                    self._actions.append({"tool": name, "element": args.get("element_id"),
                                          "name": (el_a.name or el_a.label or el_a.field) if el_a else "",
                                          "input": args.get("text", args.get("option", args.get("path", args.get("key", "")))),
                                          "result": res[:100], "url_after": obs["url"], "messages": obs["alerts"][:3],
                                          "server": [f"{n['method']} {n['path']} -> {n['status']}" for n in self._net[net0:]][:6]})
                    self._alerts = obs["alerts"]
                    full = name in ("observe", "goto") or res.startswith("REJECTED") or urlparse(obs["url"]).path != obs["path"]
                    new_lines = [l.strip() for l in obs["text"].splitlines() if l.strip() and l.strip() not in before]
                    gone = len(before - set(l.strip() for l in obs["text"].splitlines() if l.strip()))
                    change = ("\nNEW ON THE PAGE: " + " | ".join(new_lines)[:600] if new_lines else "\nNEW ON THE PAGE: nothing")
                    change += f" ({gone} line(s) of text gone)" if gone else ""
                    if (not new_lines and not gone and len(self._net) == net0 and len(self._console) == cons0
                            and obs["url"] == prev_url and obs["alerts"] == prev_alerts
                            and not res.startswith(("REJECTED", "ERROR")) and name in ("click", "press", "fill", "select", "check")):
                        change += ("\nNOTE: this action changed nothing - no new text, no server response, no console "
                                   "message, same URL and messages. If this control is meant to act, that is a FAIL: "
                                   "record it. If it is a no-op by design, say so.")
                    msgs.append({"role": "tool", "tool_call_id": c.id,
                                 "content": f"{name}: {res}{warn}{change}\n" + self._render(obs, net0, cons0, full or name == "observe")})
                    if self._image:
                        vp, shot = self._image
                        self._image = None
                        with open(shot, "rb") as f:
                            b64 = base64.b64encode(f.read()).decode()
                        msgs.append({"role": "user", "content": [{"type": "text", "text": f"Screenshot, {vp} view:"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]})
            raise _Stop(f"step limit ({self.cfg['steps']} steps) without finish")
        except _Stop as e:
            self._save_transcript(task, locals().get("msgs"), f"stopped: {e}")
            self.log(f"  [{self.name}] {task['id']} stopped: {e}")
            self.board.release(task["id"], self.name, str(e))
        except Exception as e:
            self._save_transcript(task, locals().get("msgs"), f"error: {type(e).__name__}: {e}")
            self.log(f"  [{self.name}] {task['id']} error: {type(e).__name__}: {str(e)[:160]}")
            self.board.release(task["id"], self.name, f"tester error: {type(e).__name__}")
        finally:
            try:
                page.close()
            except Exception:
                pass
            # A task that logged out (or deleted the account) leaves the shared login dead for the next task:
            # throw the context away; the next "user" task logs in again.
            if auth == "user" and (any(LOGOUT.search(n["path"]) for n in self._net) or self._clicked_logout):
                ctx = self._ctx.pop("user", None)
                if ctx is not None:
                    try:
                        ctx.close()
                    except Exception:
                        pass
            self._clicked_logout = False

    def _save_transcript(self, task, msgs, end: str, finish_args=None) -> None:
        """The full model conversation of a task, next to its screenshot (audit and debugging)."""
        try:
            with open(os.path.join(self.out, "evidence", f"{task['id']}.{task['attempts']}.json"), "w", encoding="utf-8") as f:
                json.dump({"task": {k: task[k] for k in ("id", "title", "expected", "page", "user_state")},
                           "tester": self.name, "end": end, "finish": finish_args, "messages": msgs or []},
                          f, indent=1, default=str)
        except Exception:
            pass

    def _start_url(self, task, el) -> str:
        path = el.page if el is not None else task["page"]
        p = self.map.page(path, task["user_state"]) or next(
            (q for q in self.map.pages.values() if q.path == task["page"] and q.auth == task["user_state"]), None)
        return p.url if p else self.map.site_url.rstrip("/") + (path or "/")

    def _submit(self, task, page, checks: list, confidence, follow_up) -> None:
        checks = [c for c in checks if isinstance(c, dict)]
        verdicts = [str(c.get("verdict", "UNCERTAIN")).upper() for c in checks] or ["UNCERTAIN"]
        verdict = max(verdicts, key=lambda v: SEVERITY.get(v, 2))
        notes = []
        server_errors = [n for n in self._net if n["status"] >= 500]
        if verdict in ("PASS", "UNCERTAIN") and server_errors:
            e = server_errors[0]
            verdict = "FAIL"
            notes.append(f"code: the server answered {e['status']} to {e['method']} {e['path']} during this task, "
                         f"although the tester saw no failure")
        if verdict in ("PASS", "FAIL") and self._blocked:
            b = self._blocked[0]
            verdict = "BLOCKED"
            notes.append(f"code: the read-only guard blocked {b['method']} {b['path']}, so the result is BLOCKED")
        expected_write = bool(ACTION_VERB.search(f"{task.get('title', '')} {task.get('expected', '')}"))
        writes_ok = any(n["method"] != "GET" and 200 <= n["status"] < 300 for n in self._net)
        if verdict == "PASS" and expected_write and not writes_ok and not self._blocked:
            verdict = "UNCERTAIN"
            notes.append("code: the task expects a write (create/save/send/order/...), but no successful server "
                         "write (a non-GET with a 2xx status) was seen; a success message without a write is a "
                         "success trap - a human checks")
        shot = ""
        url = ""
        if page is not None:
            shot = os.path.join(self.out, "evidence", f"{task['id']}.png")
            try:
                page.screenshot(path=shot, timeout=10000)
            except Exception:
                shot = ""
            url = page.url
        observed = {"screenshot": shot, "url": url, "server_responses": list(self._net), "checks": checks,
                    "blocked_writes": list(self._blocked), "console": self._console[-5:], "code_notes": notes,
                    "code_verdict": verdict if notes else None, "actions": list(self._actions),
                    "messages_at_end": list(self._alerts),
                    "expected_write": expected_write, "had_successful_write": writes_ok}
        try:
            conf = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            conf = 0.5
        try:
            status = self.board.submit(task["id"], self.name, observed, verdict, conf, follow_up or None)
        except Exception as e:          # the claim expired while the tester worked: the result is dropped
            self.log(f"  [{self.name}] {task['id']} result dropped: {e}")
            return
        if status == "done" and self.judge_mode == "judge":
            self.done += 1
            self.log(f"  [{self.name}] {task['id']} done (tester says {verdict}), waiting for the judge: {task['title'][:60]}")
        elif status == "done":
            reason = "; ".join(f"{c.get('name', '')}: {c.get('verdict', '')} - {c.get('observed', '')}"
                               for c in checks)[:900]
            v = self.board.judge(task["id"], verdict, ("; ".join(notes) + " | " if notes else "") + reason
                                 + " [code rules only: the AI judge comes in build step 6]", conf)
            self.done += 1
            self.log(f"  [{self.name}] {task['id']} {v}: {task['title'][:70]}")

    # ---------- the worker loop ----------
    def run(self):
        from playwright.sync_api import sync_playwright
        _default_browsers_path()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                while not self.stop.is_set():
                    if time.time() + self.cfg["seconds"] > self.deadline:
                        break
                    task = self.board.claim(self.team, self.name, ttl=self.cfg["seconds"] + 60)
                    if task is None:
                        self.stop.wait(1.0)
                        continue
                    self.run_task(browser, task)
            finally:
                browser.close()


class _Stop(Exception):
    pass


def _compact(msgs: list, keep: int = 3, limit: int = 45000) -> None:
    """Keep the context short only when it is long. Providers cache the start of a conversation (DeepSeek
    re-reads a cached prefix fast), so the history is changed only once it passes `limit` characters: then
    tool results older than the last `keep` are cut to their first lines and old screenshots are dropped."""
    size = sum(len(m.get("content") or "") if isinstance(m.get("content"), str) else 20000 for m in msgs)
    if size < limit:
        return
    tools = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    for i in tools[:-keep]:
        c = msgs[i].get("content") or ""
        if len(c) > 600:
            head = [l for l in c.split("\n") if l.startswith(("URL:", "MESSAGES", "SERVER", "WRITES", "CONSOLE"))]
            msgs[i]["content"] = c.split("\n", 1)[0] + "\n" + "\n".join(head)[:500] + "\n[older detail cut]"
    imgs = [i for i, m in enumerate(msgs) if m.get("role") == "user" and isinstance(m.get("content"), list)]
    for i in imgs[:-2]:
        msgs[i]["content"] = "[an earlier screenshot, removed to keep the context short]"


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def _as_dict(msg) -> dict:
    d = {"role": "assistant", "content": getattr(msg, "content", None) or ""}
    calls = getattr(msg, "tool_calls", None)
    if calls:
        d["tool_calls"] = [{"id": c.id, "type": "function",
                            "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                           for c in calls]
    return d
