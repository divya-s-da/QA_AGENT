"""QA Agent v2 testers: the rules that code enforces around the model.

A scripted fake model drives a real browser against ShopLite, so no API key is
needed. Skipped when no Chromium is installed.
"""

import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "benchmark", "shoplite"))
import app as shoplite  # noqa: E402

from tools import write_guard  # noqa: E402
from v2.board import TaskBoard  # noqa: E402
from v2.crawler import Crawler  # noqa: E402
from v2.tester import Tester  # noqa: E402


def chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


PORT = 18776
SITE = f"http://127.0.0.1:{PORT}"
BOB = ("bob@shoplite.test", "bob-pass-123")


def call(name, **args):
    return SimpleNamespace(id=f"c{time.monotonic_ns()}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def reply(*calls):
    return SimpleNamespace(content="", tool_calls=list(calls))


def find_id(msgs, text, kind=""):
    """The element ID on the last ELEMENTS list whose line contains text (and the kind)."""
    for m in reversed(msgs):
        c = m.get("content") or ""
        if "ELEMENTS:" in c:
            for line in c.split("ELEMENTS:")[1].splitlines():
                if text in line and (not kind or f" | {kind} | " in f" {line} "):
                    return line.split(" | ")[0].strip()
    return None


class Script:
    """A fake model: a list of functions, each msgs -> reply. Records what it saw."""
    def __init__(self, *steps):
        self.steps, self.seen = list(steps), []

    def __call__(self, team, msgs, tools=None, **kw):
        self.seen.append(json.loads(json.dumps(msgs)))
        return self.steps.pop(0)(msgs) if self.steps else reply(call("finish", checks=[], confidence=0.1))


@unittest.skipUnless(chromium_available(), "Chromium for Playwright is not installed")
class TesterRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.srv = shoplite.serve(PORT, {"B01"})
        cls.tmp = tempfile.TemporaryDirectory()
        write_guard.configure(allow_writes=False)
        crawl_board = TaskBoard()
        cls.map = Crawler(SITE, cls.tmp.name, crawl_board, email=BOB[0], password=BOB[1], log=lambda *a: None).run()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()
        write_guard.configure(allow_writes=False)

    def setUp(self):
        shoplite.reset_db()
        shoplite.DB["carts"]["bob@shoplite.test"] = {1: 1}
        self.board = TaskBoard()

    def make_tester(self, script):
        return Tester("function-t", "function", self.board, self.map, threading.Lock(), email=BOB[0], password=BOB[1],
                      deadline=time.time() + 600, stop=threading.Event(), out_dir=self.tmp.name,
                      log=lambda *a: None, chat=script)

    def place_order_task(self):
        tid = self.board.add("function", "Test button 'Place order' on /cart", page="/cart", user_state="user",
                             expected="The order is placed and saved.", risk=3)
        return self.board.claim("function", "function-t"), tid

    def test_b01_trap_becomes_fail_from_the_server_response(self):
        write_guard.configure(allow_writes=True)
        try:
            script = Script(lambda m: reply(call("click", element_id=find_id(m, '"Place order"'))),
                            lambda m: reply(call("finish", confidence=0.9, checks=[
                                {"name": "place order", "verdict": "PASS", "observed": "toast 'Order placed! Thank you.'"}])))
            task, tid = self.place_order_task()
            self.make_tester(script).run_task(self.browser, task)
        finally:
            write_guard.configure(allow_writes=False)
        t = self.board.get(tid)
        self.assertEqual(t["status"], "judged")
        self.assertEqual(t["verdict"], "FAIL", t["judge_note"])
        self.assertIn("500", t["judge_note"])
        self.assertEqual(t["tester_verdict"], "FAIL")
        obs = t["observed"]
        self.assertTrue(os.path.isfile(obs["screenshot"]))
        self.assertTrue(obs["url"].startswith(SITE))
        self.assertTrue(any(r["path"] == "/api/orders" and r["status"] == 500 for r in obs["server_responses"]))
        self.assertTrue(any("Order placed" in m for m in json.dumps(script.seen[-1]).split("MESSAGES ON SCREEN")[1:]))

    def test_read_only_write_becomes_blocked(self):
        script = Script(lambda m: reply(call("click", element_id=find_id(m, '"Place order"'))),
                        lambda m: reply(call("finish", confidence=0.9, checks=[
                            {"name": "place order", "verdict": "PASS", "observed": "toast"}])))
        task, tid = self.place_order_task()
        self.make_tester(script).run_task(self.browser, task)
        t = self.board.get(tid)
        self.assertEqual(t["verdict"], "BLOCKED", t["judge_note"])
        self.assertIn("POST /api/orders", t["judge_note"])
        self.assertEqual(shoplite.DB["orders"][-1]["id"], 1000)        # nothing was written

    def test_unknown_element_is_rejected(self):
        script = Script(lambda m: reply(call("click", element_id="el-0000000000")),
                        lambda m: reply(call("finish", confidence=0.9, checks=[
                            {"name": "x", "verdict": "UNCERTAIN", "observed": "rejected"}])))
        task, tid = self.place_order_task()
        self.make_tester(script).run_task(self.browser, task)
        tool_msgs = [m for m in script.seen[-1] if m.get("role") == "tool"]
        self.assertIn("REJECTED", tool_msgs[0]["content"])
        self.assertIn("not on the site map", tool_msgs[0]["content"])

    def test_loop_guard_releases_the_task(self):
        click = lambda m: reply(call("click", element_id=find_id(m, '"Continue shopping"')))  # noqa: E731
        task, tid = self.place_order_task()
        self.make_tester(Script(click, click, click, click)).run_task(self.browser, task)
        t = self.board.get(tid)
        self.assertEqual(t["status"], "open")
        self.assertTrue(any("loop guard" in (e["detail"] or "") for e in self.board.events(tid)))

    def test_low_confidence_goes_to_a_human(self):
        script = Script(lambda m: reply(call("finish", confidence=0.3, checks=[
            {"name": "layout", "verdict": "PASS", "observed": "looks fine"}])))
        task, tid = self.place_order_task()
        self.make_tester(script).run_task(self.browser, task)
        self.assertEqual(self.board.get(tid)["verdict"], "UNCERTAIN")

    def test_login_form_can_be_tested_read_only(self):
        self.assertIn("/login", self.map.login_posts)
        script = Script(lambda m: reply(call("fill", element_id=find_id(m, "type=email"), text="bob@shoplite.test"),
                                        call("fill", element_id=find_id(m, "type=password"), text="wrong-password")),
                        lambda m: reply(call("click", element_id=find_id(m, '"Log in"', "button"))),
                        lambda m: reply(call("finish", confidence=0.9, checks=[
                            {"name": "wrong password refused", "verdict": "PASS", "observed": "Wrong email or password"}])))
        tid = self.board.add("function", "Test the form on /login", page="/login", user_state="anon")
        self.make_tester(script).run_task(self.browser, self.board.claim("function", "function-t"))
        t = self.board.get(tid)
        self.assertEqual(t["verdict"], "PASS", t["judge_note"])
        self.assertTrue(any(r["method"] == "POST" and r["path"] == "/login" for r in t["observed"]["server_responses"]),
                        json.dumps([m for m in script.seen[-1] if m["role"] == "tool"])[:1500])
        self.assertEqual(t["observed"]["blocked_writes"], [])

    def test_observe_is_not_rejected(self):
        script = Script(lambda m: reply(call("observe")),
                        lambda m: reply(call("finish", confidence=0.9, checks=[{"name": "x", "verdict": "PASS", "observed": "-"}])))
        task, tid = self.place_order_task()
        self.make_tester(script).run_task(self.browser, task)
        tool = [m for m in script.seen[-1] if m.get("role") == "tool"][0]["content"]
        self.assertNotIn("REJECTED", tool)
        self.assertIn("ELEMENTS:", tool)

    def test_logout_task_does_not_leave_a_dead_login(self):
        tester = self.make_tester(Script(lambda m: reply(call("click", element_id=find_id(m, '"Log out"'))),
                                         lambda m: reply(call("finish", confidence=0.9, checks=[
                                             {"name": "log out", "verdict": "PASS", "observed": "login page"}]))))
        tid = self.board.add("function", "Test button 'Log out' on /cart", page="/cart", user_state="user")
        tester.run_task(self.browser, self.board.claim("function", "function-t"))
        self.assertNotIn("user", tester._ctx)                  # the logged-out browser was thrown away
        tester.chat = Script(lambda m: reply(call("finish", confidence=0.9, checks=[
            {"name": "cart opens", "verdict": "PASS", "observed": "cart"}])))
        tid2 = self.board.add("function", "Test the cart page", page="/cart", user_state="user")
        tester.run_task(self.browser, self.board.claim("function", "function-t"))
        self.assertEqual(urlparse(self.board.get(tid2)["observed"]["url"]).path, "/cart")   # logged in again

    def test_goto_outside_the_map_is_rejected(self):
        script = Script(lambda m: reply(call("goto", path="/secret-admin-panel")),
                        lambda m: reply(call("finish", confidence=0.9, checks=[{"name": "x", "verdict": "PASS", "observed": "-"}])))
        task, tid = self.place_order_task()
        self.make_tester(script).run_task(self.browser, task)
        tool_msgs = [m for m in script.seen[-1] if m.get("role") == "tool"]
        self.assertTrue(re.search(r"REJECTED: /secret-admin-panel is not a page", tool_msgs[0]["content"]))


if __name__ == "__main__":
    unittest.main()
