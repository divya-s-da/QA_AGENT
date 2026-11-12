"""Model calls for the v2 teams. One model per team, one setting each.

All calls go through LiteLLM to an OpenAI-compatible endpoint (aiRouter by
default). The keys come from the environment or .env and are never logged.
Override a team's model with V2_MODEL_<TEAM>, e.g. V2_MODEL_FUNCTION=Qwen3.6.

This module does not import config.py: that file loads Google ADK, which the
v2 runtime does not need.
"""

import os
import threading
import time
from contextlib import contextmanager

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Testers: cheap and fast. Judge: another model family than the testers.
# UI and UX: reads images. Planner: strong reasoning, few calls.
TEAM_MODELS = {
    "function": "DeepSeek-V4-Flash",
    "journey": "DeepSeek-V4-Flash",
    "uiux": "Qwen3.8",
    "planner": "Qwen3.8",            # the overview: few calls, needs reasoning
    "planner_pages": "DeepSeek-V4-Flash",   # one call per page: many calls, needs speed
    "judge": "Qwen3.8",
    "reporter": "Qwen3.6",
}

_usage_lock = threading.Lock()
USAGE: dict[str, dict] = {}


def _keys() -> list[str]:
    """Every model key, in order. The provider caps concurrent calls per key, so
    more keys = more real capacity. Sources (deduped, order kept):
      AIROUTER_API_KEYS  comma-separated list
      AIROUTER_API_KEY   the primary key (or legacy AI_ROUTER_API)
      AIROUTER_API_KEY_2, _3, ...  numbered extra keys
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(HERE, ".env"))
    except Exception:
        pass
    keys: list[str] = []

    def add(k):
        k = (k or "").strip()
        if k and k not in keys:
            keys.append(k)

    for k in os.getenv("AIROUTER_API_KEYS", "").split(","):
        add(k)
    add(os.getenv("AIROUTER_API_KEY", "") or os.getenv("AI_ROUTER_API", ""))
    i = 2
    while os.getenv(f"AIROUTER_API_KEY_{i}"):
        add(os.getenv(f"AIROUTER_API_KEY_{i}"))
        i += 1
    return keys


def _base() -> str:
    return os.getenv("AIROUTER_BASE_URL", "https://api.airouter.ch/v1")


def _per_key_slots() -> int:
    # aiRouter answers ~3-6 calls at once in a few seconds; more than that and some wait over a minute.
    return max(1, int(os.getenv("V2_MAX_CONCURRENT_CALLS", "6")))


class _Pool:
    """One bounded semaphore per key. A call takes a free slot from any key
    (round-robin), so total concurrency = keys x per-key slots."""

    def __init__(self, keys: list[str], per_key: int):
        self.items = [[k, threading.BoundedSemaphore(per_key)] for k in keys]
        self.rr = 0
        self.lock = threading.Lock()

    @contextmanager
    def slot(self):
        n = len(self.items)
        with self.lock:
            start = self.rr
            self.rr = (self.rr + 1) % n
        picked = None
        for off in range(n):                       # try every key once without waiting
            k, sem = self.items[(start + off) % n]
            if sem.acquire(blocking=False):
                picked = (k, sem)
                break
        if picked is None:                         # all keys busy: wait on the round-robin pick
            k, sem = self.items[start]
            sem.acquire()
            picked = (k, sem)
        try:
            yield picked[0]
        finally:
            picked[1].release()


_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    with _pool_lock:
        keys = _keys()
        if _pool is None or len(_pool.items) != len(keys):
            _pool = _Pool(keys, _per_key_slots()) if keys else _Pool([], _per_key_slots())
        return _pool


def key_count() -> int:
    return len(_keys())


def model_for(team: str) -> str:
    return os.getenv(f"V2_MODEL_{team.upper()}", "") or TEAM_MODELS.get(team, TEAM_MODELS["function"])


def available() -> bool:
    return bool(_keys())


def chat(team: str, messages: list, tools: list | None = None, max_tokens: int = 1500,
         timeout: float = 45, retries: int = 2):
    """One completion for a team. Returns the litellm message object."""
    import litellm
    litellm.suppress_debug_info = True
    pool = _get_pool()
    if not pool.items:
        raise RuntimeError("no model key: set AIROUTER_API_KEY (or AI_ROUTER_API) in .env")
    base = _base()
    model = model_for(team)
    last = None
    for attempt in range(retries + 1):
        try:
            with pool.slot() as key:
                t0 = time.monotonic()
                r = litellm.completion(model=f"openai/{model}", api_base=base, api_key=key, messages=messages,
                                       tools=tools, max_tokens=max_tokens, timeout=timeout)
            u = getattr(r, "usage", None)
            with _usage_lock:
                s = USAGE.setdefault(team, {"model": model, "calls": 0, "prompt_tokens": 0,
                                            "completion_tokens": 0, "seconds": 0.0})
                s["calls"] += 1
                s["seconds"] = round(s["seconds"] + time.monotonic() - t0, 1)
                if u:
                    s["prompt_tokens"] += getattr(u, "prompt_tokens", 0) or 0
                    s["completion_tokens"] += getattr(u, "completion_tokens", 0) or 0
            return r.choices[0].message
        except Exception as e:          # network or provider error
            last = e
            status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
            # A bad request (too long, malformed, auth) returns the same error every time: do not waste
            # generations on a blind retry. Retry only transient errors (timeout, rate limit, 5xx, network).
            if status in (400, 401, 403, 404, 413, 422):
                break
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"model call failed for {team} ({model}): {type(last).__name__}: {str(last)[:200]}")
