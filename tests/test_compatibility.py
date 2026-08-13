import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_compatibility.py"
spec = importlib.util.spec_from_file_location("validate_compatibility", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


class CompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((SCRIPT.parent.parent / "compatibility" / "manifest.json").read_text(encoding="utf-8"))

    def test_tracked_manifest_is_structurally_valid_without_live_evidence(self):
        result = module.validate_manifest(self.manifest)
        self.assertEqual(result["routes"], ["sensenova", "sensenova1"])
        self.assertEqual(result["records"], 0)

    def test_complete_evidence_passes_publication_gate(self):
        payload = copy.deepcopy(self.manifest)
        payload["evidence"]["records"] = [
            {
                "route": provider,
                "read_jobs": 100,
                "write_jobs": 100,
                "read_success_rate": 0.99,
                "write_success_rate": 0.98,
                "duplicate_writes": 0,
            }
            for provider in ("sensenova", "sensenova1")
        ]
        self.assertEqual(module.validate_manifest(payload, require_evidence=True)["records"], 2)

    def test_unknown_field_and_native_promotion_are_rejected(self):
        unknown = copy.deepcopy(self.manifest)
        unknown["unexpected"] = True
        with self.assertRaises(module.ManifestError):
            module.validate_manifest(unknown)
        promoted = copy.deepcopy(self.manifest)
        promoted["native"]["status"] = "production"
        with self.assertRaises(module.ManifestError):
            module.validate_manifest(promoted)

    def test_failing_evidence_is_rejected(self):
        payload = copy.deepcopy(self.manifest)
        payload["evidence"]["records"] = [{
            "route": "sensenova",
            "read_jobs": 100,
            "write_jobs": 99,
            "read_success_rate": 0.99,
            "write_success_rate": 0.99,
            "duplicate_writes": 0,
        }]
        with self.assertRaises(module.ManifestError):
            module.validate_manifest(payload, require_evidence=True)

    def test_duplicate_evidence_routes_are_rejected(self):
        payload = copy.deepcopy(self.manifest)
        record = {
            "route": "sensenova",
            "read_jobs": 100,
            "write_jobs": 100,
            "read_success_rate": 0.99,
            "write_success_rate": 0.99,
            "duplicate_writes": 0,
        }
        payload["evidence"]["records"] = [record, copy.deepcopy(record)]
        with self.assertRaises(module.ManifestError):
            module.validate_manifest(payload, require_evidence=True)


if __name__ == "__main__":
    unittest.main()
