"""The planner: what the product is, the guessed demo path, risk per page, and small tasks.

Few model calls (llm.TEAM_MODELS["planner"], a reasoning model):
1. overview(): one call with the pages, titles, the start of each page's text and the forms.
   -> product type, short summary, guessed demo journeys, risk per page (high / medium / low).
2. page_tasks(): one call per high- or medium-risk page, in parallel -> small tasks with edge cases.
   Every element ID a task names must be on that page in the site map; code drops any other ID, so
   the planner cannot invent a button.
3. From the demo journeys: journey tasks (journey team) and look tasks (UI and UX team).
High-risk tasks go on the board first. Without a model key the planner does nothing and the
checker's gap tasks remain (plain-code risk guess).
"""

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from . import llm
from .checker import risk_of

RISK = {"high": 3, "medium": 2, "low": 1}

OVERVIEW_SYSTEM = """You are the test planner for a web product that will be shown to a client in a demo.
From the site map, decide: what kind of product this is; the journeys a client would most likely see in a
demo (the first page, login, the main screen, the main action - pages one or two clicks from the start and
the big call-to-action buttons rank highest); and the risk of every page:
- high: payments, checkout, orders, login, sign up, permissions, deleting data, and the demo journeys
- medium: search, filters, forms, account settings
- low: static pages, help, about, footer links
Use only page paths from the map."""

OVERVIEW_TOOL = [{"type": "function", "function": {"name": "plan", "description": "The overview plan.",
    "parameters": {"type": "object", "properties": {
        "product_type": {"type": "string"}, "summary": {"type": "string", "description": "one or two sentences"},
        "demo_journeys": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "pages": {"type": "array", "items": {"type": "string"}},
            "steps": {"type": "array", "items": {"type": "string"}, "description": "what the user does, in order"},
            "needs_login": {"type": "boolean"}},
            "required": ["name", "pages", "steps"]}},
        "page_risk": {"type": "array", "items": {"type": "object", "properties": {
            "path": {"type": "string"}, "risk": {"type": "string", "enum": ["high", "medium", "low"]},
            "why": {"type": "string"}}, "required": ["path", "risk"]}}},
        "required": ["product_type", "summary", "demo_journeys", "page_risk"]}}}]

PAGE_SYSTEM = """You plan the tests of ONE page of a web product. Write small tasks, each with a clear finish line,
for the features on this page that a client could touch. For each task give the element IDs it uses (only IDs
from the list), the expected result, and the 2-3 edge cases most likely to break it - pick from: empty, very long,
special characters, wrong format, boundary numbers (0, negative, huge), double submit, back button mid-flow,
reload after save, logged out. Do not write a task for plain navigation links (code already checks them).
Write the expected result as what a user would see happen (a message, a new row, a changed total). Do not
guess exact URLs, redirects or wording that the page does not show: you only see the page, not the spec.
Never invent a success message, a validation text or a record ID. Do not plan sign-in, sign-up or
password-recovery pages as test targets; they are entry points, not product features to break.
At most 4 tasks; the most important first. Skip "Log out" and plain header/footer controls (tested elsewhere)."""

PAGE_TOOL = [{"type": "function", "function": {"name": "tasks", "description": "The tasks for this page.",
    "parameters": {"type": "object", "properties": {"tasks": {"type": "array", "items": {"type": "object", "properties": {
        "title": {"type": "string", "description": "e.g. 'coupon field: valid, invalid, empty'"},
        "element_ids": {"type": "array", "items": {"type": "string"}},
        "expected": {"type": "string"},
        "edge_cases": {"type": "array", "items": {"type": "string"}},
        "risk": {"type": "string", "enum": ["high", "medium", "low"]}},
        "required": ["title", "element_ids", "expected", "edge_cases"]}}}, "required": ["tasks"]}}}]


def _call(chat, system, user, tool, name, max_tokens=6000, team="planner"):
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for _ in range(2):
        msg = chat(team, msgs, tool, max_tokens=max_tokens, timeout=120 if team == "planner" else 60)
        for c in getattr(msg, "tool_calls", None) or []:
            if c.function.name == name:
                try:
                    return json.loads(c.function.arguments or "{}")
                except ValueError:
                    pass
        msgs.append({"role": "user", "content": f"Answer with the {name} tool."})
    return None


def _pages(sitemap) -> list:
    """One entry per path: the logged-in version when there is one (it shows more)."""
    best = {}
    for p in sitemap.pages.values():
        if not p.status or p.status >= 400:
            continue
        cur = best.get(p.path)
        if cur is None or (p.auth == "user" and cur.auth != "user") or (p.auth == cur.auth and len(p.elements) > len(cur.elements)):
            best[p.path] = p
    return list(best.values())


def _element_lines(sitemap, page, limit=80) -> list[str]:
    out = []
    for eid in page.elements:
        e = sitemap.elements.get(eid)
        if e is None:
            continue
        bits = [e.id, e.kind]
        if e.type:
            bits.append(f"type={e.type}")
        txt = e.name or e.label or e.field
        if txt:
            bits.append(f'"{txt[:50]}"')
        if e.form >= 0:
            bits.append(f"form#{e.form}")
        if e.kind == "link" and e.href:
            bits.append(f"-> {e.href[:50]}")
        if e.options:
            bits.append("options: " + " / ".join(e.options[:8]))
        out.append(" | ".join(bits))
    return out[:limit]


class Planner:
    def __init__(self, board, sitemap, chat=None, log=print, docs: str = "", max_pages: int = 10,
                 have_login: bool = False, two_accounts: bool = False, allow_writes: bool = False):
        self.board, self.map, self.chat, self.log = board, sitemap, chat or llm.chat, log
        self.docs, self.max_pages = docs, max_pages
        self.have_login, self.two_accounts, self.allow_writes = have_login, two_accounts, allow_writes
        self.plan: dict = {}
        self.lock = threading.Lock()
        self.seen_controls: set = set()
        self.n_tasks, self.max_tasks = 0, 30
        self.done = threading.Event()

    # ---------- 1 overview ----------
    def overview(self) -> dict:
        lines = []
        for p in _pages(self.map):
            forms = [self.map.elements[e] for e in p.elements if e in self.map.elements and self.map.elements[e].kind in
                     ("input", "select", "textarea", "button", "checkbox")]
            names = ", ".join(sorted({(e.name or e.label or e.field)[:30] for e in forms if (e.name or e.label or e.field)}))[:300]
            lines.append(f"PAGE {p.path} ({'logged in' if p.auth == 'user' else 'public'}) title='{p.title[:60]}'\n"
                         f"  controls: {names or '-'}\n  text: {(p.text or '').replace(chr(10), ' ')[:300]}")
        user = f"SITE: {self.map.site_url}\n\n" + "\n".join(lines)
        if self.docs:
            user += ("\n\nOWNER NOTES AND INSTRUCTIONS (login steps, fields the page needs, what to focus on, and "
                     "things to be careful about - follow these):\n" + self.docs[:8000])
        out = _call(self.chat, OVERVIEW_SYSTEM, user, OVERVIEW_TOOL, "plan") or {}
        paths = {p.path for p in _pages(self.map)}
        risk = {r["path"]: r.get("risk", "low") for r in out.get("page_risk", []) if isinstance(r, dict) and r.get("path") in paths}
        for path in paths:                      # pages the model left out get the plain-code guess
            risk.setdefault(path, {3: "high", 2: "medium"}.get(risk_of(path), "low"))
        journeys = []
        for j in out.get("demo_journeys", [])[:6]:
            if not isinstance(j, dict):
                continue
            pages = [x for x in j.get("pages", []) if x in paths]
            if pages:
                journeys.append({"name": str(j.get("name", "journey"))[:80], "pages": pages,
                                 "steps": [str(s)[:160] for s in j.get("steps", [])][:12],
                                 "needs_login": bool(j.get("needs_login"))})
        self.plan = {"product_type": str(out.get("product_type", "unknown"))[:80],
                     "summary": str(out.get("summary", ""))[:400], "demo_journeys": journeys, "page_risk": risk,
                     "from_docs": bool(self.docs), "model_ok": bool(out)}
        self.board.set_meta("plan", self.plan)
        self.log(f"  planner: {self.plan['product_type']}; {len(journeys)} demo journey(s); "
                 f"{sum(v == 'high' for v in risk.values())} high-risk page(s)")
        return self.plan

    # ---------- 2 page tasks ----------
    def page_tasks(self, page) -> int:
        lines = _element_lines(self.map, page)
        if not lines:
            return 0
        risk = self.plan["page_risk"].get(page.path, "medium")
        user = (f"PRODUCT: {self.plan.get('product_type')} - {self.plan.get('summary')}\nPAGE: {page.path} "
                f"({'logged in' if page.auth == 'user' else 'public'}), risk {risk}\nTITLE: {page.title}\n"
                f"ELEMENTS:\n" + "\n".join(lines) + f"\nTEXT:\n{(page.text or '')[:1200]}")
        out = _call(self.chat, PAGE_SYSTEM, user, PAGE_TOOL, "tasks", team="planner_pages", max_tokens=2500) or {}
        n = 0
        on_page = set(page.elements)
        for t in (out.get("tasks") or [])[:4]:
            if not isinstance(t, dict):
                continue
            ids = [i for i in t.get("element_ids", []) if i in on_page]      # the planner cannot invent elements
            if not ids:
                continue
            sig = frozenset(_control_key(self.map.elements[i]) for i in ids)
            with self.lock:
                if sig in self.seen_controls or self.n_tasks >= self.max_tasks:
                    continue                    # the same controls on another page, or the task cap
                self.seen_controls.add(sig)
                self.n_tasks += 1
            r = RISK.get(t.get("risk") or risk, 2)
            r = max(r, RISK.get(risk, 1)) if risk == "high" else r
            with self.lock:
                self.board.add("function", str(t.get("title", "task"))[:140], page=page.path, element_id=ids[0],
                               covers=ids[1:], user_state=page.auth, kind="planned", risk=r, source="planner",
                               steps=[str(e)[:80] for e in t.get("edge_cases", [])][:3],
                               expected=str(t.get("expected", ""))[:400])
            n += 1
        return n

    # ---------- 3 journeys and looks ----------
    def journey_and_look_tasks(self) -> int:
        n = 0
        for j in self.plan.get("demo_journeys", []):
            auth = "user" if j["needs_login"] or any(q.path in j["pages"] and q.auth == "user" for q in self.map.pages.values()
                                                    if q.status and q.status < 400 and not self.map.page(q.route, "anon")) \
                else "anon"
            self.board.add("journey", f"Journey: {j['name']}", page=j["pages"][0], user_state=auth, kind="journey",
                           risk=3, source="planner", steps=j["steps"] + [f"pages: {' > '.join(j['pages'])}"],
                           expected="Every step works, data carries across the pages, and the flow ends where the "
                                    "user expects; reload and back do not break it.")
            n += 1
        demo_pages = []
        for j in self.plan.get("demo_journeys", []):
            for p in j["pages"]:
                if p not in demo_pages:
                    demo_pages.append(p)
        for path in (demo_pages or ["/"])[:4]:
            # the logged-in version when there is one: a logged-out /cart is only a redirect to the login page
            auth = "user" if any(p.path == path and p.auth == "user" and p.status and p.status < 400
                                 for p in self.map.pages.values()) else "anon"
            self.board.add("uiux", f"Look at {path} on a desktop and a phone", page=path, user_state=auth, kind="look",
                           risk=2, source="planner",
                           expected="Nothing overlaps or is cut off, text is readable, the main action is easy to "
                                    "find, and the phone view works without sideways scrolling.")
            n += 1
        return n

    # ---------- catalog: the known critical bugs per site type (no model) ----------
    def _page_for(self, bug: dict) -> tuple[str, str]:
        """A page to start the catalog task on: a route that matches the bug's words, else the home page.
        Returns (path, user_state)."""
        want = re.findall(r"[a-z]{4,}", f"{bug['title']} {bug['area']} {bug['symptom']}".lower())
        best, score = None, 0
        for p in self.map.pages.values():
            if not p.status or p.status >= 400:
                continue
            hay = f"{p.route} {p.title}".lower()
            s = sum(1 for w in want if w in hay) + (1 if p.auth == "user" and self.have_login else 0)
            if s > score:
                best, score = p, s
        if best is None:
            auth = "user" if self.have_login and bug["area"] in ("auth", "session", "access") else "anon"
            return "/", auth
        return best.path, best.auth

    def catalog_tasks(self) -> int:
        """Seed the known critical bugs for this kind of site, so the testers always check them."""
        try:
            from .catalog import select
            types, bugs = select(self.map, have_login=self.have_login, two_accounts=self.two_accounts,
                                  allow_writes=self.allow_writes)
        except Exception as e:
            self.log(f"  catalog: not loaded ({type(e).__name__}: {str(e)[:100]})")
            return 0
        self.plan.setdefault("site_types", types)
        n = 0
        for b in bugs:
            path, auth = self._page_for(b)
            steps = [s for s in [b["how"]] if s] + [f"Known risk for this kind of site ({b['area']}, {b['severity']})."]
            self.board.add(b["team"], b["title"], page=path, user_state=auth, kind="catalog", risk=b["risk"],
                           source="catalog", steps=steps,
                           expected=b["expected"] or "The feature behaves correctly and the server agrees with the screen.")
            n += 1
        if n:
            self.log(f"  catalog: site type(s) {', '.join(types) or 'unknown'}; {n} known critical-bug check(s) added")
        return n

    def run(self) -> dict:
        try:
            return self._run()
        finally:
            self.done.set()

    def _run(self) -> dict:
        self.catalog_tasks()            # the known critical bugs first, so they are on the board even if the model fails
        try:
            self.overview()
        except Exception as e:
            self.log(f"  planner: overview failed ({str(e)[:120]}); the checker's risk guess is used")
            return self.plan
        j = self.journey_and_look_tasks()        # journeys first: the journey and UI teams can start at once
        # one page per template: /product/1 ... /product/4 share their controls, plan the first only
        seen_tpl, pages = set(), []
        for p in sorted((p for p in _pages(self.map) if self.plan["page_risk"].get(p.path) in ("high", "medium")),
                        key=lambda p: -RISK[self.plan["page_risk"][p.path]]):
            tpl = re.sub(r"/\d+(?=/|$)", "/N", p.path)
            if tpl not in seen_tpl:
                seen_tpl.add(tpl)
                pages.append(p)
        pages = pages[:self.max_pages]
        with ThreadPoolExecutor(max_workers=5) as ex:
            counts = list(ex.map(lambda p: _safe(self.page_tasks, p, self.log), pages))
        self.log(f"  planner: {sum(counts)} function task(s) on {len(pages)} page(s), {j} journey/look task(s)")
        return self.plan


def _control_key(e) -> tuple:
    return (e.kind, (e.name or e.label or e.field or "").strip().lower(), e.href or "")


def _safe(fn, page, log):
    try:
        return fn(page)
    except Exception as e:
        log(f"  planner: tasks for {page.path} failed ({str(e)[:100]})")
        return 0


FOLLOW_UP_UI = ("layout", "look", "visual", "overlap", "display", "render", "style", "css", "broken page", "mobile", "cut off")
FOLLOW_UP_FLOW = ("flow", "journey", "next page", "after login", "checkout", "redirect", "session")


def route_follow_up(board, task: dict) -> str:
    """Turn a follow-up note into a task for the right team (a plain-code rule)."""
    note = (task.get("follow_up") or "").lower()
    team = "uiux" if any(w in note for w in FOLLOW_UP_UI) else "journey" if any(w in note for w in FOLLOW_UP_FLOW) else "function"
    tid = board.add(team, f"Follow-up of {task['id']}: {task['follow_up'][:100]}", page=task["page"],
                    element_id=task.get("element_id"), user_state=task["user_state"], kind="follow-up",
                    risk=max(2, int(task.get("risk") or 1)), source="follow-up",
                    expected="The concern in the note is checked and confirmed or ruled out with evidence.")
    board.mark_follow_up_done(task["id"], tid)
    return tid
