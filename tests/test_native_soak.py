import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/native_soak.py"
spec = importlib.util.spec_from_file_location("native_soak", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


class NativeSoakTests(unittest.TestCase):
    def test_strict_success_requires_every_native_evidence_field(self):
        payload = {"status": "success", **{field: True for field in module.V1_EVIDENCE_FIELDS}}
        self.assertTrue(module.strict_success(payload, "v1-tool"))
        payload["parallel_tool_calls_observed"] = False
        self.assertFalse(module.strict_success(payload, "v1-tool"))

    def test_error_categories_do_not_retain_raw_error_text(self):
        categories = module.error_categories(["child shell outputs did not contain both hidden challenge tokens", "unexpected upstream text"])
        self.assertEqual(categories, ["hidden_output", "process_or_other"])

    def test_report_requires_100_strict_results_and_unchanged_root_state(self):
        args = type("Args", (), {"backend": "v1-tool", "provider": "sensenova", "parent_model": "gpt-5.6-sol", "runs": 100, "min_success_rate": .95})()
        good = {"status": "success", **{field: True for field in module.V1_EVIDENCE_FIELDS}}
        rows = [module.record(index, good, "v1-tool", 1.0) for index in range(1, 101)]
        self.assertTrue(module.report(args, rows, None)["promotion_eligible"])
        rows[-1]["evidence"]["state_db_unchanged"] = False
        self.assertFalse(module.report(args, rows, None)["promotion_eligible"])


if __name__ == "__main__":
    unittest.main()
