"""Server safety: SSRF blocking and per-owner run isolation (no HTTP needed)."""

import json
import os
import tempfile
import unittest

from v2 import server as S


class SsrfTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("V2_ALLOW_PRIVATE_HOSTS", None)

    def test_blocks_internal(self):
        for u in ("http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/latest",
                  "http://10.0.0.1/", "http://192.168.1.10/"):
            self.assertIsNotNone(S.ssrf_reason(u), u)

    def test_allows_public(self):
        # public host resolves -> None; if DNS is unavailable in the sandbox it also returns None (do not block on a hiccup)
        self.assertIsNone(S.ssrf_reason("https://example.com/"))

    def test_override_allows_private(self):
        os.environ["V2_ALLOW_PRIVATE_HOSTS"] = "1"
        try:
            self.assertIsNone(S.ssrf_reason("http://127.0.0.1:8000/"))
        finally:
            os.environ.pop("V2_ALLOW_PRIVATE_HOSTS", None)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.jobs = S.Jobs(self.dir)
        for rid, owner in [("aaaaaaaaaaaa", "alice"), ("bbbbbbbbbbbb", "bob"), ("cccccccccccc", "alice")]:
            os.makedirs(os.path.join(self.dir, rid), exist_ok=True)
            with open(os.path.join(self.dir, rid, "job.json"), "w", encoding="utf-8") as f:
                json.dump({"id": rid, "url": "https://x.com/", "status": "done", "created": 1, "owner": owner}, f)

    def test_owner_of(self):
        self.assertEqual(self.jobs.owner_of("aaaaaaaaaaaa"), "alice")
        self.assertIsNone(self.jobs.owner_of("zzzzzzzzzzzz"))

    def test_all_filters_by_owner(self):
        self.assertEqual(len(self.jobs.all(owner="alice")), 2)
        self.assertEqual(len(self.jobs.all(owner="bob")), 1)
        self.assertEqual(len(self.jobs.all(owner=None)), 3)      # operator sees every run


if __name__ == "__main__":
    unittest.main()
