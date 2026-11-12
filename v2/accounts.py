"""User accounts and sessions for the v2 job server (standard library only).

Why this exists: the server used one shared token, so every caller could read
every run. This adds real identity so each person sees only their own runs,
history, and cross-run memory.

- Passwords are stored as scrypt hashes with a per-user random salt; the plain
  password is never stored or logged.
- A login returns an opaque session token kept server-side (in memory) with an
  expiry. The token goes in the Authorization header, never in a URL.
- The users table is SQLite on disk, so accounts survive a restart. Sessions are
  in memory, so a restart logs everyone out (they log in again) - safe default.
"""

import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time

_SCRYPT = dict(n=16384, r=8, p=1, dklen=32)
_USERNAME = re.compile(r"^[a-zA-Z0-9_.-]{3,40}$")
SESSION_TTL = float(os.getenv("V2_SESSION_TTL", str(12 * 3600)))   # seconds


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)


class Accounts:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            " username TEXT PRIMARY KEY, salt BLOB NOT NULL, pw_hash BLOB NOT NULL, created REAL NOT NULL)")
        self._db.commit()
        self._sessions: dict[str, tuple[str, float]] = {}   # token -> (username, expires_at)

    # ---------- users ----------
    def create_user(self, username: str, password: str) -> tuple[bool, str]:
        username = (username or "").strip()
        if not _USERNAME.match(username):
            return False, "username must be 3-40 chars: letters, digits, _ . -"
        if len(password or "") < 8:
            return False, "password must be at least 8 characters"
        salt = secrets.token_bytes(16)
        with self._lock:
            if self._db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                return False, "username is taken"
            self._db.execute("INSERT INTO users (username, salt, pw_hash, created) VALUES (?, ?, ?, ?)",
                             (username, salt, _hash(password, salt), time.time()))
            self._db.commit()
        return True, "created"

    def verify(self, username: str, password: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT salt, pw_hash FROM users WHERE username = ?",
                                   ((username or "").strip(),)).fetchone()
        if not row:
            # compare against a dummy hash so a missing user takes the same time as a wrong password
            _hash(password or "", b"0" * 16)
            return False
        salt, pw_hash = row
        return secrets.compare_digest(_hash(password or "", salt), pw_hash)

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    # ---------- sessions ----------
    def login(self, username: str, password: str) -> str | None:
        if not self.verify(username, password):
            return None
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = ((username or "").strip(), time.time() + SESSION_TTL)
        return token

    def user_for(self, token: str) -> str | None:
        if not token:
            return None
        with self._lock:
            rec = self._sessions.get(token)
            if not rec:
                return None
            username, exp = rec
            if time.time() > exp:
                self._sessions.pop(token, None)
                return None
            return username

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def sweep(self) -> None:
        now = time.time()
        with self._lock:
            for t in [t for t, (_, exp) in self._sessions.items() if now > exp]:
                self._sessions.pop(t, None)
