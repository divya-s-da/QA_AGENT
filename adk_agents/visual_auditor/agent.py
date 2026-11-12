"""Visual QA specialist agent (Qwen3.8).

Theme / dark-mode / visual-regression passes. Runs on its own model instance
(LLM_MODEL_QWEN) because it is tuned for visual judgement, while the navigator
keeps DeepSeek for exploration endurance. Exposed as its own ADK web agent AND
wired as a sub-agent of navigator for in-session delegation.
"""

import json
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from google.adk.agents import Agent
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

from react_qa_agent.config import LLM_MODEL_QWEN
from react_qa_agent.tools.mcp_runtime import configure_mcp_runtime
from react_qa_agent.tools.playwright_screenshot import take_screenshot
from react_qa_agent.tools.evidence_tools import record_evidence
from react_qa_agent.tools.report_builder import build_docx_report
from react_qa_agent.tools.native_browser import (
    get_interactive_elements, native_click, native_type,
    get_page_text, get_current_url,
)
from react_qa_agent.tools.screenshot_tool import set_web_session
from react_qa_agent.tools.context_guard import cap_tool_response, prune_llm_request
from react_qa_agent.tools.site_guard import lock_host, allowed_host, is_allowed, current_off_target
from react_qa_agent.tools import write_guard

VISUAL_INSTRUCTION = """
You are a VISUAL QA SPECIALIST. Your job is theme and appearance quality,
not functional logic (the navigator handles that).

SCOPE:
- Dark mode / light mode toggling and persistence
- Layout breakage, overlapping or clipped elements per theme
- Text contrast and readability (light text on light bg, dark on dark)
- Icons/images/logos that vanish or invert badly on dark backgrounds
- Hover/focus states and disabled-control appearance if quick to check

METHOD:
1. Find how the product switches themes (toggle, settings page, profile menu).
   If none exists after a genuine search, record one evidence row
   check_id 'theme_toggle' status BLOCKED note 'no theme switcher found' and stop.
2. For EVERY screen the user asked about (or every sidebar/nav section if told
   to audit all): capture screenshot in light mode, switch to dark, capture
   again, reload the page to verify persistence, capture a third time.
3. Record evidence per screen: check_id '<screen>_theme_light',
   '<screen>_theme_dark', '<screen>_theme_persistence'. PASS only when the
   screen is clean in that mode; FAIL describes exactly what broke and where.
4. REPORT HANDLING: If the USER started this session directly, end with
   build_docx_report(), tell them the saved path. If the NAVIGATOR delegated
   to you mid-run, do NOT build a report — finish recording your evidence
   rows and transfer back to the navigator so it merges everything into ONE
   combined report.

RULES:
- Test ONLY the site you were given; off-site navigation is blocked by tools.
- You share ONE browser with the rest of the system — leave it on the target
  site when you finish.
- Record evidence constantly; a visual claim without a screenshot is not a finding.
"""

_NON_BROWSER_TOOLS = {
    "record_evidence", "build_docx_report", "get_current_url",
    "browser_handle_dialog", "browser_wait_for", "browser_navigate_back",
}

_guard_lock = threading.Lock()


def _site_guard(tool, args, tool_context):
    """Bind screenshot sessions and enforce target-site-only navigation."""
    name = tool.name
    session = getattr(tool_context, "session", None)
    session_id = getattr(session, "id", None) or "default"
    set_web_session(session_id)

    gate = write_guard.tool_gate(name)
    if gate:
        return gate

    url = args.get("url") if isinstance(args, dict) else None
    if name == "browser_navigate":
        lock_host(session_id, url or "")
        if not is_allowed(session_id, url or ""):
            return {
                "error": (
                    f"BLOCKED: '{url}' is not the target site (allowed host: "
                    f"{allowed_host(session_id)}). Return to the target site."
                )
            }
    elif name not in _NON_BROWSER_TOOLS:
        off, current = current_off_target(session_id)
        if off:
            return {
                "error": (
                    f"BLOCKED: you are on an external page ({current}). Navigate "
                    f"back to the target site ({allowed_host(session_id)})."
                )
            }
    with _guard_lock:
        pass
    return None


visual_auditor_agent = Agent(
    name="visual_auditor",
    model=LLM_MODEL_QWEN,
    description="Visual QA specialist: dark/light theme audits, contrast, layout breakage, persistence — returns a DOCX report.",
    instruction=VISUAL_INSTRUCTION,
    tools=[
        take_screenshot,
        record_evidence,
        build_docx_report,
        get_interactive_elements,
        native_click,
        native_type,
        get_page_text,
        get_current_url,
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
                "browser_press_key",
                "browser_navigate_back",
                "browser_wait_for",
            ]
        )
    ],
    mode="chat",
    timeout=7200,
    before_tool_callback=_site_guard,
    after_tool_callback=cap_tool_response,
    before_model_callback=prune_llm_request,
)

root_agent = visual_auditor_agent

from react_qa_agent.utils.adk_app import build_compacting_app
app = build_compacting_app("visual_auditor", root_agent, LLM_MODEL_QWEN)
