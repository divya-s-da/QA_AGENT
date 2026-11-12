"""Deterministic QA Pipeline Runner.

Runs the full QA testing pipeline WITHOUT an LLM orchestrator.
Each step is executed in sequence with Python control flow — no prompt following,
no loop detection needed, no state machine drift.

Usage:
    python -m react_qa_agent.run_pipeline --repo-url <url> --site-url <url> [--test-file <path>]
"""

import argparse
import asyncio
import json
import sys
import os
import shutil
import time
from datetime import datetime

from google.adk.workflow import Workflow, FunctionNode, START
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.artifacts import InMemoryArtifactService
from google.genai import types

from .config import LLM_MODEL, TEST_FILE_PATH, MAX_CHECKS_PER_SCREEN, HEARTBEAT_INTERVAL_SECONDS, MAX_TOOL_CALLS_PER_RUN
from .prompts.mapper import MAPPER_PROMPT
from .prompts.contract import CONTRACT_PROMPT
from .prompts.navigator import NAVIGATOR_PROMPT
from .prompts.report import REPORT_PROMPT
from .tools.repo_analyzer import clone_repo, grep_routes, list_src
from .tools.route_parser import parse_route_config
from .tools.component_reader import read_components
from .tools.yaml_tools import read_yaml_standards, get_rules_as_json
from .tools.docx_report import generate_docx_report
from .tools.screenshot_tool import SCREENSHOT_DIR, extract_and_save_screenshots, set_run_context, evidence_dir
from .tools.browser_server import start_browser, stop_browser, CDP_URL
from .tools.context_guard import cap_tool_response, prune_llm_request
from .tools.mcp_runtime import configure_mcp_runtime

configure_mcp_runtime()

MAX_SCREENS = 10


def build_mapper_agent():
    from google.adk.agents import Agent
    return Agent(
        name="mapper", model=LLM_MODEL, instruction=MAPPER_PROMPT,
        tools=[clone_repo, grep_routes, list_src, parse_route_config, read_components],
        output_key="screen_map", mode="chat", timeout=120,
    )

def build_contract_agent():
    from google.adk.agents import Agent
    return Agent(
        name="contract_generator", model=LLM_MODEL, instruction=CONTRACT_PROMPT,
        tools=[read_yaml_standards, get_rules_as_json],
        output_key="contracts", mode="chat", timeout=180,
    )

def build_navigator_agent():
    from google.adk.agents import Agent
    from google.adk.tools.mcp_tool import McpToolset
    from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
    from mcp import StdioServerParameters
    from .tools.playwright_screenshot import take_screenshot
    from .tools.evidence_tools import record_evidence, coverage_report
    from .tools.native_browser import (
        get_interactive_elements, native_click, native_type,
        get_page_text, get_current_url,
    )
    from .tools.invariants import check_page_invariants
    from .tools.model_router import build_lite_llm
    return Agent(
        name="navigator", model=build_lite_llm("executor"), instruction=NAVIGATOR_PROMPT,
        tools=[
            take_screenshot,
            record_evidence,
            coverage_report,
            get_interactive_elements,
            native_click,
            native_type,
            get_page_text,
            get_current_url,
            check_page_invariants,
            McpToolset(
                connection_params=StdioConnectionParams(
                    server_params=StdioServerParameters(
                        command=os.environ.get("PLAYWRIGHT_MCP_CMD", "npx"),
                        args=(
                            ["--cdp-endpoint", CDP_URL, "--image-responses", "omit"]
                            if os.environ.get("PLAYWRIGHT_MCP_CMD")
                            else ["-y", "@playwright/mcp@0.0.79",
                                  "--cdp-endpoint", CDP_URL,
                                  "--image-responses", "omit"]
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
                    timeout=90,
                ),
                tool_filter=[
                    "browser_navigate", "browser_snapshot", "browser_click",
                    "browser_type",
                    "browser_network_requests",
                    "browser_file_upload",
                    "browser_handle_dialog", "browser_press_key",
                    "browser_navigate_back", "browser_wait_for",
                ],
            )
        ],
        # NOTE: no output_key here — output_key would overwrite the state key
        # "test_evidence" with the agent's final text response, clobbering the
        # evidence list that record_evidence() persists during the run.
        mode="chat", timeout=7200,
        after_tool_callback=cap_tool_response,
        before_model_callback=prune_llm_request,
    )

def build_report_agent():
    from google.adk.agents import Agent
    from .tools.model_router import build_lite_llm
    # Narrative-only: the pipeline renders the per-check lines and the DOCX
    # itself. No tools here — a tool call would force the model to echo the
    # full results JSON back, which the gateway truncates mid-string.
    return Agent(
        name="report_generator", model=build_lite_llm("reporter"), instruction=REPORT_PROMPT,
        tools=[], output_key="final_report", mode="chat", timeout=600,
    )


def build_verifier_agent():
    from google.adk.agents import Agent
    from .prompts.verifier import VERIFIER_PROMPT
    from .tools.model_router import build_lite_llm
    return Agent(
        name="verifier", model=build_lite_llm("verifier"), instruction=VERIFIER_PROMPT,
        tools=[], mode="chat", timeout=120,
    )


async def verify_evidence(runner, session_service, user_id, screen_id, verdicts):
    """Screen-level verifier pass (--verify): judges each check from
    evidence + expectation only, never from the navigator's recorded verdict.

    Returns (verified_verdicts, rejected) where rejected lists checks the
    verifier could not verify because no traceable expectation exists.
    """
    items = []
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        items.append({
            "check_id": v.get("check_id", "?"),
            "expected": v.get("expected", ""),
            "expected_source": v.get("expected_source", ""),
            "evidence_note": (v.get("reason") or "")[:1500],
            "screenshot_path": v.get("screenshot_path", ""),
        })
    if not items:
        return [], []
    session = await session_service.create_session(
        app_name="qa_pipeline", user_id=user_id,
        state={"phase": "verify", "current_screen_index": screen_id},
    )
    sid = session.id
    msg = (
        "You are verifying N checks. For EACH check output EXACTLY ONE line in this format:\n"
        "CHECK <index>: VERDICT: <PASS|FAIL|BLOCKED> | CONFIDENCE: <0.0-1.0> | REASON: <one sentence>\n"
        "Nothing else — no preamble, no commentary, no markdown. The checks, in order:\n\n"
        + json.dumps(items, indent=1, default=str)
    )
    events = await run_agent(runner, sid, user_id, msg, "verifier", timeout_seconds=600)
    text = extract_text(events)
    verified = []
    rejected = []
    for v, raw in zip(items, text.splitlines()):
        if "VERDICT:" not in raw:
            continue
        verdict = "BLOCKED"
        if "VERDICT: PASS" in raw:
            verdict = "PASS"
        elif "VERDICT: FAIL" in raw:
            verdict = "FAIL"
        elif "VERDICT: BLOCKED" in raw:
            verdict = "BLOCKED"
        if not v["expected_source"]:
            rejected.append(v["check_id"])
        verified.append({
            "check_id": v["check_id"],
            "verdict": verdict,
            "reason": raw,
            "screen_id": str(screen_id),
            "screenshot_path": v["screenshot_path"],
            "expected": v["expected"],
            "expected_source": v["expected_source"],
        })
    return verified, rejected


def _maybe_print_live(agent_name, event, elapsed):
    """Print live progress as tool calls stream in, so the console never looks
    frozen during a long agent run (screen-level results only print at the end)."""
    if not event.content or not event.content.parts:
        return
    for part in event.content.parts:
        fc = getattr(part, "function_call", None)
        if fc is None:
            continue
        name = fc.name
        args = fc.args if isinstance(fc.args, dict) else {}
        if name == "record_evidence":
            print(f"  [LIVE] {agent_name} | record: {args.get('check_id', '?')} -> {args.get('status', '?')} (t={elapsed:.0f}s)")
        elif name == "take_screenshot":
            print(f"  [LIVE] {agent_name} | screenshot: {args.get('check_id', '?')} (t={elapsed:.0f}s)")


async def run_agent(runner, session_id, user_id, message, agent_name="agent", timeout_seconds=600):
    """Run a single agent with a timeout and return the events."""
    content = types.Content(
        role="user", parts=[types.Part.from_text(text=message)]
    )
    events = []
    start = time.monotonic()
    last_event_ts = {"ts": start}

    # Idle heartbeat: fire only when nothing has arrived for a while, so the
    # console confirms liveness during long silent stretches. A background task
    # avoids interrupting ADK's event generator with wait_for/cancellation.
    async def _idle_watcher():
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            if time.monotonic() - last_event_ts["ts"] > HEARTBEAT_INTERVAL_SECONDS:
                print(f"  [HEARTBEAT] {agent_name} still working "
                      f"(t={time.monotonic() - start:.0f}s, {len(events)} events, "
                      f"{count_tool_calls(events)} tool calls)...")

    watcher = asyncio.create_task(_idle_watcher())
    stall_state = {"last_pair": None, "repeat_count": 0, "tool_calls": 0}
    try:
        async def _run():
            async for event in runner.run_async(
                user_id=user_id, session_id=session_id, new_message=content
            ):
                events.append(event)
                last_event_ts["ts"] = time.monotonic()
                _maybe_print_live(agent_name, event, last_event_ts["ts"] - start)
                _check_stall(agent_name, event, stall_state)
        await asyncio.wait_for(_run(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        print(f"  [TIMEOUT] {agent_name} exceeded {timeout_seconds}s limit. Using partial results.")
    except StallError as se:
        print(f"  [STALL] {agent_name}: {se}")
    except Exception as e:
        err_str = str(e)
        if len(err_str) > 200:
            err_str = err_str[:200] + "..."
        print(f"  [ERROR] {agent_name} failed: {err_str}")
    finally:
        watcher.cancel()
    elapsed = time.monotonic() - start
    print(f"  [TIME] {agent_name} took {elapsed:.1f}s ({len(events)} events, {count_tool_calls(events)} tool calls)")
    return events


class StallError(Exception):
    """Raised when the agent is looping on the same (state, action) pair or
    exceeds the per-run tool-call cap (failure-inversion §5 stall controller)."""


def _check_stall(agent_name, event, stall_state):
    """Detect stalls: same tool+args pair 3x in a row, or tool-call cap.

    Progress = new states discovered, not repeated actions. Same pair 3x is a
    real loop — abort rather than burn budget.
    """
    if not event.content or not event.content.parts:
        return
    for part in event.content.parts:
        fc = getattr(part, "function_call", None)
        if fc is None:
            continue
        name = fc.name
        args = fc.args if isinstance(fc.args, dict) else {}
        if name == "record_evidence" or name == "take_screenshot":
            continue  # evidence calls are progress, not loop actions
        stall_state["tool_calls"] += 1
        if stall_state["tool_calls"] > MAX_TOOL_CALLS_PER_RUN:
            raise StallError(
                f"exceeded {MAX_TOOL_CALLS_PER_RUN} tool calls — budget cap hit, using partial results"
            )
        pair = (name, str(sorted(args.items()))[:80])
        if pair == stall_state["last_pair"]:
            stall_state["repeat_count"] += 1
        else:
            stall_state["last_pair"] = pair
            stall_state["repeat_count"] = 1
        if stall_state["repeat_count"] >= 3:
            raise StallError(
                f"same action repeated 3x in a row ({name} {str(args)[:60]}) — loop detected, using partial results"
            )


def count_tool_calls(events):
    """Count function/tool calls made by the agent across all events."""
    n = 0
    for event in events:
        if not event.content or not event.content.parts:
            continue
        for part in event.content.parts:
            if getattr(part, "function_call", None) is not None:
                n += 1
            elif getattr(part, "tool_call", None) is not None:
                n += 1
    return n


def extract_text(events):
    """Extract text content from events."""
    texts = []
    for event in events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    texts.append(part.text)
    return "\n".join(texts)


def extract_json_from_events(events):
    """Try to parse a JSON value from the agent's event text output."""
    text = extract_text(events).strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def normalize_json_output(value):
    """Convert a state/event value that may be a JSON string into a Python object."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    return value


def get_state_value(session, key):
    """Get a value from session state."""
    if session.state and key in session.state:
        return session.state[key]
    return None


async def get_fresh_session_state(session_service, session_id, user_id):
    """Re-fetch a session from the service and return its state dict.

    InMemorySessionService returns a COPY of the session; the runner updates
    its internal storage copy, so a previously-created session object holds
    stale state. Always re-fetch after running an agent.
    """
    fetched = await session_service.get_session(
        app_name="qa_pipeline", user_id=user_id, session_id=session_id
    )
    if fetched is None:
        return {}
    return fetched.state or {}


def screenshots_as_evidence(screen_id):
    """Build evidence items from screenshot files taken for a screen.

    The navigator takes screenshots named '{screen_id}_{check_id}_{timestamp}.png'
    for every check. If it fails to record evidence in state, the screenshots
    themselves prove which checks were exercised — the judge can still evaluate
    the checks, and the screenshots get included in the DOCX report.
    """
    if not os.path.isdir(evidence_dir()):
        return []
    evidence = []
    prefix = f"{screen_id}_"
    for name in sorted(os.listdir(evidence_dir())):
        if not name.startswith(prefix) or not name.endswith(".png"):
            continue
        stem = name[len(prefix):-4]
        check_id = stem.rsplit("_", 1)[0]
        evidence.append({
            "check_id": check_id,
            "status": "BLOCKED",
            "note": "Navigator did not record a verdict, but a screenshot was captured for this check.",
            "screenshot_path": os.path.join(evidence_dir(), name),
        })
    return evidence


def evidence_to_verdicts(evidence, screen_id):
    """Convert navigator-recorded evidence into the verdict shape used by the report.

    The judge agent was dropped: the navigator's own record_evidence() calls
    (PASS/FAIL/BLOCKED per check) are now the final verdicts, so no second
    LLM pass is needed.
    """
    verdicts = []
    if not isinstance(evidence, list):
        return verdicts
    for item in evidence:
        if not isinstance(item, dict):
            continue
        verdicts.append({
            "check_id": item.get("check_id", ""),
            "verdict": item.get("status", "BLOCKED"),
            "reason": item.get("note", ""),
            "screen_id": screen_id,
            "screenshot_path": item.get("screenshot_path", ""),
            "expected": item.get("expected", ""),
            "expected_source": item.get("expected_source", ""),
            "url": item.get("url", ""),
        })
    return verdicts


def render_per_check_lines(all_screen_results):
    """Deterministically render one line per check — the AI-readable report body.

    The full per-check detail already exists in all_screen_results (verdict +
    reason + screenshot path). Rendering it here never depends on the LLM, so
    the TXT report is always complete even if the report model misbehaves.
    """
    total_checks = 0
    passed = failed = blocked = 0
    lines = []
    for r in all_screen_results:
        screen_id = r.get("screen_id", "?")
        route = r.get("route", "/")
        for v in r.get("verdicts", []):
            if not isinstance(v, dict):
                continue
            verdict = v.get("verdict", "BLOCKED")
            detail = (v.get("reason") or "").strip().replace("\n", " ")
            screenshot = v.get("screenshot_path", "")
            expected = (v.get("expected") or "").strip().replace("\n", " ")
            expected_source = (v.get("expected_source") or "").strip()
            line = (
                f"screen: {screen_id} | route: {route} | check: {v.get('check_id', '?')} "
                f"| verdict: {verdict} | detail: {detail or 'no detail recorded'} "
                f"| screenshot: {screenshot}"
            )
            if expected:
                line += f" | expected: {expected}"
            if expected_source:
                line += f" | expected_source: {expected_source}"
            lines.append(line)
            total_checks += 1
            if verdict == "PASS":
                passed += 1
            elif verdict == "FAIL":
                failed += 1
            else:
                blocked += 1

    return lines, total_checks, passed, failed, blocked


async def check_llm_health() -> bool:
    """Probe the model once up front so a dead/quota-exhausted key aborts before the run."""
    try:
        import litellm
        import os as _os
        # Probe with the exact settings the agents use (provider, key, base
        # URL, headers), so the check cannot pass or fail for a different
        # backend than the real run.
        kwargs = {k: v for k, v in (getattr(LLM_MODEL, "_additional_args", None) or {}).items()
                  if k in ("api_base", "api_key", "extra_headers")}
        kwargs.update(model=LLM_MODEL.model,
                      messages=[{"role": "user", "content": "Reply with exactly: OK"}], max_tokens=5)
        resp = await litellm.acompletion(**kwargs)
        content = (resp.choices[0].message.content or "").strip()
        print(f"  [LLM OK] model responded: {content[:40]}")
        return True
    except Exception as e:
        print("  [LLM FAIL] The language model could not be reached BEFORE testing started.")
        print(f"  Reason: {type(e).__name__}: {str(e)[:300]}")
        print(f"  Model: {getattr(LLM_MODEL, 'model', '?')}. Fix: check the LLM key names in .env "
              "(see config.py) and that the provider is reachable.")
        print("  The run was aborted — no point testing with a broken model.")
        return False


def _write_partial_report(site_url, all_screen_results):
    """Write an incremental single-line report of completed screens so far."""
    per_check_lines, total_checks, passed, failed, blocked = render_per_check_lines(all_screen_results)
    lines = [
        f"PARTIAL REPORT (in progress) — site: {site_url}",
        f"screens completed: {len(all_screen_results)}",
        f"checks: {total_checks} | PASS={passed} FAIL={failed} BLOCKED={blocked}",
        "",
    ] + per_check_lines
    partial_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "qa_partial_report.txt",
    )
    with open(partial_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return partial_path


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                if s is not sys.__stdout__ and s is not sys.__stderr__:
                    s.flush()
            except Exception:
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass


async def pipeline_main(repo_url, site_url, test_file_path=None, max_screens=MAX_SCREENS,
                        charter_path=None, verify_mode=False, flake_gate=False,
                        resume=False, audit=False, map_site=False, results_json=None):
    """Run the full QA pipeline deterministically."""
    # Windows consoles default to a legacy codec that crashes on emoji/unicode.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # Mirror every console line to a timestamped log file, so if the process
    # is killed mid-run we can see exactly where it stopped.
    try:
        _log_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            f"qa_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
        )
        _log_file = open(_log_path, "w", encoding="utf-8")
        sys.stdout = _Tee(sys.stdout, _log_file)
        sys.stderr = _Tee(sys.stderr, _log_file)
        print(f"Log file: {_log_path}")
    except Exception:
        pass
    session_service = InMemorySessionService()
    artifact_service = InMemoryArtifactService()
    user_id = "pipeline_user"

    print("=" * 60)
    print("QA PIPELINE - Deterministic Execution")
    print("=" * 60)
    print(f"Repo: {repo_url or 'none (black-box, site URL only)'}")
    print(f"Site: {site_url}")
    print(f"Test file: {test_file_path or 'None'}")
    print(f"Max screens: {max_screens}")
    print(f"Max checks per screen: {MAX_CHECKS_PER_SCREEN}")
    print()
    pipeline_start = time.monotonic()

    # Charter gate (failure-inversion §1): the charter is the ONLY legitimate
    # source of expected behaviour. Missing required fields fail closed.
    charter_context = ""
    if not charter_path:
        print("  [CHARTER FAIL] --charter is required: it decides whether this run may write data.")
        print("  Aborting — fail closed, no assumptions.")
        return
    if charter_path:
        try:
            from .tools.charter import load_charter, validate_charter, charter_to_context
            charter = load_charter(charter_path)
            missing = validate_charter(charter)
            if missing:
                print(f"  [CHARTER FAIL] Missing required fields: {', '.join(missing)}")
                print("  Aborting — fail closed, no assumptions.")
                return
            charter_context = charter_to_context(charter)
            print("  [CHARTER] loaded and validated.")
            # Only an explicit `destructive_actions_allowed: true` turns writes
            # on; absent, false or any other value keeps the run read-only.
            from .tools import write_guard
            write_guard.configure(
                allow_writes=charter.get("destructive_actions_allowed") is True,
                allowlist=charter.get("write_allowlist") or [],
            )
            print(f"  [WRITE GUARD] mode: {write_guard.mode_label()}")
        except Exception as e:
            print(f"  [CHARTER FAIL] {e}")
            print("  Aborting — fail closed, no assumptions.")
            return
    else:
        print("  [CHARTER] none provided — heuristic expectations (WCAG/HTTP semantics) only.")

    # Persistent learning store (§13): corrections from past runs are loaded
    # into context so the agent stops repeating the same mistakes.
    from .tools.learning_store import get_learning_context
    learning_context = get_learning_context()
    if learning_context:
        print("  [LEARNING] loaded prior corrections/false-positives.")
        charter_context = (charter_context + "\n\n" + learning_context).strip()

    # Write-ahead journal + resumable cursor (§6).
    from .tools.state_store import journal_step, save_cursor, load_cursor, clear_run_state
    if resume:
        cursor = load_cursor()
        if cursor and cursor.get("site_url") == site_url:
            print(f"  [RESUME] found cursor: {cursor.get('screens_done', 0)} screens done. Resuming.")
        else:
            print("  [RESUME] no matching cursor — starting fresh.")
    else:
        clear_run_state()
    journal_step({"event": "pipeline_start", "site_url": site_url, "charter": charter_path})

    # Cartographer (§2): build the state graph before testing so movement is a
    # walk on a known map, and dead routes are discovered up front.
    state_graph = None
    if map_site:
        try:
            from .tools.cartographer import build_state_graph, save_state_graph
            print("  [CARTOGRAPHER] crawling site for state graph...")
            start_browser()
            state_graph = await build_state_graph(site_url, max_nodes=10, max_depth=2)
            save_state_graph(state_graph)
            print(f"  [CARTOGRAPHER] mapped {len(state_graph['nodes'])} states, "
                  f"{len(state_graph['edges'])} edges.")
            stop_browser()
        except Exception as e:
            print(f"  [CARTOGRAPHER] failed ({e}) — continuing without state graph.")

    # Fail fast: verify the LLM works before spending 25+ minutes on a run.
    if not await check_llm_health():
        return

    # Clear stale screenshots from previous runs (files AND per-test folders)
    if os.path.isdir(SCREENSHOT_DIR):
        shutil.rmtree(SCREENSHOT_DIR, ignore_errors=True)
    set_run_context(datetime.now().strftime("%Y%m%d_%H%M%S"))

    # Create session with initial state
    session = await session_service.create_session(
        app_name="qa_pipeline", user_id=user_id,
        state={
            "repo_url": repo_url,
            "site_url": site_url,
            "test_file_path": test_file_path or TEST_FILE_PATH or "",
            "phase": "setup",
            "current_screen_index": 0,
            "all_screen_results": [],
        }
    )
    session_id = session.id

    # ─── STEP 1: MAPPER (deterministic, no LLM) ─────────────────────────
    print("[1/4] MAPPER: Analyzing repository...")
    from .tools.deterministic_mapper import deterministic_mapper
    try:
        screen_map = deterministic_mapper(repo_url, site_url)
    except Exception as e:
        print(f"  [FAIL] Mapper failed: {e}")
        return

    if not screen_map or not screen_map.get("screens"):
        print("  [FAIL] Mapper did not find any screens.")
        return

    # Parse screen map
    if isinstance(screen_map, str):
        try:
            screen_map = json.loads(screen_map)
        except json.JSONDecodeError:
            print("  [FAIL] Mapper output is not valid JSON.")
            return

    screens = screen_map.get("screens", [])
    print(f"  Found {len(screens)} screens.")
    for s in screens[:5]:
        print(f"    - {s.get('screen_id', '?')}: {s.get('route_path', '?')} ({s.get('component_name', '?')})")
    if len(screens) > 5:
        print(f"    ... and {len(screens) - 5} more")

    # ─── STEP 2: CONTRACTS (deterministic, no LLM) ──────────────────────
    print("\n[2/4] CONTRACT GENERATOR: Creating test contracts...")
    from .tools.deterministic_contract import deterministic_contract_generator
    try:
        contracts = deterministic_contract_generator(screen_map)
    except Exception as e:
        print(f"  [FAIL] Contract generator failed: {e}")
        return

    if not contracts:
        print("  [FAIL] Contract generator did not produce contracts.")
        return

    num_contracts = len(contracts)
    total_checks = sum(len(c.get("checks", [])) for c in contracts)
    print(f"  Generated contracts for {num_contracts} screens ({total_checks} total checks).")

    # ─── STEP 3: NAVIGATE + JUDGE (per screen) ─────────────────────────
    all_screen_results = []
    num_to_test = min(num_contracts, max_screens)
    print(f"\n[3/4] NAVIGATOR + JUDGE: Testing {num_to_test} screens...")

    # ONE shared browser + ONE navigator instance reused for all screens.
    # The MCP browser session (and thus login state) persists across screens.
    start_browser()
    from .tools import write_guard
    guard_error = write_guard.ensure_active()
    if guard_error:
        print(f"  [WRITE GUARD] {guard_error}")
        stop_browser()
        return
    nav = build_navigator_agent()
    nav_runner = Runner(
        agent=nav, app_name="qa_pipeline",
        session_service=session_service, artifact_service=artifact_service,
    )
    verifier_runner = None
    if verify_mode:
        verifier = build_verifier_agent()
        verifier_runner = Runner(
            agent=verifier, app_name="qa_pipeline",
            session_service=session_service, artifact_service=artifact_service,
        )

    # Budget controller (§5): session-wide steps/tokens/wall-clock caps.
    from .tools.budget import BudgetController
    budget = BudgetController()

    try:
        # ─── LOGIN STEP (once, so login state persists for all screens) ──
        # Uses a fresh session so the shared MCP browser keeps its login state
        # without the conversation history accumulating context.
        print("  [LOGIN] Authenticating with the site...")
        login_session = await session_service.create_session(
            app_name="qa_pipeline", user_id=user_id,
            state={"site_url": site_url, "phase": "login"},
        )
        login_sid = login_session.id
        login_msg = (
            f"Log in to {site_url} with the site_login() tool, then verify you are logged in.\n\n"
            f"Steps:\n"
            f"1. Navigate to {site_url}\n"
            f"2. If a login form appears, call site_login(). Never type credentials yourself.\n"
            f"3. Wait for the page to load after login.\n"
            f"4. Verify login succeeded (you should no longer be on the login page).\n"
            f"5. Take a screenshot to record the logged-in state.\n"
            f"6. Call record_evidence(check_id='login_verification', status='PASS' if logged in else 'FAIL', "
            f"note='describe what you see', screenshot_path='<the screenshot path>')."
        )
        login_events = await run_agent(nav_runner, login_sid, user_id, login_msg,
                                       "navigator_login", timeout_seconds=420)
        login_state = await get_fresh_session_state(session_service, login_sid, user_id)
        login_evidence = login_state.get("test_evidence")
        if not login_evidence:
            login_evidence = extract_json_from_events(login_events)
        if login_evidence:
            print("  [LOGIN] Login step completed.")
        else:
            print("  [LOGIN] Login step produced no confirmation — continuing anyway.")

        for i in range(num_to_test):
            contract_entry = contracts[i] if i < len(contracts) else None
            if not contract_entry:
                continue

            screen_id = contract_entry.get("screen_id", f"screen_{i}")
            route = contract_entry.get("route", "/")
            all_checks = contract_entry.get("checks", [])
            # Trim to the most severe checks (contracts are pre-sorted critical-first)
            checks = all_checks[:MAX_CHECKS_PER_SCREEN]
            print(f"\n  --- Screen {i}: {screen_id} ({route}) ---")
            print(f"  Checks: {len(checks)} of {len(all_checks)} (trimmed to {MAX_CHECKS_PER_SCREEN})")

            # Fresh session per screen — only the current screen's contract is
            # passed so context stays small and never exceeds the model limit.
            screen_session = await session_service.create_session(
                app_name="qa_pipeline", user_id=user_id,
                state={
                    "repo_url": repo_url, "site_url": site_url,
                    "phase": "test_screen", "current_screen_index": i,
                },
            )
            screen_sid = screen_session.id
            current_contract = [{**contract_entry, "checks": checks}]

            nav_msg = (
                f"Test screen {i} at {site_url}{route}\n"
                f"Contracts: {json.dumps(current_contract)}\n"
                f"Current screen index: 0\n"
                f"You are ALREADY logged in to the site as the test account — the browser "
                f"session is shared and authenticated. Do NOT log in again unless the "
                f"page clearly shows a login form; if it does, call site_login() once.\n"
                f"Write mode: {write_guard.mode_label()}.\n"
                f"Test file path: {test_file_path or ''}\n"
                f"WHEN RECORDING EVIDENCE: pass expected=the contract check's expected_result "
                f"and expected_source='contract:<the check's standard_file>' (or 'charter'/'wcag'/'http-semantics' "
                f"for heuristic checks). The expectation must be recorded BEFORE you observe the result — "
                f"never invent the expectation after seeing what happened."
            )
            if charter_context:
                nav_msg += "\n\n" + charter_context
            if state_graph:
                nav_msg += (
                    "\n\nSTATE GRAPH (the site's known topology — walk it, do not jump):\n"
                    + json.dumps(state_graph, default=str)[:3000]
                )
            if not budget.within_limits(f"screens"):
                print(f"  [BUDGET] session budget exhausted ({budget.limits_report()}) — stopping screens.")
                break
            events = await run_agent(nav_runner, screen_sid, user_id, nav_msg,
                                     f"navigator_screen_{i}", timeout_seconds=7200)
            screen_state = await get_fresh_session_state(session_service, screen_sid, user_id)
            test_evidence = screen_state.get("test_evidence")
            if not test_evidence:
                test_evidence = extract_json_from_events(events)
            test_evidence = normalize_json_output(test_evidence)
            if isinstance(test_evidence, dict):
                test_evidence = test_evidence.get("evidence") or test_evidence.get("test_evidence") or []
            if not test_evidence:
                test_evidence = screenshots_as_evidence(screen_id)

            # Verdicts come straight from the navigator's recorded evidence —
            # no judge agent, no second LLM pass.
            if not test_evidence:
                print(f"  [WARN] Navigator produced no evidence for {screen_id}.")
                print("  [SKIP] No evidence — marking all checks BLOCKED.")
                verdicts = [{
                    "check_id": c.get("check_id", ""), "verdict": "BLOCKED",
                    "reason": "No evidence recorded by navigator", "screen_id": screen_id,
                } for c in checks]
            else:
                verdicts = evidence_to_verdicts(test_evidence, screen_id)
                if not verdicts:
                    print(f"  [WARN] No usable evidence entries for {screen_id} — marking all checks BLOCKED.")
                    verdicts = [{
                        "check_id": c.get("check_id", ""), "verdict": "BLOCKED",
                        "reason": "Evidence entries were not usable", "screen_id": screen_id,
                    } for c in checks]

            # Missing-check synthesis: the navigator may stop early and skip
            # some contract checks. Make them explicit BLOCKED verdicts so the
            # report always covers every planned check (Hercules completion protocol).
            recorded = {v.get("check_id") for v in verdicts if isinstance(v, dict)}
            missing = [c for c in checks if c.get("check_id") not in recorded]
            if missing:
                print(f"  [WARN] Navigator skipped {len(missing)} check(s): "
                      f"{', '.join(c.get('check_id') for c in missing[:5])}")
                for c in missing:
                    verdicts.append({
                        "check_id": c.get("check_id", ""), "verdict": "BLOCKED",
                        "reason": "Navigator did not record this check (skipped before completion)",
                        "screen_id": screen_id,
                        "expected": c.get("expected_result", ""),
                        "expected_source": f"contract:{c.get('standard_file', 'unknown')}",
                    })

            # Verifier split (--verify): an independent agent judges each check
            # from evidence + pre-declared expectation only, never seeing the
            # navigator's recorded verdict. Kills hallucinated passes/fails.
            if verify_mode and verdicts:
                verified, rejected = await verify_evidence(
                    verifier_runner, session_service, user_id, i, verdicts
                )
                if verified:
                    verdicts = verified
                if rejected:
                    print(f"  [WARN] {len(rejected)} check(s) have no traceable "
                          f"expectation — filed as observations, not verdicts: {rejected}")

            passed = sum(1 for v in verdicts if isinstance(v, dict) and v.get("verdict") == "PASS")
            failed = sum(1 for v in verdicts if isinstance(v, dict) and v.get("verdict") == "FAIL")
            blocked = sum(1 for v in verdicts if isinstance(v, dict) and v.get("verdict") == "BLOCKED")
            if not verdicts:
                overall = "BLOCKED"
            elif failed > 0:
                overall = "FAIL"
            elif passed == 0:
                overall = "BLOCKED"
            elif blocked > 0:
                overall = "PASS_WITH_CAVEATS"
            else:
                overall = "PASS"
            print(f"  Verdicts: PASS={passed}, FAIL={failed}, BLOCKED={blocked}, Overall={overall}")

            all_screen_results.append({
                "screen_id": screen_id,
                "route": route,
                "verdicts": verdicts,
                "overall": overall,
            })

            # Incremental checkpoint: write results so far to disk every screen.
            # If the process is killed mid-run, the partial report survives and
            # shows exactly which screens finished and which never started.
            try:
                _write_partial_report(site_url, all_screen_results)
                from .tools.state_store import save_cursor, journal_step
                save_cursor({
                    "site_url": site_url, "screens_done": len(all_screen_results),
                    "last_screen": screen_id, "updated": datetime.now().isoformat(),
                })
                journal_step({"event": "screen_done", "screen_id": screen_id,
                              "verdicts": len(verdicts), "overall": overall})
            except Exception as _e:
                print(f"  [WARN] checkpoint save failed: {_e}")

            await asyncio.sleep(3)

        # ─── FLAKE GATE (--flake-gate) ──────────────────────────────────
        # Every candidate FAIL is re-run up to 3x in fresh contexts.
        # 3/3 = confirmed bug. 1-2/3 = FLAKY. 0/3 = false positive, discarded.
        if flake_gate:
            print("\n  [FLAKE GATE] Re-running candidate FAILs in fresh contexts...")
            candidate_fails = []
            for r in all_screen_results:
                for v in r.get("verdicts", []):
                    if isinstance(v, dict) and v.get("verdict") == "FAIL":
                        candidate_fails.append((r["screen_id"], r["route"], v))
            for screen_id, route, v in candidate_fails[:3]:
                check_id = v.get("check_id", "?")
                confirmations = 0
                for attempt in range(3):
                    fg_session = await session_service.create_session(
                        app_name="qa_pipeline", user_id=user_id,
                        state={"phase": "flake_gate", "check_id": check_id},
                    )
                    fg_sid = fg_session.id
                    fg_msg = (
                        f"Re-verify ONLY this one check, in isolation:\n"
                        f"check_id: {check_id}\n"
                        f"route: {site_url}{route}\n"
                        f"expected: {v.get('expected', '')}\n"
                        f"expected_source: {v.get('expected_source', '')}\n"
                        f"Original observation: {(v.get('reason') or '')[:800]}\n"
                        f"Navigate to the route, reproduce the exact action, and record evidence "
                        f"with check_id '{check_id}' (PASS if it works, FAIL if it breaks). "
                        f"Use expected/expected_source from above."
                    )
                    await run_agent(nav_runner, fg_sid, user_id, fg_msg,
                                    f"flake_gate_{check_id}_{attempt}", timeout_seconds=600)
                    fg_state = await get_fresh_session_state(session_service, fg_sid, user_id)
                    fg_evidence = fg_state.get("test_evidence")
                    fg_evidence = normalize_json_output(fg_evidence) if fg_evidence else None
                    if isinstance(fg_evidence, dict):
                        fg_evidence = fg_evidence.get("evidence") or []
                    if isinstance(fg_evidence, list) and any(
                        isinstance(e, dict) and e.get("status") == "FAIL" for e in fg_evidence
                    ):
                        confirmations += 1
                if confirmations >= 3:
                    print(f"  [FLAKE] {check_id}: confirmed 3/3 (real bug)")
                elif confirmations >= 1:
                    print(f"  [FLAKE] {check_id}: FLAKY {confirmations}/3 — quarantined as flaky")
                    v["verdict"] = "FLAKY"
                    v["reason"] = f"{v.get('reason', '')} | flake_gate: {confirmations}/3 reproductions"
                else:
                    print(f"  [FLAKE] {check_id}: 0/3 — false positive, discarded")
                    v["verdict"] = "DISCARDED"
                    v["reason"] = f"{v.get('reason', '')} | flake_gate: 0/3 reproductions — false positive"
                await asyncio.sleep(3)

        # ─── REGISTRATION TEST (optional) ───────────────────────────────
        # Runs AFTER the screen loop: the shared browser must stay logged in
        # as the main account for all screens. Signup flows typically log the
        # browser into the NEW account — if run mid-pipeline, every screen
        # after it would land on the login page. At the end, it's harmless.
        reg_evidence = None
        if not write_guard.writes_allowed():
            print("  [REGISTER] skipped — read-only run (account creation is a write).")
        else:
            print("  [REGISTER] Testing account creation...")
            reg_session = await session_service.create_session(
                app_name="qa_pipeline", user_id=user_id,
                state={"site_url": site_url, "phase": "register_test"},
            )
            reg_sid = reg_session.id
            import time as _time
            # example.com is reserved (RFC 2606): mail to it is never delivered,
            # so a signup flow cannot email a real third party.
            _domain = os.getenv("QA_SIGNUP_EMAIL_DOMAIN", "example.com")
            test_email = f"qatester{int(_time.time())}@{_domain}"
            # A throwaway password for the new account only - never the real one.
            import secrets as _secrets
            test_password = _secrets.token_urlsafe(12) + "aA1!"
            reg_msg = (
                f"Test account registration on {site_url}.\n"
                f"Use this brand-new test email: {test_email}\n"
                f"Use this password for the new account: {test_password}\n\n"
                f"Steps:\n"
                f"1. Navigate to {site_url}.\n"
                f"2. Look for a 'Sign up' / 'Register' / 'Create account' link or button on the page "
                f"(try routes like /register, /signup, /sign-up if needed).\n"
                f"3. If a registration form is found: fill in the test email and password (and any "
                f"required name/role fields with sensible test values), then submit.\n"
                f"4. Verify the result: an account created / success message / redirected to a logged-in page.\n"
                f"5. Take a screenshot of the result.\n"
                f"6. Call record_evidence(check_id='account_registration', "
                f"status='PASS' if registration succeeded else 'FAIL', "
                f"note='describe what happened (form found? success message? error?)', "
                f"screenshot_path='<the screenshot path>').\n"
                f"If no registration link or form exists, call record_evidence(check_id='account_registration', "
                f"status='BLOCKED', note='No registration/signup page found', screenshot_path='')."
            )
            reg_events = await run_agent(nav_runner, reg_sid, user_id, reg_msg,
                                         "navigator_register", timeout_seconds=300)
            reg_state = await get_fresh_session_state(session_service, reg_sid, user_id)
            reg_evidence = reg_state.get("test_evidence")
            if not reg_evidence:
                reg_evidence = extract_json_from_events(reg_events)
            if reg_evidence:
                for item in reg_evidence if isinstance(reg_evidence, list) else [reg_evidence]:
                    if isinstance(item, dict):
                        print(f"  [REGISTER] {item.get('check_id')}: {item.get('status')} — {item.get('note', '')[:80]}")
            else:
                print("  [REGISTER] Registration step produced no confirmation — continuing anyway.")

        # Login + registration results join the report so the fixing AI sees them
        for name, evidence in (("login", login_evidence), ("register", reg_evidence)):
            if not evidence:
                continue
            verdicts = evidence_to_verdicts(evidence, name)
            if not verdicts:
                continue
            overall = "PASS" if any(v["verdict"] == "PASS" for v in verdicts) else "BLOCKED"
            all_screen_results.append({
                "screen_id": name,
                "route": f"{site_url} ({name} check)",
                "verdicts": verdicts,
                "overall": overall,
            })

        blocked = write_guard.blocked_writes()
        print(f"  [WRITE GUARD] mode {write_guard.mode_label()}: {len(blocked)} write request(s) blocked.")
        for item in blocked[-10:]:
            print(f"    {item['time']} {item['method']} {item['url']}")
        journal_step({"event": "write_guard", "mode": write_guard.mode_label(),
                      "blocked": blocked})

    finally:
        stop_browser()

    # ─── STEP 4: REPORT ─────────────────────────────────────────────────
    print(f"\n[4/4] REPORT GENERATOR: Compiling results...")
    report = build_report_agent()
    report_runner = Runner(
        agent=report, app_name="qa_pipeline",
        session_service=session_service, artifact_service=artifact_service,
    )
    all_screen_results_json = json.dumps(all_screen_results, default=str)
    report_msg = (
        f"Write the QA report narrative for site {site_url}.\n"
        f"All screen results (per-check verdicts with observed details): {all_screen_results_json}\n\n"
        f"Your job is ONLY the narrative: a plain-English analysis of the BREAKAGES. "
        f"Do NOT list every check — the pipeline writes those lines itself. "
        f"Write exactly four sections, plain text, no tables, no markdown:\n"
        f"BLOCKERS:\n"
        f"- one line per BLOCKED check: what could not be tested and why.\n"
        f"TOP_BREAKAGES:\n"
        f"- one line per failed check: what was tried, exactly what broke (exact "
        f"error text, HTTP status, console error), the page/route it happened on, "
        f"and the layer classification from the detail (APP_CODE / FRAMEWORK_OR_MIDDLEWARE "
        f"/ HOSTING_OR_EDGE / UNKNOWN_LAYER).\n"
        f"OBSERVATIONS_NEEDING_CONFIRMATION:\n"
        f"- one line per finding with no traceable expectation (empty expected_source) "
        f"or a possible failed precondition — these are NOT bugs.\n"
        f"FIX_THIS_FIRST:\n"
        f"- one line per recommendation, most severe first, with a concrete fix suggestion. "
        f"Match the fix to the failure's layer: do NOT suggest app-code fixes for "
        f"framework/dev-server/hosting issues, and note when a finding is a dev-server "
        f"artifact that may not reproduce in production. Never suggest adding missing "
        f"pages/features unless the contract required them.\n"
        f"If there are zero failures, write: TOP_BREAKAGES: none found.\n\n"
        f"Do NOT call any tool. Your final response IS the report text — write it "
        f"directly as your answer. No JSON, no code fences, just the four sections."
    )
    report_session = await session_service.create_session(
        app_name="qa_pipeline", user_id=user_id,
        state={"repo_url": repo_url, "site_url": site_url, "phase": "report"},
    )
    report_sid = report_session.id
    events = await run_agent(report_runner, report_sid, user_id, report_msg, "report_generator")
    report_state = await get_fresh_session_state(session_service, report_sid, user_id)
    final_report = report_state.get("final_report")

    if not final_report:
        final_report = extract_text(events)

    # Deterministic per-check lines (never depends on the LLM)
    per_check_lines, total_checks, passed, failed, blocked = render_per_check_lines(all_screen_results)
    flaky = sum(1 for r in all_screen_results
                for v in r.get("verdicts", []) if isinstance(v, dict) and v.get("verdict") in ("FLAKY", "DISCARDED"))
    summary_block = [
        f"total_screens_tested: {len(all_screen_results)}",
        f"total_checks_run: {total_checks}",
        f"results: PASS={passed} FAIL={failed} BLOCKED={blocked} FLAKY_OR_DISCARDED={flaky}",
        f"coverage: {passed + failed}/{total_checks} checks produced a verdict "
        f"({round((passed + failed) / max(total_checks, 1) * 100)}%); the rest are UNTESTED or BLOCKED — "
        f"untested is not 'fine', it is a number.",
        "UNTESTED_AREAS: " + (
            "; ".join(
                f"{r.get('screen_id')}: {sum(1 for v in r.get('verdicts', []) if isinstance(v, dict) and v.get('verdict') == 'BLOCKED')} blocked"
                for r in all_screen_results
            ) or "none"
        ),
    ]
    narrative = final_report if isinstance(final_report, str) else json.dumps(final_report, default=str)
    ai_report_text = "\n".join(summary_block) + "\n\n" + "\n".join(per_check_lines) + "\n\n" + narrative.strip()

    # ─── DOCX REPORT ──────────────────────────────────────────
    # Narrative text in the DOCX; per-check verdicts are rendered as tables
    # and screenshot captions inside docx_report.py (deterministic).
    try:
        docx_path = generate_docx_report(
            report_text=narrative,
            all_screen_results_json=all_screen_results_json,
            site_url=site_url,
            screenshots_dir=evidence_dir(),
        )
        print(f"  DOCX Report saved: {docx_path}")
    except Exception as e:
        print(f"  [ERROR] DOCX generation failed: {e}")
        docx_path = None

    # ─── PLAIN-TEXT REPORT (for AI consumption) ──────────────
    # Deterministic per-check lines + LLM narrative — complete every run.
    txt_path = None
    try:
        txt_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            f"qa_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
        )
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(ai_report_text)
        print(f"  TXT Report saved: {txt_path}")
    except Exception as e:
        print(f"  [ERROR] TXT save failed: {e}")
        txt_path = None

    # ─── OUTPUT ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("FINAL REPORT")
    print("=" * 60)
    if isinstance(final_report, str):
        print(final_report[:2000])
        if len(final_report) > 2000:
            print(f"\n... (truncated, full report in {docx_path})")
    else:
        print(json.dumps(final_report, indent=2, default=str)[:2000])

    # ─── AUDITOR (§10) ───────────────────────────────────────────
    # Adversarial review by a DIFFERENT model family. Can reject findings
    # lacking artifacts, traceable expectations, or 3/3 reproduction.
    if audit:
        try:
            from .tools.auditor import run_audit
            print("  [AUDIT] adversarial review by different model family (qwen)...")
            audit_result = run_audit(
                report_text=ai_report_text[:12000],
                results_json=all_screen_results_json[:12000],
                learning_context=learning_context,
            )
            print(f"  [AUDIT] verdict: {audit_result.get('verdict')}")
            for note in audit_result.get("notes", [])[:5]:
                print(f"  [AUDIT] note: {note}")
            for rej in audit_result.get("rejected_findings", [])[:5]:
                print(f"  [AUDIT] rejected: {rej}")
            ai_report_text += "\n\nAUDIT_RESULT: " + json.dumps(audit_result, default=str)
        except Exception as e:
            print(f"  [AUDIT] failed: {e}")

    # Journal final entry
    try:
        from .tools.state_store import journal_step
        journal_step({"event": "pipeline_end", "screens": len(all_screen_results),
                      "passed": p, "failed": f, "blocked": b})
    except Exception:
        pass

    # Summary
    total = len(all_screen_results)
    p = sum(1 for r in all_screen_results if r["overall"] == "PASS")
    c = sum(1 for r in all_screen_results if r["overall"] == "PASS_WITH_CAVEATS")
    f = sum(1 for r in all_screen_results if r["overall"] == "FAIL")
    b = total - p - c - f
    total_elapsed = time.monotonic() - pipeline_start
    print(f"\n{'=' * 60}")
    print(f"SUMMARY: {total} screens | PASS: {p} | PASS_WITH_CAVEATS: {c} | FAIL: {f} | BLOCKED: {b}")
    print(f"Total pipeline time: {total_elapsed / 60:.1f} minutes")
    if docx_path:
        print(f"DOCX Report: {docx_path}")
    if txt_path:
        print(f"TXT Report (for AI): {txt_path}")
    # Machine-readable results: the benchmark scorer (and later JUnit and the
    # review queue) read this instead of parsing report text.
    if results_json:
        try:
            with open(results_json, "w", encoding="utf-8") as fh:
                json.dump({"site_url": site_url, "screens": all_screen_results,
                           "minutes": round(total_elapsed / 60, 1)}, fh, indent=2, default=str)
            print(f"Results JSON: {results_json}")
        except Exception as e:
            print(f"  [ERROR] results JSON save failed: {e}")
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(description="Run QA pipeline deterministically")
    parser.add_argument("--repo-url", default=None,
                        help="GitHub repository URL (omit for black-box testing with only --site-url)")
    parser.add_argument("--site-url", required=True, help="Deployed site URL")
    parser.add_argument("--test-file", default=None, help="File path for upload testing")
    parser.add_argument("--max-screens", type=int, default=MAX_SCREENS,
                        help=f"Maximum number of screens to test (default: {MAX_SCREENS})")
    parser.add_argument("--charter", required=True,
                        help="Path to test charter JSON/YAML (fail-closed intake contract). "
                             "The run is read-only unless it sets destructive_actions_allowed: true. "
                             "See charters/example.yaml")
    parser.add_argument("--verify", action="store_true",
                        help="Run independent verifier pass: judge evidence+expectation, never the navigator verdict")
    parser.add_argument("--flake-gate", action="store_true",
                        help="Re-run candidate FAILs 3x in fresh contexts; report reproducibility")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from the last run's journal cursor (if it matches this site)")
    parser.add_argument("--audit", action="store_true",
                        help="Run adversarial auditor (different model family) over the final report")
    parser.add_argument("--map-site", action="store_true",
                        help="Crawl the site first to build a state graph (cartographer)")
    parser.add_argument("--results-json", default=None,
                        help="Also write every per-check verdict to this JSON file")
    args = parser.parse_args()

    asyncio.run(pipeline_main(
        args.repo_url, args.site_url, args.test_file, args.max_screens,
        charter_path=args.charter, verify_mode=args.verify, flake_gate=args.flake_gate,
        resume=args.resume, audit=args.audit, map_site=args.map_site,
        results_json=args.results_json,
    ))


if __name__ == "__main__":
    main()
