"""Runtime setup for the Playwright MCP subprocess.

The web UI and the CLI can run in managed environments where the user's home
directory is readable but not writable.  Falling back to ``npx`` in that
environment makes MCP fail before it registers any browser tools because npm
tries to write its cache.  Prefer an already-installed Playwright MCP CLI and
keep all fallback caches in a writable temporary directory.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _cli_candidates() -> list[Path]:
    """Return installed Playwright MCP CLI candidates, newest first."""
    candidates: list[Path] = []

    configured = os.environ.get("PLAYWRIGHT_MCP_CMD", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())

    for name in ("playwright-mcp", "playwright_mcp"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))

    npm_npx_root = Path.home() / ".npm" / "_npx"
    if npm_npx_root.is_dir():
        candidates.extend(npm_npx_root.glob("*/node_modules/@playwright/mcp/cli.js"))

    for root in (
        Path("/usr/local/lib/node_modules"),
        Path("/usr/lib/node_modules"),
    ):
        candidates.append(root / "@playwright" / "mcp" / "cli.js")

    existing = {p.resolve(): p for p in candidates if p.is_file()}
    return sorted(existing.values(), key=lambda p: p.stat().st_mtime, reverse=True)


def configure_mcp_runtime(env: dict[str, str] | None = None) -> dict[str, str]:
    """Make MCP startup deterministic and return the effective environment."""
    target = os.environ if env is None else env
    cache = Path(target.get("QA_MCP_CACHE", "/tmp/qa-mcp-cache"))
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "npm").mkdir(parents=True, exist_ok=True)
    (cache / "b").mkdir(parents=True, exist_ok=True)

    target.setdefault("XDG_CACHE_HOME", str(cache))
    target.setdefault("NPM_CONFIG_CACHE", str(cache / "npm"))
    target.setdefault("PWTEST_SERVER_REGISTRY", str(cache / "b"))

    # An explicit command always wins. Otherwise use the installed CLI so npm
    # is not invoked at all. The agent's McpToolset already handles the npx
    # fallback for machines where no installation is available.
    if not target.get("PLAYWRIGHT_MCP_CMD"):
        candidates = _cli_candidates()
        if candidates:
            target["PLAYWRIGHT_MCP_CMD"] = str(candidates[0])

    return target

