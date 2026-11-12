"""Context-window guards for long agentic browser sessions.

Why this exists
---------------
The navigator runs as ONE long ADK invocation (timeout=7200, dozens of tool
calls). Two facts combine badly:

  1. ADK's event compaction (utils/adk_app.py) only runs *after* an invocation
     finishes -- see runners.py "Run compaction after all events are yielded
     from the agent". It cannot shrink history *during* a marathon turn.
  2. The MCP browser tools (browser_snapshot, browser_network_requests) return
     unbounded payloads. The native tools self-cap (get_page_text 4K,
     get_interactive_elements 30K); the MCP ones did not.

So every snapshot accumulated in the invocation's context until the request hit
ContextWindowExceededError (observed: 380929 input tokens vs DeepSeek's 393216).

Two independent guards, defence in depth:

  cap_tool_response   (after_tool_callback)  -- bounds any SINGLE tool result.
  prune_llm_request   (before_model_callback) -- bounds the CUMULATIVE prompt.

Token counting, not char estimates
----------------------------------
An earlier version of this file estimated 4 chars/token and did nothing while
the request blew the window anyway. Measured on real payloads:

    playwright a11y snapshot -> 2.60 chars/token
    english prose            -> 4.48 chars/token

Snapshots are dense in opaque refs (f3e6, md_17) that tokenize near
one-token-per-fragment. A 4.0 assumption under-counts snapshot history by ~35%,
which put the "safe" budget almost exactly on the failure threshold. We now
count real tokens with tiktoken and fall back to a *pessimistic* 2.0
chars/token only if tiktoken is unavailable.
"""

import json
import logging

logger = logging.getLogger(__name__)

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover - tiktoken ships with litellm
    _ENC = None

# Pessimistic fallback: assume the dense-markup ratio, never the prose one.
FALLBACK_CHARS_PER_TOKEN = 2.0

# Ceiling for prompt input. Sits under BOTH windows in use (Qwen3.8 262144,
# DeepSeek-V4-Flash 393216) with room for a 12288 completion and for the fact
# that cl100k is only an approximation of either model's real tokenizer.
MAX_INPUT_TOKENS = 150_000

# Reserve for things that cost input tokens but are not in `contents`:
# the system instruction and the JSON tool schemas (MCP + native tools).
TOOL_SCHEMA_RESERVE_TOKENS = 10_000

# Per-tool ceilings for a single result, in characters.
TOOL_CHAR_BUDGETS = {
    "browser_snapshot": 20_000,
    "browser_network_requests": 8_000,
    "browser_console_messages": 6_000,
}
DEFAULT_TOOL_CHAR_BUDGET = 20_000

# Most recent tool results always kept verbatim: the model needs the latest
# snapshot's refs to decide its next click. Reduced under pressure, never to 0.
KEEP_RECENT_RESULTS = 6
MIN_KEEP_RECENT = 2

_TRUNC_NOTE = (
    "\n...[truncated by context_guard: {dropped} chars removed. "
    "The head is kept, so element refs above are still valid. "
    "Call get_interactive_elements() for a compact view of the full page.]"
)
_STUB = "[older tool result elided by context_guard to fit the context window]"


def count_tokens(text: str) -> int:
    """Real token count when possible, pessimistic estimate otherwise."""
    if not text:
        return 0
    if _ENC is not None:
        return len(_ENC.encode(text, disallowed_special=()))
    return int(len(text) / FALLBACK_CHARS_PER_TOKEN) + 1


def _truncate(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    return text[:budget] + _TRUNC_NOTE.format(dropped=len(text) - budget)


def cap_tool_response(tool, args, tool_context, tool_response):
    """after_tool_callback: bound one tool result.

    Returns a replacement dict, or None to leave the response untouched.
    """
    budget = TOOL_CHAR_BUDGETS.get(tool.name, DEFAULT_TOOL_CHAR_BUDGET)

    if isinstance(tool_response, str):
        return {"result": _truncate(tool_response, budget)} if len(tool_response) > budget else None

    if not isinstance(tool_response, dict):
        return None

    changed = False
    out = {}
    for key, value in tool_response.items():
        if isinstance(value, str) and len(value) > budget:
            out[key] = _truncate(value, budget)
            changed = True
        else:
            out[key] = value
    return out if changed else None


def _as_text(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def _part_tokens(part) -> int:
    """Token cost of one Content part: text, tool result, AND tool call args.

    The tool-call arm matters: the model's own function_call parts carry args
    (typed text, long selectors) that the previous version never counted.
    """
    total = 0
    text = getattr(part, "text", None)
    if text:
        total += count_tokens(text)

    fr = getattr(part, "function_response", None)
    if fr is not None and getattr(fr, "response", None) is not None:
        total += count_tokens(_as_text(fr.response))

    fc = getattr(part, "function_call", None)
    if fc is not None and getattr(fc, "args", None) is not None:
        total += count_tokens(_as_text(fc.args))

    return total


def _iter_parts(contents):
    for content in contents:
        for part in (getattr(content, "parts", None) or []):
            yield part


def _overhead_tokens(llm_request) -> int:
    """Input tokens that are not in `contents`."""
    overhead = TOOL_SCHEMA_RESERVE_TOKENS
    config = getattr(llm_request, "config", None)
    si = getattr(config, "system_instruction", None) if config is not None else None
    if si:
        overhead += count_tokens(_as_text(si))
    return overhead


def prune_llm_request(callback_context, llm_request):
    """before_model_callback: keep the outgoing prompt under MAX_INPUT_TOKENS.

    Escalates only as far as needed:
      1. stub tool results older than the newest KEEP_RECENT_RESULTS
      2. shrink the retention window toward MIN_KEEP_RECENT
      3. truncate oversized text parts, oldest first

    Returns None so the (mutated) request proceeds normally.
    """
    contents = getattr(llm_request, "contents", None)
    if not contents:
        return None

    overhead = _overhead_tokens(llm_request)
    total = overhead + sum(_part_tokens(p) for p in _iter_parts(contents))
    if total <= MAX_INPUT_TOKENS:
        return None

    started_at = total

    # Tool-result parts, newest first.
    tool_parts = [
        p for p in reversed(list(_iter_parts(contents)))
        if getattr(p, "function_response", None) is not None
        and getattr(p.function_response, "response", None) is not None
    ]

    stub_tokens = count_tokens(_STUB)

    def stub(part):
        nonlocal total
        before = _part_tokens(part)
        if before <= stub_tokens:
            return
        part.function_response.response = {"result": _STUB}
        total -= before - stub_tokens

    # 1 + 2: stub old results, tightening the retention window if needed.
    keep = KEEP_RECENT_RESULTS
    while total > MAX_INPUT_TOKENS and keep >= MIN_KEEP_RECENT:
        for part in tool_parts[keep:]:
            if total <= MAX_INPUT_TOKENS:
                break
            stub(part)
        if total <= MAX_INPUT_TOKENS:
            break
        keep -= 1

    # 3: still over -- truncate big text parts, oldest first.
    if total > MAX_INPUT_TOKENS:
        for part in _iter_parts(contents):
            if total <= MAX_INPUT_TOKENS:
                break
            text = getattr(part, "text", None)
            if not text or count_tokens(text) < 500:
                continue
            before = count_tokens(text)
            part.text = _truncate(text, 2_000)
            total -= before - count_tokens(part.text)

    # 4: last resort -- shrink the RETAINED tool results too.
    #
    # Steps 1-2 never touch the newest results, but a single snapshot can be
    # larger than the whole budget on its own, so keeping even MIN_KEEP_RECENT
    # of them verbatim can still overflow. (cap_tool_response normally bounds
    # each to 20K chars, but this guard must hold even when it has not run --
    # e.g. history replayed from an older session.) Halve an explicit char
    # budget, oldest retained first, so the newest snapshot degrades last.
    budget_chars = DEFAULT_TOOL_CHAR_BUDGET
    while total > MAX_INPUT_TOKENS and budget_chars >= 1_000:
        for part in reversed(tool_parts):  # oldest -> newest
            if total <= MAX_INPUT_TOKENS:
                break
            text = _as_text(part.function_response.response)
            if len(text) <= budget_chars:
                continue
            before = _part_tokens(part)
            part.function_response.response = {"result": _truncate(text, budget_chars)}
            total -= before - _part_tokens(part)
        budget_chars //= 2

    logger.warning(
        "context_guard: pruned prompt %d -> %d tokens (limit %d, kept %d recent results)",
        started_at, total, MAX_INPUT_TOKENS, keep,
    )
    return None
