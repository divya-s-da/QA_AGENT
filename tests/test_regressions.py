"""Fast regression tests for deterministic QA pipeline behavior."""

import json
import unittest

from tools.evidence_tools import coverage_report
from tools.report_builder import build_docx_report


class FakeToolContext:
    def __init__(self, evidence):
        self.state = {"test_evidence": evidence}


class CoverageReportTests(unittest.TestCase):
    def test_root_route_is_valid(self):
        ctx = FakeToolContext([
            {"check_id": "home_click", "url": "https://example.test/"},
        ])
        result = json.loads(coverage_report(ctx, planned_routes='["/"]'))
        self.assertEqual(result["routes_total"], 1)
        self.assertEqual(result["routes_covered"], 1)
        self.assertEqual(result["verdict"], "COMPLETE")

    def test_inventory_route_punctuation_is_normalized(self):
        ctx = FakeToolContext([
            {"check_id": "inventory", "note": "Routes: /home, /settings."},
            {"check_id": "home_click", "note": "[url: https://example.test/home]"},
        ])
        result = json.loads(coverage_report(ctx))
        self.assertEqual(result["untested_routes"], ["/settings"])
        self.assertEqual(result["routes_covered"], 1)

    def test_inventory_ignores_urls_and_local_paths_in_notes(self):
        ctx = FakeToolContext([
            {
                "check_id": "inventory",
                "note": (
                    "Full inventory = [1] / (landing page); external href "
                    "https://iana.org/domains/example; screenshot saved to "
                    "/home/divya/react_qa_agent/home_visual.png."
                ),
            },
            {"check_id": "home_visual", "url": "https://example.test/"},
        ])
        result = json.loads(coverage_report(ctx, planned_routes='["/"]'))
        self.assertEqual(result["routes_total"], 1)
        self.assertEqual(result["routes_covered"], 1)
        self.assertNotIn("/example", result["checks_per_route"])
        self.assertNotIn("/home/divya", result["checks_per_route"])


class InteractiveReportTests(unittest.TestCase):
    def test_blocked_only_session_is_not_reported_as_pass(self):
        class Context:
            state = {"test_evidence": [
                {"check_id": "login", "status": "BLOCKED", "note": "tool unavailable"}
            ]}

        import tools.report_builder as module
        captured = {}
        original = module.generate_docx_report
        module.generate_docx_report = lambda **kwargs: captured.update(kwargs) or "/tmp/report.docx"
        try:
            result = build_docx_report(Context(), site_url="https://example.test")
        finally:
            module.generate_docx_report = original
        payload = json.loads(captured["all_screen_results_json"])
        self.assertEqual(payload[0]["overall"], "BLOCKED")
        self.assertIn("/tmp/report.docx", result)


if __name__ == "__main__":
    unittest.main()
