import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/live_soak.py"
spec = importlib.util.spec_from_file_location("live_soak", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


class LiveSoakTests(unittest.TestCase):
    def test_read_only_workspace_must_remain_empty(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            workdir = Path(temporary_dir)
            self.assertEqual(module.verify_workspace(workdir, "repository-exploration"), (True, True, "ok"))
            (workdir / "unexpected.txt").write_text("x", encoding="utf-8")
            self.assertEqual(module.verify_workspace(workdir, "repository-exploration"), (False, False, "read-only workspace changed"))

    def test_write_workspace_requires_exact_single_file_and_content(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            workdir = Path(temporary_dir)
            (workdir / module.SOAK_FILENAME).write_text(module.SOAK_CONTENT, encoding="utf-8")
            self.assertEqual(module.verify_workspace(workdir, "documentation"), (True, True, "ok"))
            (workdir / "extra.txt").write_text("x", encoding="utf-8")
            self.assertEqual(module.verify_workspace(workdir, "documentation"), (False, False, "write task created unexpected paths"))

    def test_failed_write_with_no_files_is_not_a_workspace_safety_violation(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            self.assertEqual(
                module.verify_workspace(Path(temporary_dir), "documentation"),
                (True, False, "write task produced no output"),
            )

    def test_write_task_requires_exact_no_newline_bytes(self):
        self.assertIn("printf %s", module.task_for("documentation"))
        self.assertIn("no trailing newline", module.task_for("documentation"))
        self.assertEqual(module.SOAK_CONTENT.encode(), b"deepseek-worker soak")

    def test_failure_class_includes_workspace_and_stream_failures(self):
        self.assertEqual(module.failure_class({"status": "success"}, "write content mismatch"), "write content mismatch")
        self.assertEqual(module.failure_class({"status": "error", "stream_finish_reason": "length"}, "ok"), "stream:length")
        self.assertIsNone(module.failure_class({"status": "success"}, "ok"))

    def test_run_job_launches_then_polls_the_same_durable_job(self):
        class Completed:
            def __init__(self, stdout):
                self.stdout = stdout

        responses = iter([
            Completed(json.dumps({"status": "running", "job_id": "job-1"})),
            Completed(json.dumps({"status": "running", "job_id": "job-1"})),
            Completed(json.dumps({"status": "success", "job_id": "job-1"})),
        ])
        with patch.object(module.subprocess, "run", side_effect=lambda *args, **kwargs: next(responses)) as run, \
             patch.object(module.time, "sleep"):
            payload, _duration = module.run_job("worker", ["--role", "documentation"], 0.1)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(run.call_args_list[0].args[0][:3], ["worker", "--json", "launch"])
        self.assertEqual(run.call_args_list[1].args[0], ["worker", "--json", "poll", "--job-id", "job-1"])

    def test_run_job_cancels_same_job_at_outer_deadline(self):
        class Completed:
            def __init__(self, stdout):
                self.stdout = stdout

        responses = iter([
            Completed(json.dumps({"status": "running", "job_id": "job-timeout"})),
            Completed(json.dumps({"status": "running", "job_id": "job-timeout"})),
            Completed(json.dumps({"status": "blocked", "job_id": "job-timeout", "job_state": "cancelled"})),
        ])
        clock = iter([0.0, 0.0, 2.0, 2.0, 2.0])
        with patch.object(module.subprocess, "run", side_effect=lambda *args, **kwargs: next(responses)) as run, \
             patch.object(module.time, "monotonic", side_effect=lambda: next(clock)), \
             patch.object(module.time, "sleep"):
            payload, _duration = module.run_job("worker", ["--role", "documentation"], 0.1, deadline_seconds=1.0)
        self.assertEqual(payload["job_state"], "cancelled")
        self.assertEqual(run.call_args_list[-1].args[0], ["worker", "--json", "cancel", "--job-id", "job-timeout"])


if __name__ == "__main__":
    unittest.main()
