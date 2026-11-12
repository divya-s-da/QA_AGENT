"""QA Agent v2 bug catalog: site-type detection and the choice of critical-bug checks."""

import unittest

from v2 import catalog
from v2.sitemap import Page, SiteMap


def shop_map() -> SiteMap:
    m = SiteMap(site_url="http://shop.test")
    for r, t, txt in [("/", "Home", "add to cart checkout products"), ("/cart", "Cart", "basket subtotal checkout"),
                      ("/orders", "Orders", "order history"), ("/account", "Account", "profile")]:
        m.add_page(Page(route=r, path=r, url="http://shop.test" + r, status=200, auth="user", title=t, text=txt))
    return m


def bank_map() -> SiteMap:
    m = SiteMap(site_url="http://bank.test")
    for r, t, txt in [("/", "Bank", "account balance"), ("/transfer", "Transfer", "transfer funds payee amount"),
                      ("/statements", "Statements", "transaction statement")]:
        m.add_page(Page(route=r, path=r, url="http://bank.test" + r, status=200, auth="user", title=t, text=txt))
    return m


class Detect(unittest.TestCase):
    def test_catalog_loads(self):
        c = catalog.load()
        self.assertGreater(len(c["site_types"]), 20)
        self.assertGreater(len(c["bugs"]), 200)

    def test_shop_detected(self):
        self.assertEqual(catalog.detect_types(shop_map())[0], "shop")

    def test_bank_detected(self):
        self.assertEqual(catalog.detect_types(bank_map())[0], "bank")

    def test_no_signal_is_not_a_crash(self):
        m = SiteMap(site_url="http://x.test")
        m.add_page(Page(route="/", path="/", url="http://x.test/", status=200, title="Welcome", text="hello"))
        self.assertIsInstance(catalog.detect_types(m), list)


class Select(unittest.TestCase):
    def test_only_task_t0_t1_chosen(self):
        _, bugs = catalog.select(shop_map(), have_login=True, two_accounts=True, allow_writes=True)
        self.assertTrue(bugs)
        self.assertTrue(all(b["severity"] in ("T0", "T1") for b in bugs))
        self.assertLessEqual(len(bugs), 24)

    def test_generic_critical_bugs_are_kept(self):
        # the bugs the product missed before must be on the list for any site with a login
        _, bugs = catalog.select(shop_map(), have_login=True, two_accounts=False, allow_writes=True)
        ids = {b["id"] for b in bugs}
        self.assertIn("GEN-AUTH-001", ids)       # old session still valid after logout
        self.assertIn("GEN-FORM-001", ids)        # success message but nothing saved

    def test_two_account_bugs_dropped_with_one_account(self):
        c = catalog.load()
        two = {b["id"] for _, b in [("x", b) for b in catalog.select(shop_map(), have_login=True, two_accounts=True, allow_writes=True)[1]]}
        one = {b["id"] for b in catalog.select(shop_map(), have_login=True, two_accounts=False, allow_writes=True)[1]}
        has_two_acct = any(b.get("needs_two_accounts") for b in c["bugs"] if b["id"] in two)
        # a bug that needs two accounts must not appear in the one-account run
        for b in c["bugs"]:
            if b.get("needs_two_accounts") and b["id"] in one:
                self.fail(f"{b['id']} needs two accounts but was chosen for a one-account run")

    def test_login_bugs_dropped_without_login(self):
        _, bugs = catalog.select(shop_map(), have_login=False, two_accounts=False, allow_writes=True)
        for b in bugs:
            self.assertNotIn(b["area"], ("auth", "session", "access"))

    def test_write_bugs_dropped_in_read_only(self):
        c = catalog.load()
        _, bugs = catalog.select(shop_map(), have_login=True, two_accounts=True, allow_writes=False)
        chosen = {b["id"] for b in bugs}
        for b in c["bugs"]:
            if b.get("needs_write") and b["id"] in chosen:
                self.fail(f"{b['id']} needs writes but was chosen for a read-only run")


if __name__ == "__main__":
    unittest.main()
