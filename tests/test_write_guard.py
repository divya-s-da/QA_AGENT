"""Decision rules of the read-only write guard (no browser needed)."""

import unittest

from tools import write_guard as wg


class DecideTests(unittest.TestCase):
    def setUp(self):
        wg.configure(allow_writes=False, allowlist=["*/api/search"])

    def tearDown(self):
        wg.configure(allow_writes=False)

    def test_reads_pass(self):
        for method in ("GET", "HEAD", "OPTIONS", "get"):
            self.assertIsNone(wg.decide(method, "https://app.test/api/items"))

    def test_writes_blocked(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertIsNotNone(wg.decide(method, "https://app.test/api/items"))

    def test_allowlist(self):
        self.assertIsNone(wg.decide("POST", "https://app.test/api/search"))
        self.assertIsNotNone(wg.decide("POST", "https://app.test/api/search/save"))

    def test_allow_mode(self):
        wg.configure(allow_writes=True)
        self.assertIsNone(wg.decide("DELETE", "https://app.test/api/items/1"))

    def test_login_window_is_host_scoped_and_closes(self):
        with wg.login_window("app.test"):
            self.assertIsNone(wg.decide("POST", "https://app.test/api/login"))
            self.assertIsNone(wg.decide("POST", "https://auth.app.test/token"))
            self.assertIsNotNone(wg.decide("POST", "https://tracker.example/collect"))
        self.assertIsNotNone(wg.decide("POST", "https://app.test/api/login"))


class GraphQLTests(unittest.TestCase):
    def setUp(self):
        wg.configure(allow_writes=False)

    def post(self, body):
        return wg.decide("POST", "https://app.test/graphql", body)

    def test_query_passes(self):
        self.assertIsNone(self.post('{"query": "query Items { items { id } }"}'))
        self.assertIsNone(self.post('{"query": "{ items { id } }"}'))
        self.assertIsNone(self.post('[{"query": "{ a }"}, {"query": "query B { b }"}]'))

    def test_mutation_and_unknown_blocked(self):
        self.assertIsNotNone(self.post('{"query": "mutation { deleteItem(id: 1) }"}'))
        self.assertIsNotNone(self.post('{"query": "# note\\nmutation X { a }"}'))
        self.assertIsNotNone(self.post('[{"query": "{ a }"}, {"query": "mutation { b }"}]'))
        # Persisted query: no text to inspect, so it counts as a write.
        self.assertIsNotNone(self.post('{"extensions": {"persistedQuery": {"sha256Hash": "ab"}}}'))
        self.assertIsNotNone(self.post("not json"))
        self.assertIsNotNone(self.post(None))


class SafeWriteTests(unittest.TestCase):
    def setUp(self):
        wg.configure(mode="safe")

    def tearDown(self):
        wg.configure(mode="read-only")

    def test_mode_label(self):
        self.assertEqual(wg.mode_label(), "safe-write")
        self.assertFalse(wg.writes_allowed())
        self.assertTrue(wg.safe_mode())

    def test_reads_pass(self):
        self.assertIsNone(wg.decide("GET", "https://app.test/api/items"))

    def test_create_post_allowed(self):
        self.assertIsNone(wg.decide("POST", "https://app.test/api/items"))

    def test_delete_and_edit_blocked(self):
        self.assertIsNotNone(wg.decide("DELETE", "https://app.test/api/items/1"))
        self.assertIsNotNone(wg.decide("PUT", "https://app.test/api/items/1"))
        self.assertIsNotNone(wg.decide("PATCH", "https://app.test/api/items/1"))

    def test_destructive_or_financial_post_blocked(self):
        for url in ("https://app.test/api/checkout", "https://app.test/cart/pay",
                    "https://app.test/api/items/1/delete", "https://app.test/api/order/cancel",
                    "https://app.test/api/refund"):
            self.assertIsNotNone(wg.decide("POST", url), url)

    def test_ledger_records_only_writes_let_through(self):
        before = len(wg.writes_made())
        wg.decide("GET", "https://app.test/api/items")        # read: not recorded
        wg.decide("DELETE", "https://app.test/api/items/1")   # blocked: not recorded
        wg.decide("POST", "https://app.test/api/items")       # create: recorded
        self.assertEqual(len(wg.writes_made()), before + 1)
        self.assertEqual(wg.writes_made()[-1]["kind"], "safe-create")


if __name__ == "__main__":
    unittest.main()
