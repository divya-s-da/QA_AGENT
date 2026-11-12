"""Rate limiting and retry for LiteLLM calls.

aiRouter.ch limits: 3 parallel requests, 240 req/min, 10M tokens/min.
This module enforces:
  - MIN_INTERVAL_SECONDS spacing between calls (rate per minute)
  - a concurrency semaphore (MAX_CONCURRENT=2 < aiRouter's 3) so parallel
    tool calls from the agent never trip the "Too many concurrent requests"
    error
  - retries with backoff for 429s, connection errors and the concurrency
    limit ("Retry after 60s" -> wait the full 60s)
"""

import asyncio
import threading
import time

MIN_INTERVAL_SECONDS = 1.5
MAX_RETRIES = 5
MAX_TOKENS_DEFAULT = 65536
MAX_CONCURRENT = 2

_lock = asyncio.Lock()
_last_call = {"ts": 0.0}
_sem_async = asyncio.Semaphore(MAX_CONCURRENT)
_sem_sync = threading.BoundedSemaphore(MAX_CONCURRENT)


async def _throttle_async():
    while True:
        async with _lock:
            now = time.monotonic()
            if now - _last_call["ts"] >= MIN_INTERVAL_SECONDS:
                _last_call["ts"] = now
                return
        await asyncio.sleep(0.3)


def _throttle_sync():
    now = time.monotonic()
    wait = MIN_INTERVAL_SECONDS - (now - _last_call["ts"])
    if wait > 0:
        time.sleep(wait)
    _last_call["ts"] = time.monotonic()


def _is_rate_limited(exc) -> bool:
    msg = str(exc).lower()
    return (
        "429" in msg
        or "rate limit" in msg
        or "rate_limit" in msg
        or "ratelimit" in msg
        or "too many requests" in msg
        or "concurrent" in msg
    )


def _retry_wait_seconds(attempt: int, exc) -> float:
    msg = str(exc).lower()
    if "retry after" in msg or "concurrent" in msg:
        return 60.0
    return 10.0 * (attempt + 1)


def _is_retryable(exc) -> bool:
    """Retry on rate limits, the concurrency cap, and transient failures."""
    msg = str(exc).lower()
    if _is_rate_limited(exc):
        return True
    if "unable to get json response" in msg or "expecting value" in msg:
        return True
    if "cannot connect to host" in msg or "getaddrinfo" in msg or "connecterror" in msg:
        return True
    if "timed out" in msg or "timeout" in msg:
        return True
    return False


def install_throttle() -> None:
    """Patch litellm.completion / acompletion with throttling + retry."""
    import litellm
    import google.adk.models.lite_llm as adk_llm

    orig_async = litellm.acompletion
    orig_sync = litellm.completion

    async def patched_acompletion(*args, **kwargs):
        await _throttle_async()
        if "max_tokens" not in kwargs and "max_completion_tokens" not in kwargs:
            kwargs["max_tokens"] = MAX_TOKENS_DEFAULT
        if "reasoning_effort" not in kwargs:
            kwargs["reasoning_effort"] = "low"
        kwargs.setdefault("allowed_openai_params", [])
        if "reasoning_effort" not in kwargs["allowed_openai_params"]:
            kwargs["allowed_openai_params"].append("reasoning_effort")
        async with _sem_async:
            last_exc = None
            for attempt in range(MAX_RETRIES + 1):
                try:
                    return await orig_async(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    if _is_retryable(exc):
                        await asyncio.sleep(_retry_wait_seconds(attempt, exc))
                        continue
                    raise
            raise last_exc

    def patched_completion(*args, **kwargs):
        _throttle_sync()
        if "max_tokens" not in kwargs and "max_completion_tokens" not in kwargs:
            kwargs["max_tokens"] = MAX_TOKENS_DEFAULT
        if "reasoning_effort" not in kwargs:
            kwargs["reasoning_effort"] = "low"
        kwargs.setdefault("allowed_openai_params", [])
        if "reasoning_effort" not in kwargs["allowed_openai_params"]:
            kwargs["allowed_openai_params"].append("reasoning_effort")
        with _sem_sync:
            last_exc = None
            for attempt in range(MAX_RETRIES + 1):
                try:
                    return orig_sync(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    if _is_retryable(exc):
                        time.sleep(_retry_wait_seconds(attempt, exc))
                        continue
                    raise
            raise last_exc

    litellm.acompletion = patched_acompletion
    litellm.completion = patched_completion
    if hasattr(adk_llm, "acompletion"):
        adk_llm.acompletion = patched_acompletion
    if hasattr(adk_llm, "completion"):
        adk_llm.completion = patched_completion
