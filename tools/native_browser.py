"""Native compact DOM tools over CDP.

Playwright MCP's browser_snapshot returns the whole accessibility tree, which
is slow and bloats the model context. These tools (inspired by testzeus-hercules)
inject stable data-md ids into interactive elements and return only compact,
actionable fields — far smaller payloads, faster turns.
"""

import json

from .browser_server import CDP_URL


def _with_page(fn):
    """Connect over CDP, run fn(page), return its result as a JSON-safe string.

    Playwright's sync API refuses to start inside a thread with a running
    asyncio event loop ("Playwright Sync API inside asyncio loop"). ADK calls
    sync tool functions directly on its event-loop thread, so when a loop is
    running the work is handed to a dedicated worker thread (no loop there)
    and the caller awaits its result — the same pattern the sync API itself
    uses internally, just scoped to one call.
    """
    import asyncio
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    if not in_loop:
        return _sync_with_page(fn)

    # Avoid the executor context manager: its shutdown waits for the worker,
    # defeating the timeout when Playwright is stuck in a CDP call.
    ex = _executor()
    future = ex.submit(_sync_with_page, fn)
    try:
        result = future.result(timeout=_PAGE_OP_TIMEOUT)
    except TimeoutError:
        future.cancel()
        ex.shutdown(wait=False, cancel_futures=True)
        return f"ERROR: browser operation timed out after {_PAGE_OP_TIMEOUT}s"
    except BaseException:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown(wait=False, cancel_futures=True)
    return result


_PAGE_OP_TIMEOUT = 120


def _executor():
    import concurrent.futures
    return concurrent.futures.ThreadPoolExecutor(max_workers=1)


def _sync_with_page(fn):
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(CDP_URL)
            except Exception as e:
                return f"ERROR: cannot connect to browser at {CDP_URL}: {e}"
            page = _find_page(browser)
            if page is None:
                return "ERROR: no browser page found (navigate to a URL first)"
            return fn(page)
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


def _find_page(browser):
    for context in browser.contexts:
        for page in context.pages:
            if page.url and page.url != "about:blank":
                return page
    for context in browser.contexts:
        if context.pages:
            return context.pages[0]
    return None


_INJECT_JS = """
() => {
  const sel = 'a, button, input, select, textarea, [role="button"], [role="link"], ' +
              '[role="textbox"], [role="combobox"], [tabindex]:not([tabindex="-1"])';
  let n = 0;
  document.querySelectorAll('[data-md]').forEach(el => {
    const m = /^md_(\\d+)$/.exec(el.dataset.md);
    if (m) n = Math.max(n, parseInt(m[1], 10) + 1);
  });
  document.querySelectorAll(sel).forEach(el => {
    if (!el.dataset.md) { el.dataset.md = 'md_' + n; n++; }
  });
}
"""

_INJECT_AND_COLLECT_JS = """
() => {
  const sel = 'a, button, input, select, textarea, [role="button"], [role="link"], ' +
              '[role="textbox"], [role="combobox"], [tabindex]:not([tabindex="-1"])';
  let n = 0;
  document.querySelectorAll('[data-md]').forEach(el => {
    const m = /^md_(\\d+)$/.exec(el.dataset.md);
    if (m) n = Math.max(n, parseInt(m[1], 10) + 1);
  });
  document.querySelectorAll(sel).forEach(el => {
    if (!el.dataset.md) { el.dataset.md = 'md_' + n; n++; }
  });
  const out = [];
  document.querySelectorAll('[data-md]').forEach(el => {
    const md = el.dataset.md;
    const item = { md, role: el.getAttribute('role') || el.tagName.toLowerCase(), tag: el.tagName.toLowerCase() };
    const label = el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                  (el.innerText || el.value || '').trim().slice(0, 60);
    if (label) item.name = label;
    if (el.getAttribute('placeholder')) item.placeholder = el.getAttribute('placeholder');
    if (el.tagName === 'SELECT') item.options = Array.from(el.options).map(o => o.text.trim()).slice(0, 20);
    if (el.disabled) item.disabled = true;
    if (el.checked) item.checked = true;
    // Never hand a typed password to the model; say only that one is filled.
    if (el.type === 'password') { if (el.value) item.value = '[filled]'; }
    else if (el.value && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) item.value = String(el.value).slice(0, 40);
    out.push(item);
  });
  return JSON.stringify(out);
}
"""


def _locator_for(page, md_id):
    """Re-inject md ids (so ids exist even if no sensing call preceded this
    action), then resolve the element by its md id."""
    try:
        page.evaluate(_INJECT_JS)
    except Exception:
        pass
    return page.locator(f'[data-md="{md_id}"]')


def get_interactive_elements() -> str:
    """Return compact JSON of interactive elements on the current page.

    Each element has an md id (e.g. md_0) that native_click / native_type
    accept, plus role, tag, name/label, placeholder, options and state.
    Much smaller than browser_snapshot - prefer this for page sensing.
    """
    def _run(page):
        raw = str(page.evaluate(_INJECT_AND_COLLECT_JS))
        return _cap_element_dump(raw)
    return _with_page(_run)


_ELEMENT_DUMP_CHAR_BUDGET = 30000


def _cap_element_dump(raw: str) -> str:
    """Keep the dump under a token budget WITHOUT breaking clickability.

    Uncapped dumps on heavy pages were 10-50K chars per call and every call
    stays in session history forever -> ContextWindowExceededError on long
    runs. Truncates whole elements only (never mid-JSON) so all remaining
    md ids stay valid for native_click/native_type.
    """
    import json as _json
    if len(raw) <= _ELEMENT_DUMP_CHAR_BUDGET:
        return raw
    try:
        data = _json.loads(raw)
    except Exception:
        return raw[:_ELEMENT_DUMP_CHAR_BUDGET] + "\n...[truncated]"
    if not isinstance(data, list):
        return raw[:_ELEMENT_DUMP_CHAR_BUDGET] + "\n...[truncated]"
    keep: list = []
    budget = _ELEMENT_DUMP_CHAR_BUDGET
    for item in data:
        piece = _json.dumps(item)
        if budget - len(piece) < 0:
            break
        keep.append(item)
        budget -= len(piece)
    return (
        _json.dumps(keep)
        + f"\n...[showing {len(keep)} of {len(data)} interactive elements "
        + "(char budget). If you need an element beyond this list, scroll or "
        + "navigate closer to it and call get_interactive_elements again.]"
    )


def native_click(md_id: str) -> str:
    """Click the element identified by its md id (from get_interactive_elements)."""
    if not md_id:
        return "ERROR: md_id is required"
    def _run(page):
        loc = _locator_for(page, md_id)
        try:
            loc.wait_for(state="visible", timeout=5000)
            loc.click(timeout=5000)
            return f"clicked {md_id}"
        except Exception:
            try:
                page.evaluate(f"document.querySelector('[data-md=\"{md_id}\"]').click()")
                return f"clicked {md_id} (js fallback)"
            except Exception as e:
                return f"click failed for {md_id}: {e}"
    return _with_page(_run)


def native_type(md_id: str, text: str) -> str:
    """Fill text into the element identified by its md id."""
    if not md_id:
        return "ERROR: md_id is required"
    def _run(page):
        loc = _locator_for(page, md_id)
        try:
            loc.fill(text, timeout=5000)
            return f"typed into {md_id}"
        except Exception:
            try:
                loc.click(timeout=3000)
                page.keyboard.type(text)
                return f"typed into {md_id} (fallback)"
            except Exception as e:
                return f"type failed for {md_id}: {e}"
    return _with_page(_run)


def get_page_text() -> str:
    """Return the cleaned visible text of the current page (truncated)."""
    def _run(page):
        try:
            text = page.inner_text("body")
        except Exception:
            text = ""
        if not text:
            text = page.evaluate("document.body ? document.body.innerText : ''")
        text = "\n".join(line.strip() for line in str(text).splitlines() if line.strip())
        if len(text) > 4000:
            text = text[:4000] + "\n...[truncated]"
        return text or "ERROR: no visible text on page"
    return _with_page(_run)


def get_current_url() -> str:
    """Return the current page URL."""
    def _run(page):
        return page.url or "ERROR: unknown"
    return _with_page(_run)
