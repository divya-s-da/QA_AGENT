import os
import base64
import json
import re
import tempfile
from datetime import datetime


SCREENSHOT_DIR = os.environ.get(
    "QA_SCREENSHOT_DIR",
    os.path.join(os.path.expanduser("~"), ".react_qa_screenshots"),
)

# CLI runs force one folder per run (set_run_context); interactive web sessions
# key by the ADK session_id (set_web_session is set by the navigator's callback).
_RUN_OVERRIDE = None
_WEB_SESSION = None


def set_run_context(run_id):
    """Point all evidence at one per-run folder (used by the CLI pipeline)."""
    global _RUN_OVERRIDE
    _RUN_OVERRIDE = f"run_{run_id}" if run_id else None


def set_web_session(session_id):
    """Remember the interactive session the navigator is currently serving."""
    global _WEB_SESSION
    _WEB_SESSION = session_id


def evidence_dir(session_id=None):
    """Per-test evidence folder: run override > web session > explicit id > default."""
    if _RUN_OVERRIDE:
        return os.path.join(SCREENSHOT_DIR, _RUN_OVERRIDE)
    key = _WEB_SESSION or session_id or "default"
    key = re.sub(r"[^A-Za-z0-9._-]", "_", str(key))
    return os.path.join(SCREENSHOT_DIR, f"session_{key}")


def ensure_screenshot_dir(session_id=None):
    global SCREENSHOT_DIR
    d = evidence_dir(session_id)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        # Some managed runners expose the home directory read-only. Keep
        # screenshots usable (and reportable) by falling back to temp storage.
        fallback_root = os.path.join(tempfile.gettempdir(), "react_qa_screenshots")
        if SCREENSHOT_DIR == fallback_root:
            raise
        SCREENSHOT_DIR = fallback_root
        d = evidence_dir(session_id)
        os.makedirs(d, exist_ok=True)
    return d


def save_screenshot(screenshot_b64, screen_id, check_id, session_id=None):
    d = ensure_screenshot_dir(session_id)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{screen_id}_{check_id}_{timestamp}.png"
    filepath = os.path.join(d, filename)
    img_data = base64.b64decode(screenshot_b64)
    with open(filepath, "wb") as f:
        f.write(img_data)
    return filepath


def extract_and_save_screenshots(evidence, screen_id, session_id=None):
    text = json.dumps(evidence) if not isinstance(evidence, str) else evidence
    pattern = r"data:image/png;base64,([A-Za-z0-9+/=]+)"
    matches = re.findall(pattern, text)
    count = 0
    for idx, b64 in enumerate(matches):
        try:
            save_screenshot(b64, screen_id, f"screenshot_{idx}", session_id)
            count += 1
        except Exception:
            pass
    return count


def get_screenshot_dir(session_id=None):
    return ensure_screenshot_dir(session_id)
