"""QA Agent v2 task board, checker and site map: the rules that are enforced in code."""

import os
import tempfile
import threading
import unittest

from v2.board import BoardError, TaskBoard, missing_proof
from v2.checker import Checker, risk_of
from v2.sitemap import Page, SiteMap, UnknownElement

PROOF = {"screenshot": "s.png", "url": "http://app.test/cart", "server_responses": [{"url": "/api/cart", "status": 200}]}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def small_map() -> SiteMap:
    m = SiteMap(site_url="http://app.test")
    home = m.add_page(Page(route="/", path="/", url="http://app.test/", status=200))
    cart = m.add_page(Page(route="/cart", path="/cart", url="http://app.test/cart", status=200, auth="user"))
    m.add_elements(home, [{"kind": "link", "name": "Cart", "href": "/cart"}, {"kind": "input", "name": "", "field": "q", "type": "search"}])
    m.add_elements(cart, [{"kind": "button", "name": "Place order", "type": "button"}])
    return m


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.b = TaskBoard(clock=self.clock)

    def test_highest_risk_first_and_one_owner(self):
        self.b.add("function", "footer", risk=1)
        hi = self.b.add("function", "checkout", risk=3)
        self.b.add("journey", "flow", risk=3)
        t = self.b.claim("function", "w1")
        self.assertEqual(t["id"], hi)
        self.assertEqual(t["status"], "claimed")
        self.assertEqual(self.b.claim("function", "w2")["title"], "footer")
        self.assertIsNone(self.b.claim("function", "w3"))

    def test_parallel_claims_never_share_a_task(self):
        with tempfile.TemporaryDirectory() as d:
            b = TaskBoard(os.path.join(d, "board.db"))
            for i in range(40):
                b.add("function", f"t{i}")
            got, lock = [], threading.Lock()

            def worker(n):
                while True:
                    t = b.claim("function", f"w{n}")
                    if not t:
                        return
                    with lock:
                        got.append(t["id"])
            ts = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(len(got), 40)
            self.assertEqual(len(set(got)), 40)
            b.close()

    def test_threads_read_while_others_write(self):
        # one shared connection: reads must take the lock too, or rows from another thread's
        # statement break them (seen in a run as "IndexError: tuple index out of range")
        b = TaskBoard()
        errors = []

        def writer(n):
            try:
                for i in range(60):
                    tid = b.add("function", f"w{n}-{i}")
                    t = b.claim("function", f"w{n}")
                    if t:
                        b.submit(t["id"], f"w{n}", PROOF, "PASS")
            except Exception as e:
                errors.append(repr(e))

        def reader():
            try:
                for _ in range(150):
                    b.tasks(); b.counts(); b.idle(); b.follow_ups(); b.get_meta("plan")
            except Exception as e:
                errors.append(repr(e))
        ts = [threading.Thread(target=writer, args=(n,)) for n in range(5)] + [threading.Thread(target=reader) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])

    def test_claim_expires_then_stuck(self):
        tid = self.b.add("function", "slow", max_attempts=2)
        self.b.claim("function", "w1", ttl=60)
        self.clock.t += 61
        self.assertEqual(self.b.expire_claims(), [tid])
        self.assertEqual(self.b.get(tid)["status"], "open")
        self.assertEqual(self.b.claim("function", "w2", ttl=60)["id"], tid)
        self.clock.t += 61
        self.b.expire_claims()
        t = self.b.get(tid)
        self.assertEqual(t["status"], "not_tested")
        self.assertIn("stuck", t["reason"])

    def test_extend_keeps_the_claim(self):
        tid = self.b.add("function", "long")
        self.b.claim("function", "w1", ttl=60)
        self.clock.t += 50
        self.assertTrue(self.b.extend(tid, "w1", ttl=60))
        self.assertFalse(self.b.extend(tid, "w2", ttl=60))
        self.clock.t += 50
        self.assertEqual(self.b.expire_claims(), [])

    def test_loop_guard_release_goes_back_once(self):
        tid = self.b.add("journey", "back button", max_attempts=2)
        self.b.claim("journey", "w1")
        self.assertEqual(self.b.release(tid, "w1", "repeated the same action 3 times"), "open")
        self.b.claim("journey", "w2")
        self.assertEqual(self.b.release(tid, "w2", "repeated again"), "not_tested")


class ProofAndJudgeTests(unittest.TestCase):
    def setUp(self):
        self.b = TaskBoard()

    def test_missing_proof(self):
        self.assertEqual(missing_proof(PROOF), [])
        self.assertEqual(missing_proof({"screenshot": "s.png", "url": "u", "server_responses": []}), [])
        self.assertEqual(missing_proof({"url": "u"}), ["screenshot", "server_responses"])

    def test_no_proof_no_verdict(self):
        tid = self.b.add("function", "coupon")
        self.b.claim("function", "w1")
        self.assertEqual(self.b.submit(tid, "w1", {"url": "u", "screenshot": "s"}, "PASS"), "open")
        self.assertTrue(any(e["event"] == "rejected" for e in self.b.events(tid)))
        self.b.claim("function", "w1")
        self.assertEqual(self.b.submit(tid, "w1", PROOF, "PASS"), "done")

    def test_submit_needs_the_claim(self):
        tid = self.b.add("function", "x")
        self.b.claim("function", "w1")
        with self.assertRaises(BoardError):
            self.b.submit(tid, "w2", PROOF, "PASS")

    def test_judge_never_sees_tester_verdict(self):
        tid = self.b.add("function", "coupon", expected="total drops by 10%")
        self.b.claim("function", "w1")
        self.b.submit(tid, "w1", PROOF, "PASS", confidence=0.99)
        ev = self.b.evidence_for_judge(tid)
        self.assertNotIn("tester_verdict", ev)
        self.assertNotIn("confidence", ev)
        self.assertEqual(ev["expected"], "total drops by 10%")
        self.assertEqual(self.b.judge(tid, "FAIL", "total did not change"), "FAIL")

    def test_low_confidence_goes_to_a_human(self):
        tid = self.b.add("uiux", "overlap")
        self.b.claim("uiux", "w1")
        self.b.submit(tid, "w1", PROOF, "FAIL")
        self.assertEqual(self.b.judge(tid, "FAIL", confidence=0.4), "UNCERTAIN")
        self.assertIn("human", self.b.get(tid)["judge_note"])

    def test_code_results_need_proof_too(self):
        with self.assertRaises(BoardError):
            self.b.add_result("code", "load /", "PASS", {"url": "u"})
        tid = self.b.add_result("code", "load /", "PASS", PROOF, page="/")
        self.assertEqual(self.b.get(tid)["status"], "judged")


class ReuseAndFollowUpTests(unittest.TestCase):
    def setUp(self):
        self.b = TaskBoard()

    def _pass(self, team, page, el, state="user", follow=None):
        tid = self.b.add(team, "t", page=page, element_id=el, user_state=state)
        self.b.claim(team, "w")
        self.b.submit(tid, "w", PROOF, "PASS", follow_up=follow)
        self.b.judge(tid, "PASS")
        return tid

    def test_reuse_only_in_the_same_user_state(self):
        tid = self._pass("function", "/cart", "el-1", "user")
        self.assertEqual(self.b.reusable("/cart", "el-1", "user")["id"], tid)
        self.assertIsNone(self.b.reusable("/cart", "el-1", "anon"))
        self.assertIsNone(self.b.reusable("/cart", "el-2", "user"))

    def test_follow_ups(self):
        tid = self._pass("function", "/checkout", "el-9", follow="button works, but the page looks broken")
        ups = self.b.follow_ups()
        self.assertEqual([u["id"] for u in ups], [tid])
        new = self.b.add("uiux", "checkout layout", page="/checkout", source="follow-up")
        self.b.mark_follow_up_done(tid, new)
        self.assertEqual(self.b.follow_ups(), [])


class CheckerTests(unittest.TestCase):
    def test_gaps_rounds_and_finish(self):
        m, b = small_map(), TaskBoard()
        c = Checker(b, m, max_rounds=1)
        self.assertEqual(len(c.gaps()), 5)                      # 2 pages + 3 elements
        b.add_result("code", "load /", "PASS", PROOF, page="/")
        self.assertEqual(len(c.gaps()), 4)
        r = c.run_round()
        self.assertEqual((r["round"], len(r["added"]), r["final"]), (1, 4, False))
        place = [t for t in b.tasks() if "Place order" in t["title"]][0]
        self.assertEqual((place["team"], place["risk"], place["user_state"]), ("function", 3, "user"))
        self.assertEqual(c.gaps(), [])                          # pending work is not a gap
        r = c.run_round()                                       # nothing new: final
        self.assertTrue(r["final"])
        n = c.finish("time limit")
        self.assertEqual(n, 4)
        self.assertTrue(all(t["status"] in ("judged", "not_tested") for t in b.tasks()))
        self.assertTrue(b.idle())

    def test_round_limit_closes_new_gaps(self):
        m, b = small_map(), TaskBoard()
        c = Checker(b, m, max_rounds=1)
        c.run_round()
        b.close_open("tester crashed")                           # the round-1 work ended without results
        m.add_elements(m.page("/"), [{"kind": "button", "name": "Subscribe"}])
        r = c.run_round()
        self.assertTrue(r["final"])
        self.assertEqual(r["closed"], 1)
        self.assertIn("round limit", [t for t in b.tasks() if "not_tested" == t["status"]][-1]["reason"])

    def test_risk_guess(self):
        self.assertEqual(risk_of("/checkout"), 3)
        self.assertEqual(risk_of("Search"), 2)
        self.assertEqual(risk_of("/about"), 1)


class SiteMapTests(unittest.TestCase):
    def test_ids_stable_and_unknown_rejected(self):
        a, b = small_map(), small_map()
        self.assertEqual(sorted(a.elements), sorted(b.elements))
        eid = next(iter(a.elements))
        self.assertEqual(a.require_element(eid).id, eid)
        with self.assertRaises(UnknownElement):
            a.require_element("el-doesnotexist")

    def test_same_name_twice_gets_two_ids(self):
        m = SiteMap(site_url="http://app.test")
        p = m.add_page(Page(route="/", path="/", url="http://app.test/"))
        els = m.add_elements(p, [{"kind": "button", "name": "Add to cart"}, {"kind": "button", "name": "Add to cart"}])
        self.assertNotEqual(els[0].id, els[1].id)

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            m = small_map()
            m2 = SiteMap.load(m.save(os.path.join(d, "map.json")))
            self.assertEqual(m2.to_dict(), m.to_dict())


if __name__ == "__main__":
    unittest.main()
