"""QA Agent v2 report: the verdict line and the files, built by code from the board."""

import os
import tempfile
import unittest

from v2.board import TaskBoard
from v2.report import build, collect
from v2.sitemap import Page, SiteMap

PROOF = {"screenshot": "none.png", "url": "http://shop.test/", "server_responses": []}


def board_with(*results, not_tested=()):
    b = TaskBoard()
    b.set_meta("plan", {"product_type": "shop", "demo_journeys": [{"name": "buy", "pages": ["/", "/cart"], "steps": []}]})
    for page, verdict, risk in results:
        b.add_result("function", f"task on {page}", verdict, PROOF, page=page, risk=risk, reason="r", source="tester")
    for page, risk in not_tested:
        tid = b.add("function", f"open task on {page}", page=page, risk=risk)
        b.mark_not_tested(tid, "time limit (25 minutes)")
    return b


SMAP = SiteMap(site_url="http://shop.test")
for _p in ("/", "/cart", "/help"):
    SMAP.add_page(Page(route=_p, path=_p, url="http://shop.test" + _p, status=200))


class VerdictLine(unittest.TestCase):
    def line(self, b):
        return collect(b, SMAP, {})["verdict"]["key"]

    def test_fail_on_demo_path_is_not_safe(self):
        self.assertEqual(self.line(board_with(("/cart", "FAIL", 1), ("/", "PASS", 1))), "not_safe")

    def test_high_risk_fail_anywhere_is_not_safe(self):
        self.assertEqual(self.line(board_with(("/help", "FAIL", 3))), "not_safe")

    def test_fail_off_the_path_is_avoid(self):
        b = board_with(("/help", "FAIL", 1), ("/", "PASS", 1))
        r = collect(b, SMAP, {})
        self.assertEqual(r["verdict"]["key"], "avoid")
        self.assertIn("/help", r["verdict"]["line"])

    def test_untested_demo_path_is_not_confirmed(self):
        self.assertEqual(self.line(board_with(("/", "PASS", 1), not_tested=[("/cart", 3)])), "not_confirmed")

    def test_all_good_is_safe(self):
        self.assertEqual(self.line(board_with(("/", "PASS", 1), ("/cart", "PASS", 3))), "safe")

    def test_files_are_written_without_a_model(self):
        with tempfile.TemporaryDirectory() as d:
            rep = build(board_with(("/cart", "FAIL", 3), ("/", "PASS", 1)), SMAP, {"minutes": 1}, d, manual=True, chat=False)
            self.assertEqual(rep["verdict"]["line"], "NOT SAFE TO DEMO")
            for k in ("html", "txt", "json", "manual"):
                self.assertTrue(os.path.isfile(rep["files"][k]), k)
            with open(rep["files"]["html"], encoding="utf-8") as f:
                page = f.read()
            self.assertIn("NOT SAFE TO DEMO", page)
            self.assertLess(page.index("Critical bugs"), page.index("Not tested"))
            self.assertLess(page.index("Not tested"), page.index("Needs a human look"))


if __name__ == "__main__":
    unittest.main()
