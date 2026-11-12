"""Budget & stall controller (failure-inversion §5).

Tracks steps/tokens/wall-clock at session scope with hard caps, and provides
the escalation ladder: retry -> alternate strategy -> reset context ->
BLOCKED -> user question. Progress = new states + resolved assertions, never
repeated actions.
"""

import json
import os
import time

STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "budget.json"
)


class BudgetController:
    def __init__(self, budget_usd: float = 2.0, max_steps: int = 800,
                 max_wall_minutes: int = 90, feature_budget_pct: float = 0.15):
        self.budget_usd = budget_usd
        self.max_steps = max_steps
        self.max_wall = max_wall_minutes * 60
        self.feature_budget_pct = feature_budget_pct
        self.steps = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.usd_spent = 0.0
        self.started = time.time()
        self.visited_states = set()
        self.feature_steps = {}

    # --- tracking ---------------------------------------------------------
    def record_step(self, feature: str = "session") -> bool:
        """Record one action step. Returns False if a cap is exceeded."""
        self.steps += 1
        self.feature_steps[feature] = self.feature_steps.get(feature, 0) + 1
        return self.within_limits(feature)

    def record_tokens(self, tokens_in: int = 0, tokens_out: int = 0, usd: float = 0.0) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.usd_spent += usd

    def visit_state(self, state_id: str) -> bool:
        """Returns True if the state is NEW (progress). Repeats are not progress."""
        if state_id in self.visited_states:
            return False
        self.visited_states.add(state_id)
        return True

    # --- limits -----------------------------------------------------------
    def within_limits(self, feature: str = "session") -> bool:
        if self.steps >= self.max_steps:
            return False
        if time.time() - self.started >= self.max_wall:
            return False
        if self.usd_spent >= self.budget_usd:
            return False
        feat_steps = self.feature_steps.get(feature, 0)
        if feature != "session" and feat_steps > self.max_steps * self.feature_budget_pct:
            return False
        return True

    def limits_report(self) -> str:
        return (
            f"steps={self.steps}/{self.max_steps} usd={self.usd_spent:.3f}/{self.budget_usd} "
            f"wall={int(time.time() - self.started)}s/{self.max_wall}s "
            f"states_discovered={len(self.visited_states)}"
        )

    # --- escalation ladder --------------------------------------------------
    ESCALATION_LEVELS = ["retry_same", "alternate_strategy", "reset_context", "blocked", "user_question"]

    def escalate(self, level: int) -> str:
        """Return the escalation action for a stall. level 0..4."""
        level = max(0, min(4, level))
        return self.ESCALATION_LEVELS[level]

    # --- persistence ---------------------------------------------------------
    def save(self) -> None:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "steps": self.steps, "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out, "usd_spent": self.usd_spent,
                "started": self.started,
            }, f, indent=2)

    def load(self) -> None:
        if not os.path.isfile(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.steps = data.get("steps", 0)
            self.tokens_in = data.get("tokens_in", 0)
            self.tokens_out = data.get("tokens_out", 0)
            self.usd_spent = data.get("usd_spent", 0.0)
            self.started = data.get("started", time.time())
        except (json.JSONDecodeError, OSError):
            pass
