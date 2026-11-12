"""Tests for the benchmark scorer's bug classes."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "benchmark"))
import score  # noqa: E402

SITE = "http://127.0.0.1:8765"


def write(screens):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"screens": screens}, f)
    f.close()
    return f.name


def verdict(check_id, status, url, note):
    return {"check_id": check_id, "verdict": status, "reason": f"[url: {url}] {note}", "url": url}


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.path = write([{"screen_id": "screen_0", "route": "/cart", "verdicts": [
            # B01 trap: agent saw the toast and passed it -> false_pass
            verdict("place_order", "PASS", SITE + "/cart", "Clicked Place order, toast said Order placed!"),
            # B02 caught
            verdict("cart_total_qty", "FAIL", SITE + "/cart", "Quantity 2 but total stayed $20.00"),
            # unrelated FAIL
            verdict("weird_thing", "FAIL", SITE + "/cart", "Footer colour contrast too low"),
        ]}, {"screen_id": "screen_1", "route": "/", "verdicts": [
            verdict("render", "PASS", SITE + "/", "Shop page rendered"),
        ]}])

    def tearDown(self):
        os.unlink(self.path)

    def classes(self):
        result = score.score(self.path)
        return {r["bug"]["id"]: r["class"] for r in result["rows"]}, result

    def test_classes(self):
        classes, result = self.classes()
        self.assertEqual(classes["B01"], "false_pass")
        self.assertEqual(classes["B02"], "caught")
        self.assertEqual(classes["B03"], "not_reached")   # /admin never visited
        self.assertEqual(classes["B06"], "missed")        # / tested, search never mentioned
        self.assertEqual(result["summary"]["trap_false_pass"], 1)
        self.assertEqual(result["summary"]["unlinked_fails"], 1)

    def test_url_from_note_when_url_field_missing(self):
        path = write([{"screen_id": "s", "route": "/", "verdicts": [
            {"check_id": "admin_access", "verdict": "FAIL",
             "reason": f"[url: {SITE}/admin] regular user can see the users table, no 403"}]}])
        try:
            rows = {r["bug"]["id"]: r["class"] for r in score.score(path)["rows"]}
            self.assertEqual(rows["B03"], "caught")
        finally:
            os.unlink(path)

    def test_root_route_does_not_match_subpaths(self):
        self.assertTrue(score.on_route("/", "/"))
        self.assertFalse(score.on_route("/cart", "/"))
        self.assertTrue(score.on_route("/product/999", "/product"))
        self.assertFalse(score.on_route("/products", "/product"))

    def test_markdown_renders(self):
        md = score.to_markdown(score.score(self.path))
        self.assertIn("| B01 | T0 | yes | /cart | **false_pass** |", md)


if __name__ == "__main__":
    unittest.main()
