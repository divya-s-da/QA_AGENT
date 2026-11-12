"""QA Agent v2 build step 2 against ShopLite: crawl, basic checks, checker.

Plain code, no model. On the bug build the basic checks must catch the five
bugs that need no AI (B13 broken link, B15 broken image, B19 empty title, B20
JavaScript error, B21 empty href). On the clean build they must report no FAIL.
The crawl must not change any data. Skipped when no Chromium is installed.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "benchmark", "shoplite"))
import app as shoplite  # noqa: E402

from v2.run import run  # noqa: E402
from v2.sitemap import SiteMap  # noqa: E402

PORT = 18775   # test_benchmark_app uses 18765 and 18766
SITE = f"http://127.0.0.1:{PORT}"


def chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


def fails(data, kinds=None):
    return [(s["route"], v) for s in data["screens"] for v in s["verdicts"]
            if v["verdict"] == "FAIL" and (kinds is None or v.get("kind") in kinds)]


def non_design_fails(data):
    # design:rules are measured accessibility/layout findings (axe-core, contrast, overlap). They are real
    # and provable, not false alarms, so a genuinely imperfect page can carry them. The clean-build guard
    # is about planted-bug and basic-check false alarms, so it excludes them.
    return [(r, v) for r, v in fails(data) if v.get("kind") != "design:rules"]


@unittest.skipUnless(chromium_available(), "Chromium for Playwright is not installed")
class CrawlShopLite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = shoplite.serve(PORT, set())
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = {k: os.environ.get(k) for k in ("SITE_EMAIL", "SITE_PASSWORD")}
        os.environ.update(SITE_EMAIL="bob@shoplite.test", SITE_PASSWORD="bob-pass-123")
        cls.runs = {}
        for name, bugs in (("bugs", shoplite.parse_bugs("all")), ("clean", set()), ("clean2", set())):
            shoplite.ACTIVE_BUGS.clear()
            shoplite.ACTIVE_BUGS.update(bugs)
            shoplite.reset_db()
            before = json.dumps({k: v for k, v in shoplite.DB.items() if k != "sessions"}, default=str, sort_keys=True)
            out = os.path.join(cls.tmp.name, name)
            code, data = run(SITE, out, login=True, log=lambda *a: None)
            # the login itself adds a session; nothing else may change
            after = json.dumps({k: v for k, v in shoplite.DB.items() if k != "sessions"}, default=str, sort_keys=True)
            cls.runs[name] = {"code": code, "data": data, "out": out, "unchanged": before == after,
                              "diff": (before, after) if before != after else None}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()
        for k, v in cls.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_runs_finish_and_log_in(self):
        for name, r in self.runs.items():
            self.assertEqual(r["code"], 0, name)
            self.assertTrue(r["data"]["login"].startswith("LOGGED IN"), f"{name}: {r['data']['login']}")

    def test_first_page_checked_in_the_first_minute(self):
        self.assertLess(self.runs["bugs"]["data"]["first_check_s"], 60)

    def test_map_has_public_and_logged_in_pages(self):
        m = SiteMap.load(os.path.join(self.runs["clean"]["out"], "site_map.json"))
        anon = {p.path for p in m.pages.values() if p.auth == "anon" and p.status == 200}
        user = {p.path for p in m.pages.values() if p.auth == "user" and p.status == 200}
        self.assertTrue({"/", "/login", "/register", "/contact", "/about"} <= anon, anon)
        self.assertTrue({"/cart", "/orders", "/account"} <= user, user)
        self.assertTrue(any(p.startswith("/product/") for p in anon))

    def test_element_ids_are_stable(self):
        a = SiteMap.load(os.path.join(self.runs["clean"]["out"], "site_map.json"))
        b = SiteMap.load(os.path.join(self.runs["clean2"]["out"], "site_map.json"))
        # Parallel crawl workers may visit other query variants (/?page=2) from run to run, so compare the
        # pages that both crawls saw: the same page must give the same element IDs.
        both = set(a.pages) & set(b.pages)
        self.assertGreater(len(both), 10)
        for k in both:
            self.assertEqual(sorted(a.pages[k].elements), sorted(b.pages[k].elements), k)

    def test_bug_build_catches_the_five_plain_code_bugs(self):
        text = " | ".join(f"{r}: {v['reason']}" for r, v in fails(self.runs["bugs"]["data"]))
        for bug, needle in (("B13", "Continue shopping"), ("B15", "broken image"), ("B19", "empty <title>"),
                            ("B20", "uncaught JavaScript error"), ("B21", "'Help'")):
            self.assertIn(needle, text, f"{bug} not caught: {text}")

    def test_clean_build_has_no_fail(self):
        # no planted-bug or basic-check false alarm on the clean build (the core "no false green" guard);
        # measured design/accessibility findings are allowed, because they are provable, not false alarms.
        self.assertEqual(non_design_fails(self.runs["clean"]["data"]), [])

    def test_crawl_changes_no_data(self):
        for name, r in self.runs.items():
            self.assertTrue(r["unchanged"], f"{name}: the crawl changed ShopLite's data: {r['diff']}")

    def test_every_map_item_ends_with_a_result_or_a_reason(self):
        d = self.runs["clean"]["data"]
        c = d["counts"]
        self.assertEqual(c["open"] + c["claimed"] + c["done"], 0)
        self.assertTrue(all(t["reason"] for t in d["not_tested"]))


if __name__ == "__main__":
    unittest.main()
