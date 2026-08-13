import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/live_soak.py"
spec = importlib.util.spec_from_file_location("live_soak", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


class LiveSoakTests(unittest.TestCase):
    def test_read_only_workspace_must_remain_empty(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            workdir = Path(temporary_dir)
            self.assertEqual(module.verify_workspace(workdir, "repository-exploration"), (True, "ok"))
            (workdir / "unexpected.txt").write_text("x", encoding="utf-8")
            self.assertEqual(module.verify_workspace(workdir, "repository-exploration"), (False, "read-only workspace changed"))

    def test_write_workspace_requires_exact_single_file_and_content(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            workdir = Path(temporary_dir)
            (workdir / module.SOAK_FILENAME).write_text(module.SOAK_CONTENT, encoding="utf-8")
            self.assertEqual(module.verify_workspace(workdir, "documentation"), (True, "ok"))
            (workdir / "extra.txt").write_text("x", encoding="utf-8")
            self.assertEqual(module.verify_workspace(workdir, "documentation"), (False, "write task created unexpected paths"))

    def test_write_task_requires_exact_no_newline_bytes(self):
        self.assertIn("printf %s", module.task_for("documentation"))
        self.assertIn("no trailing newline", module.task_for("documentation"))
        self.assertEqual(module.SOAK_CONTENT.encode(), b"deepseek-worker soak")

    def test_failure_class_includes_workspace_and_stream_failures(self):
        self.assertEqual(module.failure_class({"status": "success"}, "write content mismatch"), "write content mismatch")
        self.assertEqual(module.failure_class({"status": "error", "stream_finish_reason": "length"}, "ok"), "stream:length")
        self.assertIsNone(module.failure_class({"status": "success"}, "ok"))


if __name__ == "__main__":
    unittest.main()
