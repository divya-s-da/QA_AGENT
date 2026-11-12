"""Cross-run memory for a site: tells NEW findings from ones seen before.

The single biggest daily-use win is to show a team only what is NEW since the
last run, not the same list every time. This module keeps a small JSON store
per site (keyed by host) and classifies each finding as new or recurring.

Fail-safe rule: when a match is not clear, the finding is treated as NEW. It is
far safer to show a bug the team has seen than to hide a real new one as
"recurring". So matching is conservative: same route, and a strong overlap of
salient words (at least two shared words and a Jaccard score over the
threshold).

The store updates ONCE per run (commit=True), from the final report only, so an
interim report does not age a finding into "recurring" before the run ends.
"""

import contextlib
import json
import os
import re
from datetime import date
from urllib.parse import urlparse

try:
    import fcntl
except ImportError:                 # not on Linux: degrade to no cross-process lock
    fcntl = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE_DIR = os.getenv("V2_MEMORY_DIR", os.path.join(ROOT, "state", "memory"))
THRESHOLD = 0.55          # Jaccard over this (and >=2 shared words) = the same finding
_MIN_SHARED = 2

_STOP = {
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "at", "for", "is", "are", "was",
    "were", "be", "been", "it", "its", "this", "that", "with", "as", "by", "from", "into", "page",
    "shows", "show", "does", "not", "no", "has", "have", "when", "then", "than", "after", "before",
    "button", "link", "field", "click", "clicked", "clicks", "user", "users",
}


def _route(page) -> str:
    p = urlparse(str(page or "")).path or str(page or "")
    p = p.strip().lower()
    if len(p) > 1:
        p = p.rstrip("/")
    return p or "/"


def _tokens(text) -> set:
    words = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split()
    return {w for w in words if len(w) >= 3 and not w.isdigit() and w not in _STOP}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def site_key(url: str) -> str:
    host = (urlparse(url).hostname or "").lower() or "site"
    return re.sub(r"[^a-z0-9.-]+", "_", host) or "site"


def _owner_key(owner) -> str:
    o = re.sub(r"[^a-z0-9]+", "_", str(owner or "shared").lower()).strip("_")
    return o or "shared"


class SiteMemory:
    def __init__(self, url: str, owner=None):
        # owner-scoped: each user's "new since last run" is relative to their OWN runs only.
        self.key = _owner_key(owner) + "__" + site_key(url)
        self.path = os.path.join(STORE_DIR, self.key + ".json")
        self.entries = self._load()

    def _load(self) -> list:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def save(self) -> None:
        try:
            os.makedirs(STORE_DIR, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.entries, f, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _best_match(self, route: str, toks: set, used: set):
        best, best_score = None, 0.0
        for i, e in enumerate(self.entries):
            if i in used or e.get("route") != route:
                continue
            etoks = set(e.get("tokens", []))
            shared = len(toks & etoks)
            score = _jaccard(toks, etoks)
            if shared >= _MIN_SHARED and score >= THRESHOLD and score > best_score:
                best, best_score = i, score
        return best

    @contextlib.contextmanager
    def _exclusive(self):
        """Hold an exclusive lock across load -> classify -> save, so two runs on
        the same (owner, site) do not overwrite each other. Degrades to no lock if
        file locking is not available."""
        fd = None
        try:
            os.makedirs(STORE_DIR, exist_ok=True)
            fd = open(self.path + ".lock", "w")
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            self.entries = self._load()          # reload fresh under the lock
            yield
        finally:
            if fd is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    fd.close()
                except Exception:
                    pass

    def classify(self, findings: list, commit: bool = True) -> dict:
        """Tag each finding dict in place with is_new / first_seen / times_seen.

        findings: dicts that have at least 'page' (route) and 'title'.
        Returns a small summary {'new': n, 'recurring': m}. Updates and saves the
        store only when commit is True (under an exclusive lock)."""
        if not commit:
            return self._classify(findings, commit=False)
        with self._exclusive():
            return self._classify(findings, commit=True)

    def _classify(self, findings: list, commit: bool) -> dict:
        today = date.today().isoformat()
        used: set = set()
        new_n = rec_n = 0
        for f in findings:
            route = _route(f.get("page"))
            toks = _tokens(f.get("title"))
            idx = self._best_match(route, toks, used)
            if idx is None:
                f["is_new"] = True
                f["first_seen"] = today
                f["times_seen"] = 1
                new_n += 1
                if commit:
                    self.entries.append({"route": route, "tokens": sorted(toks),
                                         "title": str(f.get("title", ""))[:200],
                                         "first_seen": today, "last_seen": today, "count": 1})
                    used.add(len(self.entries) - 1)
            else:
                e = self.entries[idx]
                f["is_new"] = False
                f["first_seen"] = e.get("first_seen", today)
                f["times_seen"] = int(e.get("count", 1)) + 1
                rec_n += 1
                if commit:
                    e["last_seen"] = today
                    e["count"] = int(e.get("count", 1)) + 1
                    e["tokens"] = sorted(set(e.get("tokens", [])) | toks)
                    used.add(idx)
        if commit:
            self.save()
        return {"new": new_n, "recurring": rec_n}
