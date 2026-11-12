"""The site map: every page, element and link the crawler found.

The map is the only source of truth for the rest of a run. A tester action must
name an element ID from the map; an ID that is not on the map is rejected
(require_element), so a tester cannot act on a button that does not exist.

Element IDs are stable across crawls of the same build: they hash the page
path, the element kind, its accessible name, its target (href / type / name)
and its position among elements with the same key. Query strings are kept on
the page route but not in the ID, so /?page=2 and /?page=3 share IDs for the
same shared header link.
"""

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from dataclasses import field as dc_field   # Element has an attribute named "field"
from urllib.parse import urlparse

INTERACTIVE = ("link", "button", "input", "checkbox", "radio", "select", "textarea", "tab", "form")


class UnknownElement(KeyError):
    """A tester named an element ID that the crawler never saw."""


@dataclass
class Element:
    id: str
    page: str           # page route (path + query) where it was found
    path: str           # path only
    kind: str           # one of INTERACTIVE
    name: str = ""      # accessible name / visible text
    label: str = ""     # <label> text for form fields
    href: str | None = None
    type: str = ""      # input type, button type
    field: str = ""     # name attribute
    selector: str = ""
    form: int = -1      # index of the enclosing form on the page, -1 = none
    auth: str = "anon"  # "anon" or "user": the login state it was seen in
    options: list = dc_field(default_factory=list)   # labels of a select box


@dataclass
class Page:
    route: str          # path + query, the key of the page
    path: str
    url: str
    auth: str = "anon"
    status: int = 0
    final_url: str = ""
    title: str = ""
    depth: int = 0
    found_from: str = ""
    screenshot: str = ""
    load_ms: int = 0
    elements: list[str] = field(default_factory=list)
    links_to: list[str] = field(default_factory=list)
    text: str = ""          # the start of the visible text, for the planner


@dataclass
class SiteMap:
    site_url: str
    pages: dict[str, Page] = field(default_factory=dict)        # key: f"{auth} {route}"
    elements: dict[str, Element] = field(default_factory=dict)  # key: element id
    edges: list[dict] = field(default_factory=list)             # {from, to, element, auth}
    notes: list[str] = field(default_factory=list)
    login_posts: list[str] = field(default_factory=list)   # paths the login form writes to

    # ---------- building ----------
    @staticmethod
    def page_key(route: str, auth: str) -> str:
        return f"{auth} {route}"

    def add_page(self, page: Page) -> Page:
        self.pages[self.page_key(page.route, page.auth)] = page
        return page

    def add_elements(self, page: Page, raw: list[dict]) -> list[Element]:
        """Turn raw element records from the browser into Elements with stable IDs."""
        seen: dict[str, int] = {}
        out = []
        for r in raw:
            kind = r.get("kind", "")
            if kind not in INTERACTIVE:
                continue
            target = r.get("href") if kind == "link" else f"{r.get('type', '')}|{r.get('field', '')}"
            base = "|".join([page.path, kind, (r.get("name") or "").lower(), str(target), page.auth])
            n = seen.get(base, 0)
            seen[base] = n + 1
            eid = "el-" + hashlib.sha1(f"{base}#{n}".encode("utf-8", "replace")).hexdigest()[:10]
            el = Element(id=eid, page=page.route, path=page.path, kind=kind, name=r.get("name", ""),
                         label=r.get("label", ""), href=r.get("href"), type=r.get("type", ""),
                         field=r.get("field", ""), selector=r.get("selector", ""), form=r.get("form", -1),
                         auth=page.auth, options=list(r.get("options") or []))
            self.elements.setdefault(eid, el)
            if eid not in page.elements:
                page.elements.append(eid)
            out.append(el)
        return out

    # ---------- lookups ----------
    def element(self, eid: str) -> Element | None:
        return self.elements.get(eid)

    def require_element(self, eid: str) -> Element:
        el = self.elements.get(eid)
        if el is None:
            raise UnknownElement(f"{eid} is not on the site map; the action is rejected")
        return el

    def page(self, route: str, auth: str = "anon") -> Page | None:
        return self.pages.get(self.page_key(route, auth))

    def paths(self) -> list[str]:
        """Distinct page paths (no query, either login state), in crawl order."""
        out = []
        for p in self.pages.values():
            if p.path not in out:
                out.append(p.path)
        return out

    def items(self) -> list[dict]:
        """Everything that must end the run with a result: one item per page
        path and login state, and one per interactive element."""
        out, seen = [], set()
        for p in self.pages.values():
            k = (p.path, p.auth)
            if k not in seen:
                seen.add(k)
                out.append({"type": "page", "page": p.path, "auth": p.auth, "element_id": None})
        for e in self.elements.values():
            out.append({"type": "element", "page": e.path, "auth": e.auth, "element_id": e.id, "kind": e.kind})
        return out

    # ---------- files ----------
    def to_dict(self) -> dict:
        return {"site_url": self.site_url, "pages": [asdict(p) for p in self.pages.values()],
                "elements": [asdict(e) for e in self.elements.values()], "edges": self.edges, "notes": self.notes,
                "login_posts": self.login_posts}

    @classmethod
    def from_dict(cls, d: dict) -> "SiteMap":
        m = cls(site_url=d["site_url"], edges=list(d.get("edges", [])), notes=list(d.get("notes", [])),
                login_posts=list(d.get("login_posts", [])))
        for p in d.get("pages", []):
            m.add_page(Page(**p))
        for e in d.get("elements", []):
            m.elements[e["id"]] = Element(**e)
        return m

    def save(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=1)
        return path

    @classmethod
    def load(cls, path: str) -> "SiteMap":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


def strip_fragment(url: str) -> str:
    """Drop the #fragment, but keep hash routes of single-page apps (#/cart, #!/cart)."""
    base, _, frag = url.partition("#")
    return url if frag.startswith(("/", "!/")) else base


def route_of(url: str) -> tuple[str, str]:
    """(route, path) of a URL: route keeps the query, path does not. A hash route counts as the path."""
    u = urlparse(url)
    path = u.path or "/"
    if u.fragment.startswith(("/", "!/")):
        path = path.rstrip("/") + "/#" + u.fragment
    return (path + ("?" + u.query if u.query else "")), path
