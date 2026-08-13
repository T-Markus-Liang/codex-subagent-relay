import importlib.machinery
import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "deepseek-worker"
loader = importlib.machinery.SourceFileLoader("deepseek_worker_release_gate", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)


def successful_stream(summary: str = "done") -> str:
    return "\n".join(
        map(
            json.dumps,
            [
                {"type": "tool_use", "part": {"state": {"status": "completed"}}},
                {
                    "type": "text",
                    "part": {
                        "text": json.dumps(
                            {"status": "success", "summary": summary, "files_changed": [], "tests": [], "risks": []}
                        )
                    },
                },
                {"type": "step_finish", "part": {"reason": "stop", "tokens": {}}},
            ],
        )
    )


class ReleaseGateTests(unittest.TestCase):
    def test_fault_matrix_rejects_bad_streams_and_accepts_only_complete_contracts(self):
        cases = {
            "missing_finish": "\n".join(
                map(
                    json.dumps,
                    [
                        {"type": "tool_use", "part": {"state": {"status": "completed"}}},
                        {"type": "text", "part": {"text": "not a result"}},
                    ],
                )
            ),
            "length": "\n".join(
                map(
                    json.dumps,
                    [
                        {"type": "tool_use", "part": {"state": {"status": "completed"}}},
                        {"type": "text", "part": {"text": "partial"}},
                        {"type": "step_finish", "part": {"reason": "length", "tokens": {}}},
                    ],
                )
            ),
            "no_tool": "\n".join(
                map(
                    json.dumps,
                    [
                        {"type": "text", "part": {"text": json.dumps({"status": "success", "summary": "guessed", "files_changed": [], "tests": [], "risks": []})}},
                        {"type": "step_finish", "part": {"reason": "stop", "tokens": {}}},
                    ],
                )
            ),
            "error_event": "\n".join(
                map(
                    json.dumps,
                    [
                        {"type": "tool_use", "part": {"state": {"status": "completed"}}},
                        {"type": "text", "part": {"text": json.dumps({"status": "success", "summary": "bad", "files_changed": [], "tests": [], "risks": []})}},
                        {"type": "error", "part": {}},
                        {"type": "step_finish", "part": {"reason": "stop", "tokens": {}}},
                    ],
                )
            ),
        }
        for name, stream in cases.items():
            with self.subTest(name=name):
                parsed, _usage, invalid, activity = module.parse_opencode_events(stream)
                diagnostics = module.opencode_stream_diagnostics(stream)
                self.assertTrue(invalid or diagnostics["failure"] is not None or not activity)
                self.assertFalse(parsed is not None and activity and diagnostics["failure"] is None)
        parsed, _usage, invalid, activity = module.parse_opencode_events(successful_stream())
        diagnostics = module.opencode_stream_diagnostics(successful_stream())
        self.assertEqual(parsed["status"], "success")
        self.assertFalse(invalid)
        self.assertTrue(activity)
        self.assertIsNone(diagnostics["failure"])

    def test_fallback_stress_is_bounded_and_never_accepts_invalid_primary_output(self):
        invalid = module.subprocess.CompletedProcess(
            [],
            0,
            "\n".join(
                map(
                    json.dumps,
                    [
                        {"type": "tool_use", "part": {"state": {"status": "completed"}}},
                        {"type": "text", "part": {"text": "not-json"}},
                        {"type": "step_finish", "part": {"reason": "stop", "tokens": {}}},
                    ],
                )
            ),
            "",
        )
        valid = module.subprocess.CompletedProcess([], 0, successful_stream("fallback"), "")
        args = SimpleNamespace(
            task="inspect only",
            task_file=None,
            role="repository-exploration",
            provider="auto",
            sandbox="auto",
            timeout=75,
            no_record=True,
        )
        with tempfile.TemporaryDirectory() as workdir, tempfile.TemporaryDirectory() as circuit_dir:
            args.workdir = workdir
            responses = [item for _ in range(32) for item in (invalid, valid)]
            with patch.object(module, "invoke_codex", side_effect=responses) as invoke, \
                 patch.object(module, "CIRCUIT_STATE_PATH", Path(circuit_dir) / "circuit.json"), \
                 patch.object(module, "CIRCUIT_LOCK_PATH", Path(circuit_dir) / "circuit.lock"):
                payloads = []
                for _ in range(32):
                    module.circuit_reset()
                    payloads.append(module.run_worker(args))
        self.assertEqual(invoke.call_count, 64)
        self.assertTrue(all(payload["status"] == "success" for payload in payloads))
        self.assertTrue(all(payload["provider"] == "sensenova" for payload in payloads))
        self.assertTrue(all(payload["timeout_seconds"] == 75 for payload in payloads))

    def test_parallel_write_lock_allows_one_owner_and_blocks_contenders(self):
        with tempfile.TemporaryDirectory() as workdir, tempfile.TemporaryDirectory() as codex_home:
            barrier = threading.Barrier(12)
            release_owner = threading.Event()
            results = []
            result_lock = threading.Lock()

            def contender() -> None:
                barrier.wait()
                try:
                    handle = module.workdir_lock(Path(workdir))
                except module.WorkerError:
                    outcome = "blocked"
                else:
                    outcome = "owner"
                    release_owner.wait(timeout=2)
                    module.fcntl.flock(handle.fileno(), module.fcntl.LOCK_UN)
                    handle.close()
                with result_lock:
                    results.append(outcome)

            with patch.object(module, "CODEX_HOME", Path(codex_home)):
                threads = [threading.Thread(target=contender) for _ in range(12)]
                for thread in threads:
                    thread.start()
                while len(results) < 11:
                    pass
                release_owner.set()
                for thread in threads:
                    thread.join(timeout=2)
        self.assertEqual(results.count("owner"), 1)
        self.assertEqual(results.count("blocked"), 11)

    def test_parallel_launches_keep_job_metadata_and_output_paths_isolated(self):
        class DummyProcess:
            def __init__(self, pid):
                self.pid = pid

        with tempfile.TemporaryDirectory() as workdir, tempfile.TemporaryDirectory() as jobs:
            counter = iter(range(1000, 1024))
            token_counter = iter(f"{number:012x}" for number in range(24))
            args = SimpleNamespace(
                task="inspect only",
                task_file=None,
                role="repository-exploration",
                workdir=workdir,
                provider="auto",
                sandbox="read-only",
                timeout=0,
                no_record=True,
            )
            launched = []
            launched_lock = threading.Lock()

            def launch() -> None:
                result = module.launch_worker(args)
                with launched_lock:
                    launched.append(result)

            with patch.object(module, "JOB_ROOT", Path(jobs)), patch.object(module, "workspace_snapshot", return_value={}), patch.object(module.time, "time", return_value=1_700_000_000), patch.object(module.secrets, "token_hex", side_effect=token_counter), patch.object(module.subprocess, "Popen", side_effect=lambda *a, **k: DummyProcess(next(counter))):
                threads = [threading.Thread(target=launch) for _ in range(24)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=2)
                job_ids = [item["job_id"] for item in launched]
                self.assertEqual(len(job_ids), 24)
                self.assertEqual(len(set(job_ids)), 24)
                for job_id in job_ids:
                    metadata, stdout, stderr = module.job_paths(job_id)
                    self.assertTrue(metadata.is_file())
                    self.assertTrue(stdout.is_file())
                    self.assertTrue(stderr.is_file())

    def test_run_log_never_persists_task_text_or_secret_like_risk_content(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            log_path = Path(temporary_dir) / "runs.jsonl"
            payload = {
                "status": "error",
                "provider": "sensenova",
                "role": "implementation",
                "duration_seconds": 1.0,
                "usage": {},
                "summary": "do not record this task body",
                "risks": ["do not record secret sk-example-value"],
            }
            with patch.object(module, "RUN_LOG_PATH", log_path):
                module.record_run(payload)
            recorded = log_path.read_text(encoding="utf-8")
        self.assertNotIn("task body", recorded)
        self.assertNotIn("sk-example-value", recorded)
        self.assertEqual(set(json.loads(recorded).keys()), {"timestamp", "run_type", "status", "provider", "role", "duration_seconds", "stream_finish_reason", "stream_retry_count", "usage"})


if __name__ == "__main__":
    unittest.main()
