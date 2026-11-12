"""The checker: plain code inside the task board.

When no work is left, it compares the site map with the board. Every page and
element without a result becomes a new task. It may send work back at most
`max_rounds` times; after that, and at the time limit, everything still open
goes to NOT_TESTED with a reason, so each item on the map ends the run with a
result or a reason. The checker asks "was everything done?"; the judge asks
"was it done correctly?".
"""

from .board import TaskBoard
from .sitemap import SiteMap

# which team gets a gap, by element kind
TEAM_FOR_KIND = {"link": "function", "button": "function", "input": "function", "checkbox": "function",
                 "radio": "function", "select": "function", "textarea": "function", "tab": "function",
                 "form": "function"}
RISK_WORDS = (("pay", 3), ("checkout", 3), ("order", 3), ("login", 3), ("log in", 3), ("sign in", 3),
              ("password", 3), ("delete", 3), ("remove", 3), ("admin", 3), ("cart", 2), ("search", 2),
              ("account", 2), ("profile", 2), ("register", 2), ("sign up", 2))


def risk_of(text: str) -> int:
    """A first, plain-code risk guess. The planner replaces it (build step 4)."""
    t = (text or "").lower()
    return max([r for w, r in RISK_WORDS if w in t] or [1])


class Checker:
    def __init__(self, board: TaskBoard, sitemap: SiteMap, max_rounds: int = 2):
        self.board, self.map, self.max_rounds = board, sitemap, max_rounds

    @property
    def round(self) -> int:
        return self.board.get_meta("checker_round", 0)

    def _covered(self) -> tuple[set, set, set]:
        """(results, pending, closed): keys with a judged/done result, with work still
        open or claimed, and with a NOT_TESTED reason. Key: (page, auth, element_id)."""
        results, pending, closed = set(), set(), set()
        for t in self.board.tasks():
            keys = [(t["page"], t["user_state"], t["element_id"])]
            keys += [(t["page"], t["user_state"], e) for e in (t.get("covers") or [])]
            bucket = results if t["status"] in ("judged", "done") else pending if t["status"] in ("open", "claimed") else closed
            bucket.update(keys)
        return results, pending, closed

    def gaps(self) -> list[dict]:
        """Map items with no result, no pending work and no NOT_TESTED reason."""
        results, pending, closed = self._covered()
        out = []
        for it in self.map.items():
            k = (it["page"], it["auth"], it["element_id"])
            if k not in results and k not in pending and k not in closed:
                out.append(it)
        return out

    def run_round(self) -> dict:
        """Call when the board is idle. Returns {"round", "added", "closed", "final"}."""
        r = self.round
        gaps = self.gaps()
        if not gaps:
            return {"round": r, "added": [], "closed": 0, "final": True}
        if r >= self.max_rounds:
            return {"round": r, "added": [], "closed": self.finish(f"round limit reached ({self.max_rounds} checker rounds)"),
                    "final": True}
        r += 1
        self.board.set_meta("checker_round", r)
        added = []
        # A form is tested as one task: its fields and buttons are covered by it.
        forms, members = {}, {}
        for it in gaps:
            el = self.map.element(it["element_id"]) if it["element_id"] else None
            if el is not None and el.kind == "form":
                forms[(el.page, el.auth, el.form)] = it
        for it in gaps:
            el = self.map.element(it["element_id"]) if it["element_id"] else None
            if el is not None and el.kind != "form" and el.form >= 0 and (el.page, el.auth, el.form) in forms:
                members.setdefault((el.page, el.auth, el.form), []).append(el)
        grouped = {e.id for els in members.values() for e in els}
        for key, it in forms.items():
            f_el = self.map.element(it["element_id"])
            els = members.get(key, [])
            names = [e.name or e.label or e.field or e.kind for e in els]
            title = f"Test the form on {f_el.path}" + (f" ({', '.join(n for n in names if n)[:120]})" if names else "")
            risk = max([risk_of(f_el.path), risk_of(f_el.field)] + [risk_of(f"{e.name} {e.label} {e.field}") for e in els])
            added.append(self.board.add("function", title, page=it["page"], element_id=f_el.id, user_state=it["auth"],
                                        kind="form", risk=risk, round=r, source="checker",
                                        covers=[e.id for e in els], steps=[f"{e.id} {e.kind} '{n}'" for e, n in zip(els, names)],
                                        expected="Valid input is accepted and saved; wrong or empty input is refused "
                                                 "with a clear message; nothing breaks."))
        # The same control on many pages (a header button, "Add to cart" on each card) is tested once;
        # the task covers every copy (reuse before re-test).
        same: dict[tuple, list] = {}
        for it in gaps:
            el = self.map.element(it["element_id"]) if it["element_id"] else None
            if el is None or el.kind == "form" or el.id in grouped:
                continue
            name = (el.name or el.label or el.field or "").strip().lower()
            if name:
                same.setdefault((el.kind, name, el.auth, el.href or ""), []).append(el)
        dup_of = {}
        for els in same.values():
            for e in els[1:]:
                dup_of[e.id] = els[0]
        for it in gaps:
            if it["element_id"] in grouped or it["element_id"] in dup_of or (it["element_id"] and it in forms.values()):
                continue
            if it["type"] == "page":
                title = f"Check page {it['page']} ({it['auth']})"
                team, kind, risk = "function", "page", risk_of(it["page"])
            else:
                el = self.map.element(it["element_id"])
                title = f"Test {el.kind} '{el.name or el.label or el.field or el.href}' on {el.path}"
                team, kind = TEAM_FOR_KIND.get(el.kind, "function"), el.kind
                risk = max(risk_of(el.path), risk_of(f"{el.name} {el.label} {el.href or ''}"))
            copies = [e for e in dup_of if dup_of[e].id == it["element_id"]] if it["element_id"] else []
            if copies:
                pages = sorted({self.map.element(e).path for e in copies} | {it["page"]})
                title += f" (the same control is on {len(pages)} page(s): one test covers all copies)"
            added.append(self.board.add(team, title, page=it["page"], element_id=it["element_id"],
                                        user_state=it["auth"], kind=kind, risk=risk, round=r, source="checker",
                                        covers=copies,
                                        expected="It does what its name says, and the page or the server shows the "
                                                 "result; no error is shown."))
        return {"round": r, "added": added, "closed": 0, "final": False}

    def finish(self, reason: str) -> int:
        """The last comparison: open work and every uncovered map item get NOT_TESTED."""
        n = self.board.close_open(reason)
        for it in self.gaps():
            tid = self.board.add("checker", f"Not tested: {it['type']} on {it['page']}", page=it["page"],
                                 element_id=it["element_id"], user_state=it["auth"], kind=it["type"],
                                 source="checker", round=self.round)
            self.board.mark_not_tested(tid, reason)
            n += 1
        return n
