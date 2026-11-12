"""Container entrypoint for the QA-tester agent.

Order of operations (mirrors launch_adk_web.py on Windows):
  1. Apply the idempotent SSE keepalive patch to the installed ADK api_server.
  2. Start the shared headless Chromium with CDP on 9222.
  3. exec the ADK web server (DevServer, nested agents) on 0.0.0.0:8000.

API keys are read from the container environment only — never from the image.
"""

import importlib.util
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO.parent) not in sys.path:
    sys.path.insert(0, str(REPO.parent))


def apply_keepalive_patch() -> None:
    spec = importlib.util.find_spec("google.adk.cli.api_server")
    if spec is None or spec.origin is None:
        print("WARNING: google.adk.cli.api_server not found; keepalive patch skipped", flush=True)
        return
    api_server_path = Path(spec.origin)
    src = api_server_path.read_text(encoding="utf-8")
    if "# Sisyphus patch (SSE keepalive)" in src:
        print("SSE keepalive patch already present", flush=True)
        return
    old = "from contextlib import asynccontextmanager"
    if old not in src:
        print("WARNING: api_server import line not found; keepalive patch NOT applied", flush=True)
        return
    body = REPO / "tools" / "adk_sse_keepalive_body.py"
    if not body.exists():
        print("WARNING: adk_sse_keepalive_body.py missing; keepalive patch NOT applied", flush=True)
        return
    patched = src.replace(
        old,
        "from contextlib import asynccontextmanager\nfrom contextlib import suppress",
    )
    pattern = re.compile(
        r"      # Convert the events to properly formatted SSE\n"
        r"      async def event_generator\(\):.*?(?=\n      # Returns a streaming response)",
        re.DOTALL,
    )
    if not pattern.search(patched):
        print("WARNING: api_server event_generator marker not found; keepalive patch NOT applied", flush=True)
        return
    patched = pattern.sub(lambda m: body.read_text(encoding="utf-8"), patched, count=1)
    api_server_path.write_text(patched, encoding="utf-8")
    print("applied SSE keepalive patch to api_server.py", flush=True)


def start_browser() -> None:
    if not os.environ.get("CHROME_PATH"):
        matches = sorted((Path.home() / ".cache" / "ms-playwright").glob("chromium-*/chrome-linux/chrome"))
        if matches:
            os.environ["CHROME_PATH"] = str(matches[-1])
    from react_qa_agent.tools.browser_server import start_browser as _start

    _start()
    print("browser up on 9222", flush=True)


def main() -> None:
    apply_keepalive_patch()
    start_browser()
    agents_dir = str(REPO / "adk_agents")
    argv = [
        sys.executable,
        "-m",
        "google.adk.cli",
        "web",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    origins = os.environ.get("ALLOW_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000")
    for origin in origins.split(","):
        origin = origin.strip()
        if origin:
            argv += ["--allow_origins", origin]
    argv.append(agents_dir)
    os.execv(sys.executable, argv)


if __name__ == "__main__":
    main()
