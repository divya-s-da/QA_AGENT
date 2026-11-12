"""Scoped testing: a run can be limited to one section or one page of a site."""

import tempfile
import unittest

from v2.board import TaskBoard
from v2.crawler import Crawler


class ScopeTests(unittest.TestCase):
    def mk(self, scope=None, page_only=False):
        return Crawler("https://x.com/checkout", tempfile.mkdtemp(), TaskBoard(":memory:"),
                       scope=scope, page_only=page_only)

    def test_no_scope_allows_all(self):
        c = self.mk()
        self.assertIsNone(c.scope)
        self.assertTrue(c._in_scope("https://x.com/anything"))

    def test_section_scope_includes_children_only(self):
        c = self.mk(scope="/checkout")
        self.assertTrue(c._in_scope("https://x.com/checkout"))
        self.assertTrue(c._in_scope("https://x.com/checkout/step2"))
        self.assertFalse(c._in_scope("https://x.com/cart"))
        self.assertFalse(c._in_scope("https://x.com/checkoutx"))   # must respect the path boundary

    def test_scope_is_normalized(self):
        c = self.mk(scope="/Checkout/")
        self.assertEqual(c.scope, "/checkout")
        self.assertTrue(c._in_scope("https://x.com/checkout/pay"))

    def test_page_only_flag(self):
        c = self.mk(scope="/checkout", page_only=True)
        self.assertTrue(c.page_only)


if __name__ == "__main__":
    unittest.main()
