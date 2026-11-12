"""The task board: one SQLite file per run, shared by every team.

Teams never talk to each other. They pin tasks and results here in one format,
and the checker and the report read the same rows. The rules are enforced in
code, not in prompts:

1. Claim before work: claim() hands a task to one worker inside one write
   transaction, so two workers never get the same task.
2. Claims expire: a claim that is not extended or finished in time goes back to
   OPEN. After max_attempts it goes to NOT_TESTED ("stuck"), so nothing loops.
3. Reuse before re-test: reusable() returns an earlier judged result for the
   same page, element and user state.
4. Follow-ups become tasks: follow_ups() lists the notes that the planner must
   turn into tasks; mark_follow_up_done() closes them.
5. No proof, no verdict: submit() rejects a result without a screenshot, a
   landing URL and the server responses; the task goes back to OPEN.
6. The judge never sees the tester's verdict: evidence_for_judge() leaves it out.

Statuses: open -> claimed -> done -> judged, or not_tested with a reason.
Verdicts: PASS, FAIL, BLOCKED, UNCERTAIN (UNCERTAIN = needs a human look).
"""

import json
import os
import sqlite3
import threading
import time

STATUSES = ("open", "claimed", "done", "judged", "not_tested")
VERDICTS = ("PASS", "FAIL", "BLOCKED", "UNCERTAIN")
PROOF_KEYS = ("screenshot", "url", "server_responses")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  team TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'check',
  page TEXT NOT NULL DEFAULT '',
  element_id TEXT,
  covers TEXT NOT NULL DEFAULT '[]',
  user_state TEXT NOT NULL DEFAULT 'anon',
  title TEXT NOT NULL DEFAULT '',
  risk INTEGER NOT NULL DEFAULT 1,
  steps TEXT NOT NULL DEFAULT '[]',
  expected TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','claimed','done','judged','not_tested')),
  claimed_by TEXT,
  claim_expires REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 2,
  observed TEXT,
  tester_verdict TEXT,
  confidence REAL,
  verdict TEXT,
  judge_note TEXT,
  follow_up TEXT,
  follow_up_done INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  round INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'planner',
  created REAL NOT NULL,
  updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(team, status, risk);
CREATE INDEX IF NOT EXISTS tasks_target ON tasks(page, element_id, user_state);
CREATE TABLE IF NOT EXISTS events (ts REAL, task_id TEXT, event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class BoardError(Exception):
    pass


class TaskBoard:
    def __init__(self, path: str = ":memory:", clock=time.time, human_below: float = 0.6):
        self.path = path
        self.clock = clock
        self.human_below = human_below      # judge confidence under this -> UNCERTAIN
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL") if path != ":memory:" else None

    # ---------- internals ----------
    def _tx(self):
        board = self

        class _Tx:
            def __enter__(self_):
                board._lock.acquire()
                board._db.execute("BEGIN IMMEDIATE")
                return board._db

            def __exit__(self_, et, ev, tb):
                try:
                    board._db.execute("ROLLBACK" if et else "COMMIT")
                finally:
                    board._lock.release()
                return False
        return _Tx()

    def _log(self, db, task_id, event, detail=""):
        db.execute("INSERT INTO events VALUES (?,?,?,?)", (self.clock(), task_id, event,
                                                           detail if isinstance(detail, str) else json.dumps(detail)))

    @staticmethod
    def _row(r) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for k in ("steps", "observed", "covers"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    pass
        return d

    def _next_id(self, db, team: str) -> str:
        n = db.execute("SELECT COUNT(*) FROM tasks WHERE team = ?", (team,)).fetchone()[0] + 1
        while True:
            tid = f"{team}-{n:03d}"
            if not db.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone():
                return tid
            n += 1

    # ---------- meta ----------
    def get_meta(self, k: str, default=None):
        with self._lock:
            r = self._db.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_meta(self, k: str, v) -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, json.dumps(v)))

    # ---------- adding ----------
    def add(self, team: str, title: str, *, page: str = "", element_id: str | None = None,
            user_state: str = "anon", kind: str = "check", risk: int = 1, steps=None, expected: str = "",
            round: int = 0, source: str = "planner", max_attempts: int = 2, task_id: str | None = None,
            covers: list | None = None) -> str:
        """Pin a task. The expected result is written here, before any test runs.
        covers: more element IDs that this task tests (the fields of a form)."""
        now = self.clock()
        with self._tx() as db:
            tid = task_id or self._next_id(db, team)
            db.execute("INSERT INTO tasks (id, team, kind, page, element_id, covers, user_state, title, risk, steps,"
                       " expected, round, source, max_attempts, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (tid, team, kind, page, element_id, json.dumps(covers or []), user_state, title, int(risk),
                        json.dumps(steps or []), expected, round, source, max_attempts, now, now))
            self._log(db, tid, "added", {"team": team, "source": source, "round": round})
        return tid

    def add_result(self, team: str, title: str, verdict: str, observed: dict, *, reason: str = "",
                   expected: str = "", page: str = "", element_id: str | None = None, user_state: str = "anon",
                   kind: str = "check", risk: int = 1, steps=None, source: str = "code", round: int = 0) -> str:
        """A result made by plain code (basic checks): pinned, proved and judged at once.
        Code results follow the same proof rule as tester results."""
        verdict = verdict.upper()
        if verdict not in VERDICTS:
            raise BoardError(f"unknown verdict {verdict}")
        missing = missing_proof(observed)
        if missing:
            raise BoardError(f"code result without proof ({', '.join(missing)}): {title}")
        tid = self.add(team, title, page=page, element_id=element_id, user_state=user_state, kind=kind,
                       risk=risk, steps=steps, expected=expected, source=source, round=round)
        now = self.clock()
        with self._tx() as db:
            db.execute("UPDATE tasks SET status='judged', observed=?, tester_verdict=?, verdict=?, confidence=1.0,"
                       " judge_note=?, reason=?, updated=? WHERE id=?",
                       (json.dumps(observed), verdict, verdict, "checked by code", reason, now, tid))
            self._log(db, tid, "judged", {"verdict": verdict, "by": "code"})
        return tid

    # ---------- rule 1 + 2: claims ----------
    def claim(self, team: str, worker: str, ttl: float = 180.0) -> dict | None:
        """Give the highest-risk open task of the team to this worker, or None."""
        self.expire_claims()
        now = self.clock()
        with self._tx() as db:
            # Tasks that destroy shared test data (delete, cancel, clear...) go last, so they cannot take the
            # data away from the other tasks of the run (one tester deleted the order a journey needed).
            r = db.execute("SELECT * FROM tasks WHERE team = ? AND status = 'open'"
                           " ORDER BY (title LIKE '%delete%' OR title LIKE '%remove%' OR title LIKE '%cancel%' OR"
                           " title LIKE '%clear%' OR title LIKE '%reset%' OR title LIKE '%log out%' OR"
                           " title LIKE '%logout%') ASC, risk DESC, round ASC, created ASC, id ASC LIMIT 1",
                           (team,)).fetchone()
            if r is None:
                return None
            db.execute("UPDATE tasks SET status='claimed', claimed_by=?, claim_expires=?, attempts=attempts+1,"
                       " updated=? WHERE id=? AND status='open'", (worker, now + ttl, now, r["id"]))
            self._log(db, r["id"], "claimed", {"worker": worker, "ttl": ttl})
            return self._row(db.execute("SELECT * FROM tasks WHERE id = ?", (r["id"],)).fetchone())

    def extend(self, task_id: str, worker: str, ttl: float = 180.0) -> bool:
        with self._tx() as db:
            cur = db.execute("UPDATE tasks SET claim_expires=?, updated=? WHERE id=? AND status='claimed'"
                             " AND claimed_by=?", (self.clock() + ttl, self.clock(), task_id, worker))
            return cur.rowcount == 1

    def expire_claims(self) -> list[str]:
        """Claims past their time go back to OPEN, or to NOT_TESTED after max_attempts."""
        now = self.clock()
        moved = []
        with self._tx() as db:
            for r in db.execute("SELECT * FROM tasks WHERE status='claimed' AND claim_expires < ?", (now,)).fetchall():
                self._reopen(db, r, f"claim expired (worker {r['claimed_by']} did not finish)")
                moved.append(r["id"])
        return moved

    def _reopen(self, db, r, why: str) -> str:
        if r["attempts"] >= r["max_attempts"]:
            db.execute("UPDATE tasks SET status='not_tested', claimed_by=NULL, claim_expires=NULL, verdict=NULL,"
                       " reason=?, updated=? WHERE id=?", (f"stuck: {why}; tried {r['attempts']} times",
                                                          self.clock(), r["id"]))
            self._log(db, r["id"], "not_tested", why)
            return "not_tested"
        db.execute("UPDATE tasks SET status='open', claimed_by=NULL, claim_expires=NULL, updated=? WHERE id=?",
                   (self.clock(), r["id"]))
        self._log(db, r["id"], "reopened", why)
        return "open"

    def release(self, task_id: str, worker: str, why: str = "stuck") -> str:
        """The loop guard stopped this worker: the task goes back on the board once.
        Returns "open", "not_tested", or "lost" when the claim had already expired."""
        with self._tx() as db:
            r = db.execute("SELECT * FROM tasks WHERE id=? AND status='claimed' AND claimed_by=?",
                           (task_id, worker)).fetchone()
            if r is None:            # the claim expired and the task moved on: nothing to release
                self._log(db, task_id, "release_ignored", {"worker": worker, "why": why})
                return "lost"
            return self._reopen(db, r, why)

    # ---------- rule 5: the proof rule ----------
    def submit(self, task_id: str, worker: str, observed: dict, tester_verdict: str,
               confidence: float = 1.0, follow_up: str | None = None) -> str:
        """Pin a tester's result. Without proof the result is thrown out and the
        task reopens (it counts as an attempt). Returns the new status."""
        with self._tx() as db:
            r = db.execute("SELECT * FROM tasks WHERE id=? AND status='claimed' AND claimed_by=?",
                           (task_id, worker)).fetchone()
            if r is None:
                raise BoardError(f"{task_id} is not claimed by {worker} (claim expired or wrong worker)")
            missing = missing_proof(observed)
            if missing:
                self._log(db, task_id, "rejected", {"missing": missing})
                return self._reopen(db, r, f"no proof, no verdict: missing {', '.join(missing)}")
            v = (tester_verdict or "").upper()
            if v not in VERDICTS:
                raise BoardError(f"unknown verdict {tester_verdict}")
            db.execute("UPDATE tasks SET status='done', observed=?, tester_verdict=?, confidence=?, follow_up=?,"
                       " claimed_by=NULL, claim_expires=NULL, updated=? WHERE id=?",
                       (json.dumps(observed), v, float(confidence), follow_up, self.clock(), task_id))
            self._log(db, task_id, "done", {"worker": worker})
            return "done"

    # ---------- rule 6: the judge ----------
    def evidence_for_judge(self, task_id: str) -> dict:
        """What the judge may see: the task and the evidence, never the tester's verdict."""
        t = self.get(task_id)
        if t is None or t["status"] != "done":
            raise BoardError(f"{task_id} is not waiting for the judge")
        return {k: t[k] for k in ("id", "team", "page", "element_id", "user_state", "title", "steps",
                                  "expected", "observed")}

    def judge(self, task_id: str, verdict: str, note: str = "", confidence: float = 1.0) -> str:
        v = verdict.upper()
        if v not in VERDICTS:
            raise BoardError(f"unknown verdict {verdict}")
        if confidence < self.human_below:
            note = (note + " " if note else "") + f"[judge confidence {confidence:.2f}: needs a human look]"
            v = "UNCERTAIN"
        with self._tx() as db:
            cur = db.execute("UPDATE tasks SET status='judged', verdict=?, judge_note=?, confidence=?, updated=?"
                             " WHERE id=? AND status='done'", (v, note, float(confidence), self.clock(), task_id))
            if cur.rowcount != 1:
                raise BoardError(f"{task_id} is not waiting for the judge")
            self._log(db, task_id, "judged", {"verdict": v})
        return v

    # ---------- rule 3: reuse ----------
    def reusable(self, page: str, element_id: str | None, user_state: str) -> dict | None:
        """The latest judged PASS for the same page, element and user state."""
        with self._lock:
            r = self._db.execute("SELECT * FROM tasks WHERE status='judged' AND verdict='PASS' AND page=? AND"
                                 " user_state=? AND element_id IS ? ORDER BY updated DESC LIMIT 1",
                                 (page, user_state, element_id)).fetchone()
            return self._row(r)

    # ---------- rule 4: follow-ups ----------
    def follow_ups(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM tasks WHERE follow_up IS NOT NULL AND follow_up != ''"
                                    " AND follow_up_done = 0 AND status IN ('done','judged') ORDER BY updated").fetchall()
            return [self._row(r) for r in rows]

    def mark_follow_up_done(self, task_id: str, new_task_id: str | None = None) -> None:
        with self._tx() as db:
            db.execute("UPDATE tasks SET follow_up_done=1 WHERE id=?", (task_id,))
            self._log(db, task_id, "follow_up_done", new_task_id or "")

    # ---------- closing ----------
    def mark_not_tested(self, task_id: str, reason: str) -> None:
        with self._tx() as db:
            db.execute("UPDATE tasks SET status='not_tested', claimed_by=NULL, claim_expires=NULL, reason=?,"
                       " updated=? WHERE id=? AND status IN ('open','claimed')", (reason, self.clock(), task_id))
            self._log(db, task_id, "not_tested", reason)

    def close_open(self, reason: str) -> int:
        """At the time limit or the round limit: every open or claimed task gets NOT_TESTED."""
        with self._lock:
            ids = [r[0] for r in self._db.execute("SELECT id FROM tasks WHERE status IN ('open','claimed')").fetchall()]
        for tid in ids:
            self.mark_not_tested(tid, reason)
        return len(ids)

    # ---------- reading ----------
    def get(self, task_id: str) -> dict | None:
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

    def tasks(self, **where) -> list[dict]:
        sql, args = "SELECT * FROM tasks", []
        if where:
            sql += " WHERE " + " AND ".join(f"{k} IS ?" for k in where)
            args = list(where.values())
        with self._lock:
            return [self._row(r) for r in self._db.execute(sql + " ORDER BY created, id", args).fetchall()]

    def counts(self) -> dict:
        out = {s: 0 for s in STATUSES}
        with self._lock:
            for s, n in self._db.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall():
                out[s] = n
            out["verdicts"] = {v: n for v, n in self._db.execute(
                "SELECT verdict, COUNT(*) FROM tasks WHERE status='judged' GROUP BY verdict").fetchall()}
        return out

    def events(self, task_id: str | None = None) -> list[dict]:
        with self._lock:
            if task_id:
                rows = self._db.execute("SELECT * FROM events WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM events ORDER BY rowid").fetchall()
            return [dict(r) for r in rows]

    def idle(self) -> bool:
        """True when no task is open, claimed or waiting for the judge."""
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('open','claimed','done')").fetchone()[0] == 0

    def close(self) -> None:
        with self._lock:
            self._db.close()


def missing_proof(observed: dict | None) -> list[str]:
    """The proof keys a result lacks. server_responses must be present; an empty
    list is allowed and means the action sent no request."""
    observed = observed or {}
    out = []
    for k in PROOF_KEYS:
        v = observed.get(k)
        if k == "server_responses":
            if not isinstance(v, list):
                out.append(k)
        elif not v:
            out.append(k)
    return out
