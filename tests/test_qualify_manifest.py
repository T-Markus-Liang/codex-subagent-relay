import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "qualify_manifest.py"
spec = importlib.util.spec_from_file_location("qualify_manifest", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


class QualifyManifestTests(unittest.TestCase):
    def report(self, provider, runs=100, successes=100, write=False):
        return {
            "provider": provider,
            "role": "documentation" if write else "repository-exploration",
            "runs": runs,
            "successes": successes,
            "workspace_verification_failures": 0,
            "partial_write_runs": 0,
            "duplicate_write_incidents": 0 if write else None,
            "write_safety_violations": 0 if write else 0,
        }

    def test_explicit_reports_build_a_record(self):
        record = module.build_record(
            "sensenova1", 1.0, 0.98, self.report("sensenova1"), self.report("sensenova1", successes=98, write=True)
        )
        self.assertEqual(record["route"], "sensenova1")
        self.assertEqual(record["write_jobs"], 100)
        self.assertEqual(record["duplicate_writes"], 0)

    def test_auto_report_is_rejected_as_route_evidence(self):
        with self.assertRaises(module.ManifestError):
            module.validate_report(self.report("auto"), label="read report", route="sensenova1", required_runs=100, minimum_rate=.95, write=False)

    def test_write_report_requires_explicit_zero_duplicate_metric(self):
        report = self.report("sensenova1", write=True)
        report["duplicate_write_incidents"] = None
        with self.assertRaises(module.ManifestError):
            module.validate_report(report, label="write report", route="sensenova1", required_runs=100, minimum_rate=.95, write=True)


if __name__ == "__main__":
    unittest.main()
