import importlib.util
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/operational_report.py"
spec = importlib.util.spec_from_file_location("operational_report", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


class OperationalReportTests(unittest.TestCase):
    def test_source_local_daily_provider_and_run_type_aggregates(self):
        now = datetime(2026, 8, 14, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            rows = [
                {"timestamp": "2026-08-13T00:00:00+00:00", "status": "success", "provider": "sensenova", "run_type": "external_run", "duration_seconds": 10},
                {"timestamp": "2026-08-13T01:00:00+00:00", "status": "error", "provider": "sensenova", "run_type": "external_run", "duration_seconds": 20, "fallback_used": True, "stream_retry_count": 1},
                {"timestamp": "2026-08-12T00:00:00+00:00", "status": "success", "provider": "sensenova1", "run_type": "native_v1_canary", "duration_seconds": 30},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\nnot-json\n", encoding="utf-8")
            payload = module.report(path, 7, now)
        self.assertEqual(payload["overall"]["runs"], 3)
        self.assertEqual(payload["overall"]["success_rate_percent"], 66.67)
        self.assertEqual(payload["providers"]["sensenova"]["fallback_runs"], 1)
        self.assertEqual(payload["run_types"]["native_v1_canary"]["runs"], 1)
        self.assertEqual(payload["invalid_records_skipped"], 1)
        self.assertIn("Do not add", payload["coverage_warning"])

    def test_missing_log_reports_empty_source_without_creating_it(self):
        path = Path(tempfile.gettempdir()) / "no-such-relay-operations-log.jsonl"
        path.unlink(missing_ok=True)
        payload = module.report(path, 7, datetime(2026, 8, 14, tzinfo=UTC))
        self.assertFalse(payload["log_present"])
        self.assertEqual(payload["overall"]["runs"], 0)


if __name__ == "__main__":
    unittest.main()
