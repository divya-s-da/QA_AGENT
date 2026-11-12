"""Cross-run memory: a finding seen before is 'recurring', a genuinely new one is 'new'.
Fail-safe: when a match is not clear, the finding is treated as NEW (never hidden)."""

import os
import tempfile
import unittest

os.environ["V2_MEMORY_DIR"] = tempfile.mkdtemp()

from v2 import memory as M   # noqa: E402  (import after the env var is set)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        M.STORE_DIR = tempfile.mkdtemp()      # a clean store per test
        self.url = "https://app.example.com/"

    def classify(self, findings):
        return M.SiteMemory(self.url).classify(findings)

    def test_first_run_all_new(self):
        f = [{"page": "/cart", "title": "Checkout button does nothing"}]
        self.assertEqual(self.classify(f)["new"], 1)
        self.assertTrue(f[0]["is_new"])

    def test_identical_finding_is_recurring_next_run(self):
        self.classify([{"page": "/cart", "title": "Checkout button does nothing on the cart page"}])
        f = [{"page": "/cart", "title": "Checkout button does nothing on the cart page"}]
        self.assertEqual(self.classify(f)["recurring"], 1)
        self.assertFalse(f[0]["is_new"])

    def test_reworded_same_bug_is_recurring(self):
        self.classify([{"page": "/cart", "title": "Checkout button does nothing on the cart page"}])
        f = [{"page": "/cart", "title": "The checkout button does nothing when clicked on the cart"}]
        self.assertGreaterEqual(self.classify(f)["recurring"], 1)
        self.assertFalse(f[0]["is_new"])

    def test_genuinely_new_bug_is_new(self):
        self.classify([{"page": "/cart", "title": "Checkout button does nothing"}])
        f = [{"page": "/search", "title": "Search returns results for an empty query"}]
        self.classify(f)
        self.assertTrue(f[0]["is_new"])

    def test_same_words_different_route_is_new(self):
        # Fail-safe: same wording on a different page is a different finding.
        self.classify([{"page": "/cart", "title": "Total ignores quantity"}])
        f = [{"page": "/checkout", "title": "Total ignores quantity"}]
        self.classify(f)
        self.assertTrue(f[0]["is_new"])

    def test_no_commit_does_not_persist(self):
        f = [{"page": "/cart", "title": "Checkout button does nothing"}]
        M.SiteMemory(self.url).classify(f, commit=False)
        # a later run still sees it as new, because nothing was stored
        g = [{"page": "/cart", "title": "Checkout button does nothing"}]
        self.assertGreaterEqual(M.SiteMemory(self.url).classify(g)["new"], 1)


if __name__ == "__main__":
    unittest.main()
