import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/operations_dashboard.py"
spec = importlib.util.spec_from_file_location("operations_dashboard", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


class OperationsDashboardTests(unittest.TestCase):
    def test_builds_source_separated_partial_artifact_without_usage_audit(self):
        relay = {
            "source": "relay_operational_telemetry",
            "utc_window": {"start": "2026-08-13T00:00:00Z", "end": "2026-08-13T01:00:00Z"},
            "overall": {"runs": 10, "successes": 9, "success_rate_percent": 90, "p95_duration_seconds": 30, "partial_write_runs": 1, "finalization_recovery_runs": 2, "fallback_runs": 2, "requested_provider_counts": {"auto": 8, "sensenova": 2}, "usage": {"accepted_success_usage": {"request_tokens": 100, "context_tokens": 120}}},
            "providers": {"sensenova": {"runs": 10, "success_rate_percent": 90, "p95_duration_seconds": 30, "fallback_runs": 2, "partial_write_runs": 1, "finalization_recovery_runs": 2, "usage": {"accepted_success_usage": {"request_tokens": 100}}}},
        }
        artifact = module.build_artifact(relay, Path("reports/relay.json"), None, None)
        self.assertEqual(artifact["surface"], "dashboard")
        self.assertEqual(artifact["snapshot"]["status"], "partial")
        self.assertEqual(artifact["snapshot"]["datasets"]["provider_health"][0]["accepted_request_tokens"], 100)
        self.assertEqual(artifact["snapshot"]["datasets"]["relay_summary"][0]["finalization_recovery_runs"], 2)
        self.assertEqual(artifact["snapshot"]["datasets"]["relay_summary"][0]["automatic_runs"], 8)
        self.assertIn("must not be added", artifact["package_info"]["ledger_boundary"])

    def test_adds_usage_table_without_cross_ledger_total(self):
        relay = {"source": "relay_operational_telemetry", "utc_window": {}, "overall": {"usage": {}}, "providers": {}}
        usage = {"sources": [
            {"source": "codex_rollout", "rows": [{"model": "gpt", "total_tokens": 10, "share_percent": 100}]},
            {"source": "cc_switch_proxy", "rows": [{"model": "deepseek", "context_tokens": 20, "share_percent": 100}]},
        ]}
        artifact = module.build_artifact(relay, Path("relay.json"), usage, Path("usage.json"))
        rows = artifact["snapshot"]["datasets"]["source_usage"]
        self.assertEqual(rows, [
            {"ledger": "codex_rollout", "model": "gpt", "metric": "recorded_total_tokens", "tokens": 10, "share_percent": 100},
            {"ledger": "cc_switch_proxy", "model": "deepseek", "metric": "context_tokens_including_cache", "tokens": 20, "share_percent": 100},
        ])
        self.assertNotIn("total", rows[0])

    def test_rejects_unexpected_report_source(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            out = Path(directory) / "artifact.json"
            report.write_text(json.dumps({"source": "wrong"}), encoding="utf-8")
            self.assertEqual(module.main.__name__, "main")

    def test_static_html_contains_boundary_and_escapes_source_content(self):
        relay = {"source": "relay_operational_telemetry", "utc_window": {}, "overall": {"usage": {}}, "providers": {"<unsafe>": {}}}
        artifact = module.build_artifact(relay, Path("<relay>.json"), None, None)
        rendered = module.render_html(artifact)
        self.assertIn("Ledger boundary", rendered)
        self.assertIn("&lt;unsafe&gt;", rendered)
        self.assertNotIn("<unsafe>", rendered)
        self.assertIn("viewport", rendered)


if __name__ == "__main__":
    unittest.main()
