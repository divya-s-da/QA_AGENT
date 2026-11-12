"""ADK web exposure for the QA navigator agent.

Same agent as the pipeline uses, plus the build_docx_report tool so an
interactive session can hand back a Word report with images. The model comes
from react_qa_agent.config (aiRouter.ch, flat-rate).
"""

import json
import os
import sys
import threading
from pathlib import Path

# Repo folder IS the `react_qa_agent` package; its parent goes on the path (works on Windows and in Docker).
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from google.adk.agents import Agent
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

from react_qa_agent.prompts.navigator import NAVIGATOR_PROMPT
from react_qa_agent.config import LLM_MODEL_DEEPSEEK
from react_qa_agent.tools.mcp_runtime import configure_mcp_runtime
import sys as _sys

_repo_root = str(Path(__file__).resolve().parents[2])
if _repo_root not in _sys.path:
    _sys.path.insert(0, _repo_root)

from adk_agents.visual_auditor.agent import visual_auditor_agent
from react_qa_agent.utils.adk_app import build_compacting_app
from react_qa_agent.tools.context_guard import cap_tool_response, prune_llm_request
from react_qa_agent.tools.playwright_screenshot import take_screenshot
from react_qa_agent.tools.evidence_tools import record_evidence, coverage_report
from react_qa_agent.tools.spec_library import list_specs, search_specs, read_spec
from react_qa_agent.tools.report_builder import build_docx_report
from react_qa_agent.tools.invariants import check_page_invariants
from react_qa_agent.tools.visual_probe import visual_probe
from react_qa_agent.tools.native_browser import (
    get_interactive_elements, native_click, native_type,
    get_page_text, get_current_url,
)
from react_qa_agent.tools.screenshot_tool import set_web_session
from react_qa_agent.tools.site_guard import lock_host, allowed_host, is_allowed, current_off_target
from react_qa_agent.tools import write_guard
from react_qa_agent.tools.site_login import site_login

WEB_INSTRUCTION = """

INTERACTIVE SESSION MODE (ADK web):
- TOOL NAMES ARE EXACT. Call ONLY these tools and NEVER invent, shorten,
  or approximate a tool name:
    take_screenshot, record_evidence, coverage_report, build_docx_report,
    list_specs, search_specs, read_spec,
    get_interactive_elements, native_click, native_type, get_page_text,
    get_current_url, check_page_invariants, visual_probe,
    site_login, get_blocked_writes,
    browser_navigate, browser_snapshot, browser_click, browser_type,
    browser_network_requests, browser_file_upload, browser_handle_dialog,
    browser_press_key, browser_navigate_back, browser_wait_for.
  If you are unsure which tool fits, re-read this list instead of guessing
  (there is no take_pseudo or any other tool outside this list).
- The user will give you a site URL or a feature to test. Treat every session
  as a deep human-grade test: follow the BRANCHED TESTING METHODOLOGY above,
  including the COVERAGE INVENTORY RULE and the VISUAL/THEME branch (branch 8).
- SPECIFICATIONS: call list_specs() at the start of every session. If any
  document is listed, the product has a written spec and it OUTRANKS your
  assumptions. Before testing a feature, search_specs() for the rule that
  governs it and read_spec() around the hit. Declare `expected` from the
  spec's own words BEFORE you act, and cite it as
  expected_source='spec:<document>:<line>'. A behaviour that contradicts the
  spec is a FAIL even if it looks reasonable; a behaviour the spec does not
  mention is an observation, not a bug — say so in the note. If list_specs()
  reports none, test against the charter, HTTP semantics and WCAG as before.
- MANDATORY FIRST STEP — build the inventory: open every nav/sidebar/menu/
  footer/user-dropdown entry, record the full numbered list as check_id
  'inventory', then test every row before you declare the session complete.
- You have a specialist sub-agent: visual_auditor (Qwen3.8). For dedicated
  dark/light theme audits or deep visual passes, transfer to it; it reports
  back with evidence. Functional/logic testing stays with you.
- LOGIN: navigate to the site's login page and call site_login(). You are
  not given the test account's credentials, on purpose. NEVER ask the user
  for a password, and if the user pastes one into the chat, do not type it -
  tell them to put it in the server's .env (SITE_EMAIL / SITE_PASSWORD).
- Test ONLY the site the user gave you. Never wander to other websites —
  the     browser tools block off-site navigation.
- Explore the ACTUAL rendered user interface — inspect what is really on the
  page (get_interactive_elements, get_page_text), take screenshots of each
  branch you test, and record evidence for every branch with record_evidence.
- COVERAGE GATE (MANDATORY BEFORE REPORTING): call coverage_report() before
  build_docx_report(). If it returns verdict INCOMPLETE, you are NOT done:
  test every route in untested_routes (with at least one real interaction —
  click, type, submit, toggle — not just a render check) and add interaction
  checks to every route in render_only_routes. Only after it returns COMPLETE
  (or you have documented exactly why a route cannot be exercised) may you
  produce the report.
- CLOSE-OUT ORDER (do not skip and do not reorder): (1) the PERSISTENCE
  SWEEP — revisit every screen where you created something and confirm it is
  still there; (2) coverage_report() until it returns COMPLETE; (3) confirm
  every screen has its '<screen>_visual' check recorded; only then (4) the
  report. A screen you only looked at is not a screen you tested.
- At the END of your testing, call build_docx_report() to produce a Word
  document that includes every verdict and every screenshot you captured.
  Tell the user the DOCX file path when it is saved.
"""

configure_mcp_runtime()

# ---------------------------------------------------------------------------
# Stall / loop guard (failure-inversion §5, same logic as the CLI pipeline).
# The ADK web session has no stall controller, so an agent stuck repeating
# the same tool call would burn the whole session. Track consecutive
# identical (tool, args) pairs per session; on the 3rd repeat, short-circuit
# the tool call and hand the model a corrective instruction instead.
# ---------------------------------------------------------------------------

_STALL_THRESHOLD = 3
_PROGRESS_TOOLS = {"record_evidence", "take_screenshot", "get_page_text"}
_NON_BROWSER_TOOLS = {
    "record_evidence", "build_docx_report", "get_current_url",
    "browser_network_requests", "browser_handle_dialog",
    "browser_file_upload", "browser_wait_for", "browser_navigate_back",
}

_stall_lock = threading.Lock()
_stall_state = {}  # session_id -> {"last": (tool, args_sig) | None, "count": int}


def _stall_guard(tool, args, tool_context):
    """BeforeToolCallback: block off-site navigation AND a 3rd consecutive
    identical tool call. Returns a corrective tool response (so ADK skips the
    real call) or None (proceed normally).
    """
    name = tool.name
    session = getattr(tool_context, "session", None)
    # ADK Session exposes `.id` (not `.session_id`); fall back to "default" only
    # when no real session is attached (CLI/pipeline runs set their own context).
    session_id = getattr(session, "id", None) or "default"
    set_web_session(session_id)

    gate = write_guard.tool_gate(name)
    if gate:
        return gate

    # TARGET-SITE ENFORCEMENT: the first browser_navigate locks the host;
    # any navigation elsewhere is rejected, and no tool may act on a page
    # that is off the target site.
    url = args.get("url") if isinstance(args, dict) else None
    if name == "browser_navigate":
        lock_host(session_id, url or "")
        if not is_allowed(session_id, url or ""):
            return {
                "error": (
                    f"BLOCKED: '{url}' is not the target site (allowed host: "
                    f"{allowed_host(session_id)}). You must test ONLY the site "
                    "you were given. Use browser_navigate to return to the "
                    "target site."
                )
            }
    elif name not in _NON_BROWSER_TOOLS:
        off, current = current_off_target(session_id)
        if off:
            return {
                "error": (
                    f"BLOCKED: you are on an external page ({current}). Navigate "
                    f"back to the target site ({allowed_host(session_id)}) before "
                    "continuing. Never test or interact with other websites."
                )
            }

    if name in _PROGRESS_TOOLS:
        return None  # evidence/screenshot calls are progress, not loop actions

    args_sig = json.dumps(args, sort_keys=True, default=str)[:120]
    with _stall_lock:
        entry = _stall_state.get(session_id)
        if entry and entry[0] == name and entry[1] == args_sig:
            entry[2] += 1
        else:
            _stall_state[session_id] = [name, args_sig, 1]
            return None
        if entry[2] >= _STALL_THRESHOLD:
            return {
                "error": (
                    f"STALL DETECTED: you called {name} with identical args "
                    f"{_STALL_THRESHOLD} times in a row ({json.dumps(args)[:120]}). "
                    "This action is not making progress. STOP repeating it and "
                    "do something different: navigate to another page, inspect "
                    "another element, or finish the branch and summarize your "
                    "findings."
                )
            }
    return None


root_agent = Agent(
    name="navigator",
    model=LLM_MODEL_DEEPSEEK,
    description="Deep human-grade QA tester: explores the live UI, thinks in branches, returns a DOCX report with screenshots.",
    instruction=NAVIGATOR_PROMPT + WEB_INSTRUCTION,
    tools=[
        take_screenshot,
        record_evidence,
        coverage_report,
        build_docx_report,
        list_specs,
        search_specs,
        read_spec,
        get_interactive_elements,
        native_click,
        native_type,
        get_page_text,
        get_current_url,
        check_page_invariants,
        visual_probe,
        site_login,
        write_guard.get_blocked_writes,
        McpToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(
                    command=os.environ.get(
                        "PLAYWRIGHT_MCP_CMD",
                        r"C:\Program Files\nodejs\npx.cmd" if os.name == "nt" else "npx",
                    ),
                    args=(
                        ["--cdp-endpoint", "http://127.0.0.1:9222", "--image-responses", "omit"]
                        if os.environ.get("PLAYWRIGHT_MCP_CMD")
                        else ["-y", "@playwright/mcp@0.0.79", "--cdp-endpoint", "http://127.0.0.1:9222", "--image-responses", "omit"]
                    ),
                    # Playwright MCP maintains a small server registry.  The
                    # managed runner's ~/.cache is read-only, so keep that
                    # registry in a writable temp location unless the caller
                    # explicitly provides a cache directory.
                    env={
                        **os.environ,
                        "XDG_CACHE_HOME": os.environ.get(
                            "XDG_CACHE_HOME", "/tmp/qa-mcp-cache"
                        ),
                        "PWTEST_SERVER_REGISTRY": os.environ.get(
                            "PWTEST_SERVER_REGISTRY", "/tmp/qa-mcp-cache/b"
                        ),
                    },
                ),
                timeout=150,
            ),
            tool_filter=[
                "browser_navigate",
                "browser_snapshot",
                "browser_click",
                "browser_type",
                "browser_network_requests",
                "browser_file_upload",
                "browser_handle_dialog",
                "browser_press_key",
                "browser_navigate_back",
                "browser_wait_for",
            ]
        )
    ],
    mode="chat",
    timeout=7200,
    sub_agents=[visual_auditor_agent],
    before_tool_callback=_stall_guard,
    after_tool_callback=cap_tool_response,
    before_model_callback=prune_llm_request,
)

app = build_compacting_app("navigator", root_agent, LLM_MODEL_DEEPSEEK)
