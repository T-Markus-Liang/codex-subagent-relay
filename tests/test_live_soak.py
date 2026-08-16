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
        self.assertEqual(module.failure_class({"status": "blocked", "attempt_failure_categories": ["provider_busy"]}, "ok"), "worker:provider_busy")
        self.assertEqual(
            module.failure_class(
                {"status": "blocked", "attempt_failure_categories": ["provider_busy"]},
                "write task produced no output",
            ),
            "worker:provider_busy",
        )
        self.assertIsNone(module.failure_class({"status": "success"}, "ok"))

    def test_consecutive_failure_stop_requires_only_recent_non_successes(self):
        failed = {"status": "error", "expected_output": False}
        succeeded = {"status": "success", "expected_output": True}
        self.assertIsNone(module.consecutive_failure_stop([failed, failed], 3))
        self.assertEqual(
            module.consecutive_failure_stop([succeeded, failed, failed, failed], 3),
            "3 consecutive non-success attempts",
        )
        self.assertIsNone(module.consecutive_failure_stop([failed, succeeded, failed], 2))
        self.assertIsNone(module.consecutive_failure_stop([failed, failed], 0))

    def test_provider_busy_stops_a_batch_as_inconclusive(self):
        self.assertEqual(
            module.consecutive_failure_stop(
                [{"status": "blocked", "expected_output": False, "failure_class": "worker:provider_busy"}],
                3,
            ),
            "provider busy; qualification batch not started",
        )

    def test_opencode_idle_preflight_keeps_only_count_and_state(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": "/bin/zsh\nopencode\n/opt/bin/opencode\n"})()
        with patch.object(module.subprocess, "run", return_value=completed):
            preflight = module.opencode_idle_preflight()
        self.assertEqual(preflight, {"opencode_idle": False, "observed_opencode_process_count": 2, "inspection": "ok"})

    def test_build_report_marks_unchecked_preflight_as_nonqualifying(self):
        args = type("Args", (), {"worker": "worker", "provider": "sensenova", "role": "documentation", "runs": 100})()
        report = module.build_report(args, [], False, None, {"opencode_idle": None, "observed_opencode_process_count": None, "inspection": "not_required"})
        self.assertEqual(report["qualification_preflight"]["inspection"], "not_required")
        self.assertEqual(report["runs"], 0)

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
