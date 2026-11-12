"""The judge: decides every tester result from the evidence alone.

- Always on, one thread, takes results in DONE status from the board (matched by task ID).
- Another model family than the testers (llm.TEAM_MODELS["judge"]), and it sees the screenshot.
- It never sees the tester's verdict or confidence: evidence() removes them.
- Code combines the verdicts (combine()), so the product never shows a green it cannot prove:
    code facts first (a 5xx during the task -> FAIL, a blocked write -> BLOCKED);
    judge FAIL -> FAIL; tester FAIL but judge PASS -> UNCERTAIN (a human looks);
    judge PASS with low confidence, or no judge answer -> UNCERTAIN; otherwise the judge's verdict.
"""

import base64
import json
import os
import threading
import time

from . import llm

SEVERITY = {"FAIL": 3, "UNCERTAIN": 2, "BLOCKED": 1, "PASS": 0}
MIN_CONFIDENCE = 0.6

SYSTEM = """You are the judge of a QA team. A tester did one task on a web page. You get the task, the expected
result, the tester's actions and observations, the server responses and a screenshot of the final page.
You do NOT get the tester's own verdict: decide yourself, from the evidence only.
Rules:
- PASS a check only if the evidence proves the expected behaviour: a visible result on the page or the
  screenshot, or a server response that confirms it. A claim with no evidence is UNCERTAIN.
- Reaching a page or opening a form is never a PASS on its own. When the task expects an action (create, save,
  send, submit, add, order, place, delete, update, pay), PASS it only with persisted evidence: a matching
  successful write in the server responses (a non-GET request with a 2xx status), or the data still present
  after a reload. A success message with no such write is a success trap: FAIL it, do not PASS it.
- FAIL a check when the evidence shows wrong behaviour: an error, a wrong number (a total must be
  price x quantity minus any discount; counts must match; a sort must be in order), a success message that
  the server response contradicts, data that is gone after a reload, an input that should be refused but is
  accepted.
- BLOCKED when the check could not be done (tool error, write blocked by the guard, login failed).
- A browser validation message that stops an empty or wrong form is the form working: not a FAIL.
- The expected result was written by a planner who only saw the pages, not the product's spec. If the product
  does something different from it but reasonable and not broken (another message, another page after a
  save), that check is UNCERTAIN, not FAIL. FAIL needs evidence that something is broken or wrong.
- Judge every check that was made. Be strict and brief: one short reason (max 25 words) per check."""

TOOL = [{"type": "function", "function": {"name": "verdict", "description": "Your decision.",
    "parameters": {"type": "object", "properties": {
        "checks": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "verdict": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED", "UNCERTAIN"]},
            "reason": {"type": "string", "description": "the evidence that decides it"}},
            "required": ["name", "verdict", "reason"]}},
        "confidence": {"type": "number", "description": "0..1"},
        "summary": {"type": "string", "description": "one sentence for the report"}},
        "required": ["checks", "confidence", "summary"]}}}]


def evidence(ev: dict) -> dict:
    """The board's judge view, with every verdict-like field of the tester removed."""
    obs = dict(ev.get("observed") or {})
    checks = [{"name": c.get("name", ""), "observed": c.get("observed", "")} for c in obs.get("checks", [])
              if isinstance(c, dict)]
    srv = obs.get("server_responses", [])
    writes_ok = sum(1 for n in srv if isinstance(n, dict) and n.get("method") != "GET"
                    and 200 <= int(n.get("status", 0) or 0) < 300)
    return {"task": {k: ev.get(k) for k in ("id", "title", "expected", "page", "user_state", "steps")},
            "checks_made": checks, "actions": obs.get("actions", []), "final_url": obs.get("url", ""),
            "messages_at_end": obs.get("messages_at_end", []), "server_responses": srv[-25:],
            "successful_writes": writes_ok, "blocked_writes": obs.get("blocked_writes", []),
            "console_errors": obs.get("console", []), "screenshot": obs.get("screenshot", "")}


def combine(tester: str, judge: str | None, confidence: float, code: str | None) -> tuple[str, str]:
    """(final verdict, why). See the module doc for the order of the rules."""
    if code in ("FAIL", "BLOCKED"):
        return code, "code fact"
    if judge is None:
        return ("UNCERTAIN", "no judge answer: a PASS is not shown without the judge") if tester == "PASS" \
            else (tester, "no judge answer")
    if judge == "FAIL":
        return "FAIL", "judge"
    if tester == "FAIL" and judge == "PASS":
        return "UNCERTAIN", "the tester saw a failure that the judge did not confirm: a human decides"
    if judge == "PASS" and confidence < MIN_CONFIDENCE:
        return "UNCERTAIN", f"judge confidence {confidence:.2f}"
    return judge, "judge"


def ask(ev: dict, chat=None) -> dict | None:
    """One judge call. Returns the tool arguments, or None when the model gives no decision."""
    chat = chat or llm.chat
    e = evidence(ev)
    shot = e.pop("screenshot")
    content = [{"type": "text", "text": "EVIDENCE:\n" + json.dumps(e, indent=1, default=str)[:14000]}]
    if shot and os.path.isfile(shot):
        with open(shot, "rb") as f:
            content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(f.read()).decode()}})
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]
    for _ in range(2):
        msg = chat("judge", msgs, TOOL, max_tokens=5000, timeout=120)
        for c in getattr(msg, "tool_calls", None) or []:
            if c.function.name == "verdict":
                try:
                    return json.loads(c.function.arguments or "{}")
                except ValueError:
                    pass
        msgs.append({"role": "user", "content": "Answer with the verdict tool."})
    return None


def judge_task(board, task_id: str, chat=None, log=print) -> str | None:
    t = board.get(task_id)
    if t is None or t["status"] != "done":
        return None
    ev = board.evidence_for_judge(task_id)
    obs = t["observed"] if isinstance(t["observed"], dict) else {}
    try:
        out = ask(ev, chat)
    except Exception as e:
        log(f"  [judge] {task_id}: model error {str(e)[:120]}")
        out = None
    j_verdict = None
    conf = 0.0
    if out:
        verdicts = [str(c.get("verdict", "UNCERTAIN")).upper() for c in out.get("checks", []) if isinstance(c, dict)]
        j_verdict = max(verdicts or ["UNCERTAIN"], key=lambda v: SEVERITY.get(v, 2))
        try:
            conf = max(0.0, min(1.0, float(out.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
    final, why = combine(t["tester_verdict"], j_verdict, conf, obs.get("code_verdict"))
    # Hard floor for the success trap: an action the task expected is never a PASS without a successful write.
    # This holds even over a confident judge PASS, so the product never shows a green write it cannot prove.
    if final == "PASS" and obs.get("expected_write") and not obs.get("had_successful_write"):
        final, why = "UNCERTAIN", "the task expects a write, but no successful server write was seen: possible success trap, a human checks"
    parts = []
    if obs.get("code_notes"):
        parts.append("; ".join(obs["code_notes"]))
    if out:
        parts.append(out.get("summary", ""))
        parts.append("; ".join(f"{c.get('name', '')}: {c.get('verdict', '')} - {c.get('reason', '')}"
                               for c in out.get("checks", []) if isinstance(c, dict)))
    parts.append(f"[tester: {t['tester_verdict']}; judge: {j_verdict or 'none'} ({conf:.2f}); final by {why}]")
    note = " | ".join(p for p in parts if p)[:1500]
    # the board turns a low confidence into UNCERTAIN; combine() already decided, so pass full confidence
    v = board.judge(task_id, final, note, confidence=1.0 if final != "PASS" else max(conf, MIN_CONFIDENCE))
    log(f"  [judge] {task_id} {v} (tester {t['tester_verdict']}, judge {j_verdict or '-'}): {t['title'][:60]}")
    return v


class Judge(threading.Thread):
    """One judge worker. With count > 1, worker i takes the tasks whose ID hashes to i, so no task is judged twice."""
    def __init__(self, board, stop: threading.Event, chat=None, log=print, index: int = 0, count: int = 1):
        super().__init__(name=f"judge-{index + 1}", daemon=True)
        self.board, self.stop, self.chat, self.log = board, stop, chat, log
        self.index, self.count = index, count
        self.judged = 0

    def mine(self, task_id: str) -> bool:
        import zlib
        return zlib.crc32(task_id.encode()) % self.count == self.index

    def drain(self) -> int:
        n = 0
        for t in self.board.tasks(status="done"):
            if not self.mine(t["id"]):
                continue
            if judge_task(self.board, t["id"], self.chat, self.log):
                n += 1
        self.judged += n
        return n

    def run(self):
        while not self.stop.is_set():
            if not self.drain():
                self.stop.wait(1.0)
        self.drain()           # judge what is left after the stop
