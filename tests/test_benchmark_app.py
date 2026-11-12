"""Self-test for the ShopLite benchmark: every planted bug must be present in
the bug build and absent in the clean build. If this test fails, benchmark
scores mean nothing.

Server-side bugs are checked over HTTP. Browser-side bugs (JS handlers) are
checked with Playwright and skipped when no Chromium is installed.
"""

import http.cookiejar
import json
import os
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "benchmark", "shoplite"))
import app as shoplite  # noqa: E402

PORT = 18765
BASE = f"http://127.0.0.1:{PORT}"


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, method, path, body=None, form=None):
        data, headers = None, {}
        if body is not None:
            data, headers = json.dumps(body).encode(), {"Content-Type": "application/json"}
        if form is not None:
            data = "&".join(f"{k}={urllib.request.quote(v)}" for k, v in form.items()).encode()
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
        r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(r) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def login(self, email="bob@shoplite.test", password="bob-pass-123"):
        return self.req("POST", "/login", form={"email": email, "password": password})


def set_bugs(bugs):
    shoplite.ACTIVE_BUGS.clear()
    shoplite.ACTIVE_BUGS.update(bugs)
    shoplite.reset_db()


class ServerBugs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = shoplite.serve(PORT, set())

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def both(self, bug_id, probe):
        """probe() returns True when the bug is visible. Clean = False, bug = True."""
        set_bugs(set())
        self.assertFalse(probe(), f"{bug_id}: clean build shows the bug")
        set_bugs({bug_id})
        self.assertTrue(probe(), f"{bug_id}: bug build does not show the bug")

    def test_login_works(self):
        set_bugs(set())
        c = Client()
        status, page = c.login()
        self.assertEqual(status, 200)
        self.assertIn("Hi, Bob Tester", page)
        self.assertIn("Wrong email", Client().login(password="nope")[1])

    def test_B01_order_not_saved(self):
        def probe():
            c = Client(); c.login()
            c.req("POST", "/api/cart/add", {"product_id": 1})
            status, _ = c.req("POST", "/api/orders")
            orders = json.loads(c.req("GET", "/api/orders")[1])
            return status != 200 or len(orders) == 1
        self.both("B01", probe)

    def test_B02_total_ignores_qty(self):
        def probe():
            c = Client(); c.login()
            c.req("POST", "/api/cart/add", {"product_id": 1})
            c.req("POST", "/api/cart/update", {"product_id": 1, "qty": 2})
            return "$40.00" not in c.req("GET", "/cart")[1]
        self.both("B02", probe)

    def test_B03_admin_open_to_user(self):
        def probe():
            c = Client(); c.login()
            status, page = c.req("GET", "/admin")
            return status == 200 and "alice@shoplite.test" in page
        self.both("B03", probe)
        set_bugs(set())
        c = Client(); c.login("alice@shoplite.test", "alice-pass-123")
        self.assertEqual(c.req("GET", "/admin")[0], 200)

    def test_B06_search_ignored(self):
        self.both("B06", lambda: "Steel Bottle" in Client().req("GET", "/?q=mug")[1])

    def test_B07_invalid_email_accepted(self):
        def probe():
            page = Client().req("POST", "/register", form={"name": "X", "email": "abc", "password": "longpassword"})[1]
            return "account was created" in page
        self.both("B07", probe)

    def test_B08_profile_not_saved(self):
        def probe():
            c = Client(); c.login()
            ok = c.req("POST", "/api/profile", {"name": "Robert"})[0] == 200
            return ok and "Hi, Robert" not in c.req("GET", "/account")[1]
        self.both("B08", probe)

    def test_B09_page2_same_as_page1(self):
        self.both("B09", lambda: "Canvas Tote" in Client().req("GET", "/?page=2")[1])

    def test_B10_sort_as_text(self):
        # Low to high, page 1 must start with Notebook Set ($9.50). As text,
        # "$9.50" sorts last and "$100.00" first.
        self.both("B10", lambda: "Notebook Set" not in Client().req("GET", "/?sort=price")[1])

    def test_B13_continue_shopping_404(self):
        def probe():
            c = Client(); c.login()
            return 'href="/shop"' in c.req("GET", "/cart")[1]
        self.both("B13", probe)
        self.assertEqual(Client().req("GET", "/shop")[0], 404)

    def test_B14_coupon_no_discount(self):
        def probe():
            c = Client(); c.login()
            c.req("POST", "/api/cart/add", {"product_id": 1})
            ok = c.req("POST", "/api/cart/coupon", {"code": "SAVE10"})[0] == 200
            return ok and "$18.00" not in c.req("GET", "/cart")[1]
        self.both("B14", probe)

    def test_B15_broken_image(self):
        def probe():
            page = Client().req("GET", "/")[1]
            import re
            srcs = re.findall(r'<img src="([^"]+)"', page)
            return any(Client().req("GET", s)[0] != 200 for s in srcs)
        self.both("B15", probe)

    def test_B16_unlabeled_input(self):
        self.both("B16", lambda: 'for="nl"' not in Client().req("GET", "/")[1])

    def test_B17_wide_element(self):
        def probe():
            c = Client(); c.login()
            return "width:1600px" in c.req("GET", "/orders")[1]
        self.both("B17", probe)

    def test_B18_heading_skip(self):
        self.both("B18", lambda: "<h4>" in Client().req("GET", "/about")[1])

    def test_B19_empty_title(self):
        self.both("B19", lambda: "<title></title>" in Client().req("GET", "/contact")[1])

    def test_B21_empty_href(self):
        self.both("B21", lambda: 'href=""' in Client().req("GET", "/")[1])

    def test_B22_stack_trace(self):
        def probe():
            status, page = Client().req("GET", "/product/999")
            return status == 500 and "Traceback" in page
        self.both("B22", probe)
        set_bugs(set())
        self.assertEqual(Client().req("GET", "/product/999")[0], 404)


def _have_playwright():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_have_playwright(), "Playwright Chromium not installed")
class BrowserBugs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.srv = shoplite.serve(PORT + 1, set())
        cls.base = f"http://127.0.0.1:{PORT + 1}"
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()

    def page(self, login=True):
        ctx = self.browser.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        if login:
            page.goto(self.base + "/login")
            page.fill("#email", "bob@shoplite.test")
            page.fill("#password", "bob-pass-123")
            page.click("button[type=submit]")
            page.wait_for_url(self.base + "/")
        return page

    def both(self, bug_id, probe):
        set_bugs(set())
        self.assertFalse(probe(), f"{bug_id}: clean build shows the bug")
        set_bugs({bug_id})
        self.assertTrue(probe(), f"{bug_id}: bug build does not show the bug")

    def test_B01_success_toast_without_order(self):
        def probe():
            p = self.page()
            p.evaluate("api('POST','/api/cart/add',{product_id:1})")
            p.goto(self.base + "/cart")
            p.click("#place-order")
            p.wait_for_timeout(500)
            toast = p.inner_text("#toast")
            saved = len(p.evaluate("fetch('/api/orders').then(r=>r.json())"))
            return "placed" in toast.lower() and saved == 1
        self.both("B01", probe)

    def test_B04_cancel_deletes(self):
        def probe():
            p = self.page()
            p.goto(self.base + "/orders")
            p.click("text=Delete")
            p.click("#confirm-no")
            p.wait_for_timeout(500)
            p.goto(self.base + "/orders")
            return "#1000" not in p.content()
        self.both("B04", probe)

    def test_B05_logout_keeps_session(self):
        def probe():
            p = self.page()
            p.click("#logout-btn")
            p.wait_for_url(self.base + "/login")
            p.goto(self.base + "/account")
            return "Your account" in p.content()
        self.both("B05", probe)

    def test_B11_double_review(self):
        def probe():
            p = self.page()
            p.goto(self.base + "/product/1")
            p.click("#tab-reviews")
            p.fill("#review", "Great tote")
            p.evaluate("document.getElementById('review-btn').click();document.getElementById('review-btn').click()")
            p.wait_for_timeout(800)
            p.goto(self.base + "/product/1")
            return "Reviews (2)" in p.content()
        self.both("B11", probe)

    def test_B12_dead_tab(self):
        def probe():
            p = self.page(login=False)
            p.goto(self.base + "/product/1")
            p.click("#tab-reviews")
            return p.get_attribute("#tab-reviews", "aria-selected") != "true"
        self.both("B12", probe)

    def test_B17_horizontal_overflow(self):
        def probe():
            p = self.page()
            p.goto(self.base + "/orders")
            return p.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 2")
        self.both("B17", probe)

    def test_B20_console_error(self):
        def probe():
            p = self.page(login=False)
            p.goto(self.base + "/")
            p.wait_for_timeout(300)
            return bool(p.errors)
        self.both("B20", probe)


class Manifest(unittest.TestCase):
    def test_manifest_matches_app(self):
        import yaml
        path = os.path.join(os.path.dirname(shoplite.__file__), "bugs.yaml")
        with open(path, encoding="utf-8") as f:
            ids = {b["id"] for b in yaml.safe_load(f)["bugs"]}
        self.assertEqual(ids, shoplite.ALL_BUGS)
        with open(shoplite.__file__, encoding="utf-8") as f:
            src = f.read()
        for bug_id in shoplite.ALL_BUGS:
            self.assertIn(f'bug("{bug_id}")', src, f"{bug_id} is in the manifest but not planted")


if __name__ == "__main__":
    unittest.main()
