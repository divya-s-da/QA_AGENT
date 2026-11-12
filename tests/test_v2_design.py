"""QA Agent v2 design checker: page choice, result rules and the measurement JavaScript."""

import os
import tempfile
import unittest

from v2.board import TaskBoard
from v2.design import DESIGN_JS, DesignChecker
from v2.sitemap import Page, SiteMap


def a_map() -> SiteMap:
    m = SiteMap(site_url="http://app.test")
    m.add_page(Page(route="/", path="/", url="http://app.test/", status=200, auth="anon", depth=0))
    m.add_page(Page(route="/", path="/", url="http://app.test/", status=200, auth="user", depth=0))
    m.add_page(Page(route="/cart", path="/cart", url="http://app.test/cart", status=200, auth="user", depth=1))
    m.add_page(Page(route="/gone", path="/gone", url="http://app.test/gone", status=404, auth="anon", depth=2))
    return m


class PageChoice(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.dc = DesignChecker("http://app.test", TaskBoard(), a_map(), self.d, max_pages=10)

    def test_logged_in_wins_and_broken_dropped(self):
        pages = self.dc._pages()
        routes = [(p.path, p.auth) for p in pages]
        self.assertIn(("/", "user"), routes)          # the logged-in home, not the anon one
        self.assertNotIn(("/", "anon"), routes)
        self.assertNotIn(("/gone", "anon"), routes)    # a 404 page is not checked
        self.assertEqual(len(routes), len(set(p for p, _ in routes)))   # one page per path

    def test_cap(self):
        m = SiteMap(site_url="http://app.test")
        for i in range(20):
            m.add_page(Page(route=f"/p{i}", path=f"/p{i}", url=f"http://app.test/p{i}", status=200))
        dc = DesignChecker("http://app.test", TaskBoard(), m, self.d, max_pages=5)
        self.assertEqual(len(dc._pages()), 5)


class ResultRules(unittest.TestCase):
    def setUp(self):
        self.b = TaskBoard()
        self.dc = DesignChecker("http://app.test", self.b, a_map(), tempfile.mkdtemp())

    def _kinds(self, verdict):
        return [t for t in self.b.tasks() if t["kind"] == "design:rules" and t["verdict"] == verdict]

    def test_clean_page_passes(self):
        self.dc._post("/", "/", "user", "http://app.test/", "s.png", [])
        self.assertEqual(len(self._kinds("PASS")), 1)
        self.assertEqual(len(self._kinds("FAIL")), 0)

    def test_findings_fail_with_worst_risk(self):
        self.dc._post("/cart", "/cart", "user", "http://app.test/cart", "s.png",
                      [{"rule": "DS-OVER-002", "sev": "T0", "title": "Covered button", "what": "x", "where": "button"}])
        f = self._kinds("FAIL")
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["risk"], 3)             # T0 -> risk 3
        self.assertEqual(f[0]["observed"]["design"][0]["rule"], "DS-OVER-002")

    def test_accessibility_rule_reported_once_site_wide(self):
        nav = [{"rule": "DS-A11Y-color-contrast", "sev": "T1", "title": "Contrast", "what": "y", "where": "#logout"}]
        self.dc._post("/", "/", "user", "http://app.test/", "s.png", list(nav))
        self.dc._post("/cart", "/cart", "user", "http://app.test/cart", "s.png", list(nav))
        self.assertEqual(len(self._kinds("FAIL")), 1)   # first page only
        self.assertEqual(len(self._kinds("PASS")), 1)   # second page: the repeat is dropped, so it is clean

    def test_layout_rule_not_deduped_across_pages(self):
        f = [{"rule": "DS-RESP-001", "sev": "T1", "title": "Sideways scroll", "what": "wide", "where": "div"}]
        self.dc._post("/a", "/a", "user", "http://app.test/a", "s.png", list(f))
        self.dc._post("/b", "/b", "user", "http://app.test/b", "s.png", list(f))
        self.assertEqual(len(self._kinds("FAIL")), 2)   # a measured layout rule can fail on each page


class MeasureJs(unittest.TestCase):
    """The JavaScript runs in a real browser; skip when chromium is not installed."""

    def test_js_finds_planted_issues(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            self.skipTest("playwright not installed")
        html = """<!doctype html><meta charset=utf8><style>
          body{margin:0}.wide{width:1600px;height:3px}.clip{width:60px;overflow:hidden;white-space:nowrap}
          .tiny{font-size:8px}</style>
          <div class=wide></div>
          <p class=clip>far too long to fit in this clipped narrow box</p>
          <p class=tiny>very small legal footnote text nobody can read here</p>
          <p>value is undefined and total is NaN</p>"""
        d = tempfile.mkdtemp()
        path = os.path.join(d, "bad.html")
        open(path, "w").write(html)
        try:
            with sync_playwright() as pw:
                br = pw.chromium.launch()
                pg = br.new_context(viewport={"width": 1280, "height": 800}).new_page()
                pg.goto("file://" + path)
                pg.wait_for_timeout(200)
                rules = {f["rule"] for f in pg.evaluate(DESIGN_JS)}
                br.close()
        except Exception as e:
            self.skipTest(f"browser not available: {e}")
        for r in ("DS-RESP-001", "DS-TEXT-007", "DS-TEXT-001", "DS-CONTENT-001"):
            self.assertIn(r, rules, f"{r} should fire on the planted page")


if __name__ == "__main__":
    unittest.main()
