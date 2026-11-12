"""Per-session login credentials, held in memory only.

A client cannot be asked to edit a .env file on a server. They must type the
test account into the product, the agent must use it, and the product must
forget it. This module is that store.

What it guarantees:

  memory only     nothing is written to disk, so a crash or a restart loses
                  the credentials instead of leaving them somewhere
  per session     one tester's account never reaches another tester's run
  time limited    an entry expires, so a forgotten tab does not keep a
                  password alive all day
  never readable  no function returns the password to a caller that only
                  asks "is a credential present". Only site_login() reads the
                  value, and it redacts the value from its own output.

The model never sees these values. They do not enter a prompt, a tool result,
an event or a report. The browser receives them, and nothing else.
"""

from __future__ import annotations

import threading
import time

DEFAULT_TTL_SECONDS = 2 * 60 * 60

_lock = threading.Lock()
_store: dict[str, dict] = {}


def _prune(now: float) -> None:
    """Drop expired entries. The caller must hold the lock."""
    for key in [k for k, v in _store.items() if v["expires"] <= now]:
        _store.pop(key, None)


def put(session_id: str, email: str, password: str,
        password_fallback: str = "", ttl: int = DEFAULT_TTL_SECONDS) -> bool:
    """Store one account for one session. Returns False when input is empty."""
    session_id = (session_id or "").strip()
    email = (email or "").strip()
    password = password or ""
    if not session_id or not email or not password:
        return False
    now = time.time()
    with _lock:
        _prune(now)
        _store[session_id] = {
            "email": email,
            "password": password,
            "fallback": password_fallback or "",
            "expires": now + max(60, int(ttl)),
        }
    return True


def get(session_id: str):
    """(email, password, fallback) for a live entry, else None.

    Only site_login() calls this.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return None
    now = time.time()
    with _lock:
        _prune(now)
        entry = _store.get(session_id)
        if not entry:
            return None
        return entry["email"], entry["password"], entry["fallback"]


def status(session_id: str) -> dict:
    """What the user interface may know: presence and time left, never a value."""
    session_id = (session_id or "").strip()
    now = time.time()
    with _lock:
        _prune(now)
        entry = _store.get(session_id)
        if not entry:
            return {"present": False, "email": "", "expiresIn": 0}
        email = entry["email"]
        # Show enough of the address to recognise the account, not all of it.
        name, _, domain = email.partition("@")
        masked = (name[:2] + "***@" + domain) if domain else (name[:2] + "***")
        return {"present": True, "email": masked,
                "expiresIn": int(entry["expires"] - now)}


def clear(session_id: str) -> bool:
    """Forget one session's account. Called on logout, delete and run end."""
    with _lock:
        return _store.pop((session_id or "").strip(), None) is not None


def clear_all() -> int:
    with _lock:
        n = len(_store)
        _store.clear()
        return n


def active_sessions() -> int:
    """How many sessions hold an account. For diagnostics; no values."""
    now = time.time()
    with _lock:
        _prune(now)
        return len(_store)
