import os

from google.adk.agents import Agent
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters
from ..prompts.navigator import NAVIGATOR_PROMPT
from ..config import LLM_MODEL
from ..tools.playwright_screenshot import take_screenshot
from ..tools.evidence_tools import record_evidence, coverage_report
from ..tools.native_browser import (
    get_interactive_elements, native_click, native_type,
    get_page_text, get_current_url,
)
from ..tools.screenshot_tool import set_web_session
from ..tools.site_guard import lock_host, allowed_host, is_allowed, current_off_target
from ..tools.mcp_runtime import configure_mcp_runtime
from ..tools import write_guard
from ..tools.site_login import site_login

configure_mcp_runtime()

_NON_BROWSER_TOOLS = {
    "record_evidence", "get_current_url", "browser_network_requests",
    "browser_handle_dialog", "browser_file_upload", "browser_wait_for",
    "browser_navigate_back",
}


def _site_guard(tool, args, tool_context):
    """BeforeToolCallback: keep the navigator on the target site only."""
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
                    f"{allowed_host(session_id)}). Test ONLY the site you were "
                    "given; navigate back to it."
                )
            }
    elif name not in _NON_BROWSER_TOOLS:
        off, current = current_off_target(session_id)
        if off:
            return {
                "error": (
                    f"BLOCKED: you are on an external page ({current}). Navigate "
                    f"back to the target site ({allowed_host(session_id)}) before "
                    "continuing."
                )
            }
    return None

navigator_agent = Agent(
    name="navigator",
    model=LLM_MODEL,
    description="Navigates the live site using Playwright MCP, executes test actions from contracts.",
    instruction=NAVIGATOR_PROMPT,
    tools=[
        take_screenshot,
        record_evidence,
        coverage_report,
        get_interactive_elements,
        native_click,
        native_type,
        get_page_text,
        get_current_url,
        site_login,
        write_guard.get_blocked_writes,
        McpToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(
                    command=os.environ.get("PLAYWRIGHT_MCP_CMD", "npx"),
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
                )
            ),
            tool_filter=[
                # Core navigation & interaction
                "browser_navigate",
                "browser_snapshot",
                "browser_click",
                "browser_type",
                "browser_network_requests",
                # Dialog/modal handling
                "browser_file_upload",
                "browser_handle_dialog",
                "browser_press_key",
                "browser_navigate_back",
                "browser_wait_for",
            ]
        )
    ],
    # NOTE: no output_key — it would overwrite state key "test_evidence" with
    # the agent's final text, clobbering the list record_evidence() persists.
    mode="chat",
    timeout=1800,
    before_tool_callback=_site_guard,
)
