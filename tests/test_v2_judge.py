"""QA Agent v2 judge: evidence without the tester's verdict, and the verdict rules in code."""

import json
import unittest
from types import SimpleNamespace

from v2.board import TaskBoard
from v2.judge import combine, evidence, judge_task

PROOF = {"screenshot": "missing-file.png", "url": "http://shop.test/cart", "server_responses": [{"method": "POST", "path": "/api/cart/update", "status": 200}]}


def answer(verdict, confidence=0.9):
    args = {"checks": [{"name": "total", "verdict": verdict, "reason": "r"}], "confidence": confidence, "summary": "s"}
    return SimpleNamespace(content="", tool_calls=[SimpleNamespace(id="j1", function=SimpleNamespace(name="verdict", arguments=json.dumps(args)))])


class FakeJudge:
    def __init__(self, reply):
        self.reply, self.seen = reply, []

    def __call__(self, team, msgs, tools=None, **kw):
        assert team == "judge"
        self.seen.append(msgs)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def done_task(board, tester_verdict, code_verdict=None):
    tid = board.add("function", "Cart total with quantity 2", page="/cart", user_state="user",
                    expected="Total = price x quantity")
    board.claim("function", "w")
    obs = dict(PROOF, checks=[{"name": "total", "verdict": tester_verdict, "observed": "qty 2, total $20"}],
               actions=[{"tool": "fill", "name": "Quantity", "input": "2"}], code_verdict=code_verdict,
               code_notes=["code: 500"] if code_verdict else [])
    board.submit(tid, "w", obs, tester_verdict, 0.95)
    return tid


class CombineRules(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(combine("PASS", "FAIL", 0.9, None)[0], "FAIL")
        self.assertEqual(combine("FAIL", "PASS", 0.9, None)[0], "UNCERTAIN")
        self.assertEqual(combine("PASS", "PASS", 0.4, None)[0], "UNCERTAIN")
        self.assertEqual(combine("PASS", "PASS", 0.9, None)[0], "PASS")
        self.assertEqual(combine("PASS", None, 0.0, None)[0], "UNCERTAIN")
        self.assertEqual(combine("FAIL", None, 0.0, None)[0], "FAIL")
        self.assertEqual(combine("PASS", "PASS", 0.9, "FAIL")[0], "FAIL")
        self.assertEqual(combine("PASS", "PASS", 0.9, "BLOCKED")[0], "BLOCKED")


class JudgeOnTheBoard(unittest.TestCase):
    def setUp(self):
        self.b = TaskBoard()

    def test_judge_never_sees_the_tester_verdict(self):
        tid = done_task(self.b, "PASS")
        fake = FakeJudge(answer("FAIL"))
        judge_task(self.b, tid, fake, log=lambda *a: None)
        text = json.dumps(fake.seen[0])
        self.assertNotIn("tester_verdict", text)
        self.assertNotIn('"verdict"', text.split("EVIDENCE")[1])      # no verdict field in the evidence
        self.assertIn("qty 2, total $20", text)                      # the observation is there
        ev = evidence(self.b.evidence_for_judge(tid) if False else {"observed": {"checks": [{"name": "a", "verdict": "PASS", "observed": "o"}]}})
        self.assertEqual(ev["checks_made"], [{"name": "a", "observed": "o"}])

    def test_false_pass_is_caught(self):
        tid = done_task(self.b, "PASS")
        self.assertEqual(judge_task(self.b, tid, FakeJudge(answer("FAIL")), log=lambda *a: None), "FAIL")
        t = self.b.get(tid)
        self.assertEqual(t["status"], "judged")
        self.assertIn("tester: PASS; judge: FAIL", t["judge_note"])

    def test_disagreement_goes_to_a_human(self):
        tid = done_task(self.b, "FAIL")
        self.assertEqual(judge_task(self.b, tid, FakeJudge(answer("PASS")), log=lambda *a: None), "UNCERTAIN")

    def test_no_judge_no_green(self):
        tid = done_task(self.b, "PASS")
        self.assertEqual(judge_task(self.b, tid, FakeJudge(RuntimeError("down")), log=lambda *a: None), "UNCERTAIN")

    def test_code_fact_wins(self):
        tid = done_task(self.b, "FAIL", code_verdict="FAIL")
        self.assertEqual(judge_task(self.b, tid, FakeJudge(answer("PASS")), log=lambda *a: None), "FAIL")

    def test_agreeing_pass_stays_pass(self):
        tid = done_task(self.b, "PASS")
        self.assertEqual(judge_task(self.b, tid, FakeJudge(answer("PASS", 0.9)), log=lambda *a: None), "PASS")

    def test_write_task_without_a_write_is_not_green(self):
        # a success message with no successful server write is a success trap: never a PASS, even over a judge PASS
        tid = self.b.add("journey", "Place an order", page="/checkout", user_state="user",
                         expected="The order is placed and appears in the orders list")
        self.b.claim("journey", "w")
        obs = dict(PROOF, server_responses=[{"method": "GET", "path": "/orders", "status": 200}],
                   checks=[{"name": "order placed", "verdict": "PASS", "observed": "saw 'Order placed!'"}],
                   expected_write=True, had_successful_write=False)
        self.b.submit(tid, "w", obs, "PASS", 0.95)
        self.assertEqual(judge_task(self.b, tid, FakeJudge(answer("PASS", 0.9)), log=lambda *a: None), "UNCERTAIN")


if __name__ == "__main__":
    unittest.main()
