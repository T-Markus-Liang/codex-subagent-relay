import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "deepseek-worker"
loader = importlib.machinery.SourceFileLoader("deepseek_worker", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.runtime_dir = tempfile.TemporaryDirectory()
        root = Path(self.runtime_dir.name)
        self.patches = [
            patch.object(module, "CIRCUIT_STATE_PATH", root / "circuit.json"),
            patch.object(module, "CIRCUIT_LOCK_PATH", root / "circuit.lock"),
            patch.object(module, "PROVIDER_LOCK_ROOT", root / "provider-locks"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.runtime_dir.cleanup()

    def test_declarative_runtime_config_matches_current_production_policy(self):
        config = module.load_runtime_config(module.ROOT / "relay.toml")
        self.assertEqual(config["fallback"]["read_only"], ("sensenova", "sensenova1"))
        self.assertEqual(config["fallback"]["worker"], ("sensenova", "sensenova1"))
        self.assertEqual(config["providers"]["sensenova1"]["adapter"], "opencode")
        self.assertEqual(config["timeouts"]["read_only_seconds"], 75)

    def test_declarative_runtime_config_rejects_unknown_adapter(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / "relay.toml"
            source = (module.ROOT / "relay.toml").read_text(encoding="utf-8")
            path.write_text(source.replace('adapter = "opencode"', 'adapter = "shell"', 1), encoding="utf-8")
            with self.assertRaises(module.WorkerError):
                module.load_runtime_config(path)

    def test_native_role_layers_are_minimal_and_do_not_require_user_role_for_builtin(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            source = Path(temporary_dir) / "explorer.toml"
            source.write_text('name = "explorer"\nmodel = "deepseek-v4-flash"\nmodel_provider = "sensenova"\n', encoding="utf-8")
            self.assertIsNone(module.native_role_layer_text("explorer", "sensenova", "builtin", source))
            minimal = tomllib.loads(module.native_role_layer_text("explorer", "sensenova", "minimal", source))
            model_only = tomllib.loads(module.native_role_layer_text("explorer", "sensenova", "model-only", source))
            provider_only = tomllib.loads(module.native_role_layer_text("explorer", "sensenova", "provider-only", source))
            self.assertNotIn("model", minimal)
            self.assertNotIn("model_provider", minimal)
            self.assertEqual(model_only["model"], "deepseek-v4-flash")
            self.assertEqual(provider_only["model_provider"], "sensenova")
            self.assertEqual(module.native_role_layer_text("explorer", "sensenova", "full", source), source.read_text(encoding="utf-8"))

    def test_native_role_bisection_stops_before_upstream_when_spawn_is_unavailable(self):
        blocked = {
            "status": "blocked", "role_layer": "builtin", "spawned": False,
            "provider_completed": False, "errors": ["agent type is currently not available"],
        }
        with patch.object(module, "native_v1_canary", return_value=blocked) as canary:
            payload = module.native_role_bisection("sensenova", Path.cwd(), 60, "gpt-5.6-sol")
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["layers_attempted"], ["builtin"])
        self.assertEqual(payload["first_failing_layer"], "builtin")
        self.assertFalse(payload["production_eligible"])
        canary.assert_called_once_with("sensenova", Path.cwd(), 60, "gpt-5.6-sol", role_layer="builtin")

    def test_native_role_bisection_continues_to_first_failing_custom_layer(self):
        outcomes = [
            {"status": "success", "role_layer": "builtin", "spawned": True},
            {"status": "success", "role_layer": "minimal", "spawned": True},
            {"status": "blocked", "role_layer": "model-only", "spawned": True},
        ]
        with patch.object(module, "native_v1_canary", side_effect=outcomes) as canary:
            payload = module.native_role_bisection("sensenova", Path.cwd(), 60)
        self.assertEqual(payload["layers_attempted"], ["builtin", "minimal", "model-only"])
        self.assertEqual(payload["first_failing_layer"], "model-only")
        self.assertEqual(canary.call_count, 3)

    def test_opencode_adapter_command_is_scoped_to_role_and_workdir(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            command = module.adapter_command("sensenova1", Path(temporary_dir), "repository-exploration", "read-only", "inspect only")
        self.assertEqual(command[0], str(module.OPENCODE_PATH))
        self.assertEqual(command[command.index("--dir") + 1], temporary_dir)
        self.assertEqual(command[command.index("--agent") + 1], "plan")
        self.assertEqual(command[command.index("--model") + 1], "sensenova1/deepseek-v4-flash")

    def test_finalization_command_reuses_session_and_preserves_role_sandbox(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            command = module.finalization_command(Path(temporary_dir), "session_abcdefgh", "read-only")
        self.assertEqual(command[command.index("--session") + 1], "session_abcdefgh")
        self.assertEqual(command[command.index("--agent") + 1], "plan")
        self.assertIn("Do not make any changes", command[-1])
        self.assertEqual(module.finalization_timeout(60), 15)
        self.assertEqual(module.finalization_timeout(4), 4)

    def test_invoke_codex_closes_stdin(self):
        class DummyProcess:
            pid = 123
            returncode = 0

            def communicate(self, timeout):
                self.timeout = timeout
                return "", ""

        with patch.object(module.subprocess, "Popen", return_value=DummyProcess()) as popen:
            module.invoke_codex(["codex", "exec", "test"], Path.cwd(), 5)
        self.assertIs(popen.call_args.kwargs["stdin"], module.subprocess.DEVNULL)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_launched_job_provider_has_its_own_group_for_safe_timeout(self):
        class DummyProcess:
            pid = 123
            returncode = 0

            def communicate(self, timeout):
                return "", ""

        with patch.object(module.subprocess, "Popen", return_value=DummyProcess()) as popen:
            module.invoke_codex(["opencode", "run"], Path.cwd(), 5, env={"DEEPSEEK_WORKER_LAUNCHED": "1"})
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_launched_job_timeout_signals_only_provider_group(self):
        class DummyProcess:
            pid = 123
            returncode = 0
            calls = 0

            def communicate(self, timeout):
                self.calls += 1
                if self.calls == 1:
                    raise module.subprocess.TimeoutExpired(["opencode"], timeout)
                return "", ""

        with patch.object(module.subprocess, "Popen", return_value=DummyProcess()), \
             patch.object(module.os, "killpg") as killpg:
            result = module.invoke_codex(["opencode", "run"], Path.cwd(), 5, env={"DEEPSEEK_WORKER_LAUNCHED": "1"})
        self.assertEqual(result.returncode, 124)
        killpg.assert_called_once_with(123, module.signal.SIGTERM)

    def test_cancellation_stops_worker_and_active_provider_groups(self):
        metadata = {"pid": 100, "active_provider_pid": 200}
        with patch.object(module, "terminate_process_group", side_effect=[True, True]) as terminate:
            self.assertTrue(module.terminate_job_processes(metadata))
        self.assertEqual(terminate.call_args_list[0].args, (100,))
        self.assertEqual(terminate.call_args_list[1].args, (200,))

    def test_launch_and_poll_use_nonblocking_job_files(self):
        class DummyProcess:
            pid = 4242

        with tempfile.TemporaryDirectory() as workdir, tempfile.TemporaryDirectory() as jobs:
            args = SimpleNamespace(
                task="inspect only",
                task_file=None,
                role="repository-exploration",
                workdir=workdir,
                provider="auto",
                sandbox="read-only",
                timeout=90,
                no_record=True,
            )
            with patch.object(module, "JOB_ROOT", Path(jobs)), patch.object(module, "workspace_snapshot", return_value={}), patch.object(module.secrets, "token_hex", return_value="abcdef123456"), patch.object(module.subprocess, "Popen", return_value=DummyProcess()) as popen:
                launched = module.launch_worker(args)
                self.assertEqual(launched["status"], "running")
                command = popen.call_args.args[0]
                self.assertEqual(command[command.index("--telemetry-scope") + 1], "production")
                self.assertEqual(popen.call_args.kwargs["stdin"], module.subprocess.DEVNULL)
                job_id = launched["job_id"]
                metadata, stdout, _stderr = module.job_paths(job_id)
                self.assertTrue(metadata.is_file())
                stdout.write_text('{"status":"success","summary":"done","files_changed":[],"tests":[],"risks":[]}\n')
                completed = module.poll_worker(job_id)
                self.assertEqual(completed["status"], "success")
                self.assertEqual(completed["job_state"], "succeeded")
                self.assertTrue((metadata.parent / "before.json").is_file())
                self.assertTrue((metadata.parent / "after.json").is_file())
                self.assertTrue((metadata.parent / "result.json").is_file())
                events = (metadata.parent / "events.jsonl").read_text(encoding="utf-8")
                self.assertIn('"event":"created"', events)
                self.assertIn('"event":"terminal"', events)
                self.assertFalse(stdout.exists())

    def test_launch_reuses_opaque_idempotency_key_in_same_workdir(self):
        class DummyProcess:
            pid = 4242

        with tempfile.TemporaryDirectory() as workdir, tempfile.TemporaryDirectory() as jobs:
            args = SimpleNamespace(
                task="inspect only", task_file=None, role="repository-exploration", workdir=workdir,
                provider="auto", sandbox="read-only", timeout=90, no_record=True, idempotency_key="caller-key-1",
            )
            with patch.object(module, "JOB_ROOT", Path(jobs)), patch.object(module, "workspace_snapshot", return_value={}), patch.object(module.secrets, "token_hex", return_value="abcdef123456"), patch.object(module.subprocess, "Popen", return_value=DummyProcess()) as popen:
                first = module.launch_worker(args)
                second = module.launch_worker(args)
                self.assertEqual(first["job_id"], second["job_id"])
                self.assertTrue(second["idempotent_reuse"])
                self.assertEqual(popen.call_count, 1)
                metadata = module.read_job_metadata(first["job_id"])
                self.assertNotIn("caller-key-1", json.dumps(metadata))
                self.assertEqual(metadata["state"], "running")

    def test_poll_recovers_stale_pid_as_failed_with_workspace_manifest(self):
        with tempfile.TemporaryDirectory() as workdir, tempfile.TemporaryDirectory() as jobs:
            job_id = "1700000000-abcdef123456"
            with patch.object(module, "JOB_ROOT", Path(jobs)):
                metadata, _stdout, stderr = module.job_paths(job_id)
                module.write_json(module.job_artifact_path(job_id, "before.json"), {})
                module.write_json(metadata, {
                    "schema_version": 1, "job_id": job_id, "state": "running", "pid": 99999999,
                    "started_at": module.datetime.now(module.UTC).isoformat(), "role": "implementation",
                    "provider": "auto", "workdir": workdir, "timeout_seconds": 90,
                })
                stderr.parent.mkdir(parents=True, exist_ok=True)
                stderr.write_text("stream vanished", encoding="utf-8")
                with patch.object(module, "process_is_running", return_value=False):
                    payload = module.poll_worker(job_id)
                self.assertEqual(payload["status"], "error")
                self.assertEqual(payload["job_state"], "failed")
                self.assertTrue(module.job_artifact_path(job_id, "result.json").is_file())
                self.assertTrue(module.job_artifact_path(job_id, "after.json").is_file())

    def test_zero_pid_is_never_an_active_job(self):
        self.assertFalse(module.process_is_running(0))

    def test_list_and_inspect_hide_task_and_idempotency_material(self):
        with tempfile.TemporaryDirectory() as jobs:
            job_id = "1700000000-abcdef123456"
            with patch.object(module, "JOB_ROOT", Path(jobs)):
                metadata, _stdout, _stderr = module.job_paths(job_id)
                module.write_json(metadata, {
                    "schema_version": 1, "job_id": job_id, "state": "succeeded", "created_at": "2026-08-13T00:00:00+00:00",
                    "role": "implementation", "provider": "auto", "workdir": "/private/workdir", "task_sha256": "task-digest",
                    "idempotency_key_sha256": "key-digest", "result_path": "result.json",
                })
                module.write_json(module.job_artifact_path(job_id, "result.json"), {"status": "success", "summary": "done", "files_changed": [], "tests": [], "risks": []})
                listed = module.list_jobs()
                inspected = module.inspect_job(job_id)
            rendered = json.dumps({"listed": listed, "inspected": inspected})
            self.assertNotIn("/private/workdir", rendered)
            self.assertNotIn("task-digest", rendered)
            self.assertNotIn("key-digest", rendered)
            self.assertEqual(listed["jobs"][0]["job_id"], job_id)
            self.assertEqual(inspected["result"]["status"], "success")
            self.assertNotIn("events", inspected)

    def test_cleanup_previews_only_old_terminal_jobs_and_keeps_active_or_invalid_jobs(self):
        with tempfile.TemporaryDirectory() as jobs:
            with patch.object(module, "JOB_ROOT", Path(jobs)):
                old_id = "1700000000-abcdef123456"
                recent_id = "1700000001-abcdef123457"
                active_id = "1700000002-abcdef123458"
                old_finished = (module.datetime.now(module.UTC) - module.timedelta(hours=169)).isoformat()
                recent_finished = (module.datetime.now(module.UTC) - module.timedelta(hours=1)).isoformat()
                for job_id, state, finished_at in ((old_id, "succeeded", old_finished), (recent_id, "failed", recent_finished), (active_id, "running", None)):
                    metadata, _stdout, _stderr = module.job_paths(job_id)
                    data = {"schema_version": 1, "job_id": job_id, "state": state}
                    if finished_at:
                        data["finished_at"] = finished_at
                    module.write_json(metadata, data)
                invalid = Path(jobs) / "invalid-job"
                invalid.mkdir()
                (invalid / "meta.json").write_text("not json", encoding="utf-8")

                preview = module.cleanup_terminal_jobs(168)
                self.assertTrue(preview["dry_run"])
                self.assertEqual(preview["candidate_job_ids"], [old_id])
                self.assertTrue((Path(jobs) / old_id).is_dir())
                self.assertGreaterEqual(preview["skipped"]["active"], 1)
                self.assertGreaterEqual(preview["skipped"]["invalid"], 1)

                applied = module.cleanup_terminal_jobs(168, apply=True)
                self.assertFalse(applied["dry_run"])
                self.assertEqual(applied["removed_job_ids"], [old_id])
                self.assertFalse((Path(jobs) / old_id).exists())
                self.assertTrue((Path(jobs) / recent_id).is_dir())
                self.assertTrue((Path(jobs) / active_id).is_dir())

    def test_cleanup_rejects_unbounded_retention(self):
        with self.assertRaises(module.WorkerError):
            module.cleanup_terminal_jobs(-1)
        with self.assertRaises(module.WorkerError):
            module.cleanup_terminal_jobs(module.MAX_TERMINAL_JOB_RETENTION_HOURS + 1)

    def test_legacy_flat_artifacts_are_visible_but_not_treated_as_runnable_jobs(self):
        with tempfile.TemporaryDirectory() as jobs:
            legacy_id = "1700000000-abcdef123456"
            legacy_path = Path(jobs) / f"{legacy_id}.json"
            legacy_path.write_text(json.dumps({
                "job_id": legacy_id,
                "pid": 999999,
                "started_at": "2026-08-01T00:00:00+00:00",
                "role": "repository-exploration",
                "provider": "sensenova",
                "workdir": "/private/legacy-workdir",
                "task": "must not escape list output",
            }), encoding="utf-8")
            with patch.object(module, "JOB_ROOT", Path(jobs)):
                listed = module.list_jobs()
                inspected = module.inspect_job(legacy_id)
                cleanup = module.cleanup_terminal_jobs(0)
            rendered = json.dumps({"listed": listed, "inspected": inspected, "cleanup": cleanup})
            self.assertEqual(listed["legacy"]["count"], 1)
            self.assertEqual(listed["jobs"][0]["state"], "legacy")
            self.assertEqual(inspected["job"]["legacy_action"], "archive-only")
            self.assertEqual(cleanup["skipped"]["legacy"], 1)
            self.assertNotIn("/private/legacy-workdir", rendered)
            self.assertNotIn("must not escape", rendered)
            self.assertFalse((Path(jobs) / "legacy").exists())

    def test_legacy_archive_requires_explicit_action_and_apply(self):
        with tempfile.TemporaryDirectory() as jobs:
            legacy_id = "1700000000-abcdef123456"
            legacy_path = Path(jobs) / f"{legacy_id}.json"
            legacy_path.write_text(json.dumps({"job_id": legacy_id, "pid": 999999}), encoding="utf-8")
            with patch.object(module, "JOB_ROOT", Path(jobs)):
                dry_run = module.cleanup_terminal_jobs(0, legacy_action="archive")
                self.assertTrue((Path(jobs) / f"{legacy_id}.json").is_file())
                with patch.object(module, "process_is_running", return_value=False):
                    applied = module.cleanup_terminal_jobs(0, apply=True, legacy_action="archive")
            self.assertEqual(dry_run["legacy"]["candidate_job_ids"], [legacy_id])
            self.assertEqual(applied["legacy"]["archived_job_ids"], [legacy_id])
            self.assertFalse((Path(jobs) / f"{legacy_id}.json").exists())
            self.assertTrue((Path(jobs) / "legacy" / f"{legacy_id}.json").is_file())

    def test_cancel_marks_job_without_replaying_and_preserves_changes(self):
        with tempfile.TemporaryDirectory() as workdir, tempfile.TemporaryDirectory() as jobs:
            job_id = "1700000000-abcdef123456"
            changed = Path(workdir) / "changed.txt"
            changed.write_text("existing change", encoding="utf-8")
            with patch.object(module, "JOB_ROOT", Path(jobs)):
                metadata, _stdout, _stderr = module.job_paths(job_id)
                module.write_json(module.job_artifact_path(job_id, "before.json"), {})
                module.write_json(metadata, {
                    "schema_version": 1, "job_id": job_id, "state": "running", "pid": 4242,
                    "started_at": module.datetime.now(module.UTC).isoformat(), "role": "implementation",
                    "provider": "auto", "workdir": workdir, "timeout_seconds": 90,
                })
                with patch.object(module, "terminate_process_group", return_value=True) as terminate:
                    payload = module.cancel_worker(job_id)
                self.assertEqual(payload["status"], "blocked")
                self.assertEqual(payload["files_changed"], ["changed.txt"])
                self.assertEqual(payload["job_state"], "partial")
                terminate.assert_called_once_with(4242)

    def test_timeout_with_workspace_change_is_partial_not_replayable_error(self):
        with tempfile.TemporaryDirectory() as workdir, tempfile.TemporaryDirectory() as jobs:
            job_id = "1700000000-abcdef123456"
            changed = Path(workdir) / "changed.txt"
            changed.write_text("existing change", encoding="utf-8")
            with patch.object(module, "JOB_ROOT", Path(jobs)):
                metadata, _stdout, _stderr = module.job_paths(job_id)
                module.write_json(module.job_artifact_path(job_id, "before.json"), {})
                module.write_json(metadata, {
                    "schema_version": 1, "job_id": job_id, "state": "running", "pid": 4242,
                    "started_at": (module.datetime.now(module.UTC) - module.timedelta(seconds=200)).isoformat(),
                    "role": "implementation", "provider": "auto", "workdir": workdir, "timeout_seconds": 1,
                })
                with patch.object(module, "terminate_process_group", return_value=True):
                    payload = module.poll_worker(job_id)
                self.assertEqual(payload["status"], "error")
                self.assertEqual(payload["job_state"], "partial")
                self.assertEqual(payload["files_changed"], ["changed.txt"])

    def test_native_canary_config_filters_unsupported_storage_field(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            source = Path(temporary_dir) / "source.toml"
            destination = Path(temporary_dir) / "destination.toml"
            source.write_text(
                'model_provider = "openai-chatgpt"\n'
                'disable_response_storage = true\n'
                'network_access = "enabled"\n'
                'model = "gpt-parent"\n'
                '[model_providers.openai-chatgpt]\n'
                'name = "OpenAI"\n'
                'base_url = "https://example.test/v1"\n'
                'wire_api = "responses"\n'
                'requires_openai_auth = true\n'
                'experimental_bearer_token = "managed"\n'
                '[model_providers.sensenova]\n'
                'name = "SenseNova"\n'
                'base_url = "http://127.0.0.1:15741/v1"\n'
                'wire_api = "responses"\n',
                encoding="utf-8",
            )
            module.write_native_canary_config(source, destination, "sensenova")
            copied = destination.read_text(encoding="utf-8")
            self.assertNotIn("disable_response_storage", copied)
            self.assertNotIn("network_access", copied)
            parsed = module.tomllib.loads(copied)
            self.assertEqual(parsed["model_provider"], "openai-chatgpt")
            self.assertTrue(parsed["agents"]["enabled"])
            self.assertEqual(parsed["model_providers"]["openai-chatgpt"]["base_url"], "https://example.test/v1")
            self.assertEqual(parsed["model_providers"]["sensenova"]["base_url"], "http://127.0.0.1:15741/v1")

    def test_sol_route(self):
        self.assertEqual(module.route("architecture")["target"], "sol")

    def test_terra_route(self):
        self.assertEqual(module.route("validation")["target"], "terra")

    def test_explorer_route(self):
        result = module.route("repository-exploration")
        self.assertEqual(result["provider"], module.RUNTIME_CONFIG["fallback"]["read_only"][0])
        self.assertEqual(result["sandbox"], "read-only")

    def test_worker_route(self):
        result = module.route("implementation")
        self.assertEqual(result["provider"], module.RUNTIME_CONFIG["fallback"]["worker"][0])
        self.assertEqual(result["sandbox"], "workspace-write")

    def test_worker_prompt_contains_literal_result_contract(self):
        prompt = module.worker_prompt("inspect files", "repository-exploration")
        self.assertIn('{"status":"success","summary":"concise result","files_changed":[],"tests":[],"risks":[]}', prompt)
        self.assertIn("no Markdown fence or surrounding prose", prompt)
        self.assertIn('never use 0, null, or the string "none"', prompt)

    def test_write_worker_prompt_requires_actual_changed_paths(self):
        prompt = module.worker_prompt("write one file", "documentation")
        self.assertIn('"files_changed":["relative/path-you-actually-changed"]', prompt)
        self.assertIn("empty list after a write is invalid", prompt)

    def test_opencode_command_targets_requested_workdir(self):
        invalid = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "starting"}}) + "\n"
        completed = module.subprocess.CompletedProcess([], 0, invalid, "")
        with tempfile.TemporaryDirectory() as workdir:
            args = SimpleNamespace(task="inspect", task_file=None, role="repository-exploration", workdir=workdir, provider="sensenova", sandbox="read-only", timeout=90, no_record=True)
            with patch.object(module, "invoke_codex", return_value=completed) as invoke:
                module.run_worker(args)
        self.assertIn("--dir", invoke.call_args.args[0])
        command = invoke.call_args.args[0]
        self.assertEqual(command[command.index("--dir") + 1], str(Path(workdir).resolve()))

    def test_declared_files_must_match_real_workdir_changes(self):
        response = '{"status":"success","summary":"done","files_changed":["README.md"],"tests":[],"risks":[]}'
        events = "\n".join(map(json.dumps, [
            {"type": "tool_use", "part": {"state": {"status": "completed"}}},
            {"type": "text", "part": {"text": response}},
            {"type": "step_finish", "part": {"reason": "stop"}},
        ]))
        completed = module.subprocess.CompletedProcess([], 0, events, "")
        with tempfile.TemporaryDirectory() as workdir:
            args = SimpleNamespace(task="inspect", task_file=None, role="implementation", workdir=workdir, provider="sensenova1", sandbox="workspace-write", timeout=90, no_record=True)
            with patch.object(module, "invoke_codex", return_value=completed):
                payload = module.run_worker(args)
        self.assertEqual(payload["status"], "error")
        self.assertTrue(any("declared files do not match" in risk for risk in payload["risks"]))

    def test_accepted_summary_redacts_absolute_workdir(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            workdir = Path(temporary_dir)
            response = json.dumps({"status": "success", "summary": f"Inspected {workdir} successfully", "files_changed": [], "tests": [], "risks": []})
            events = "\n".join(map(json.dumps, [
                {"type": "tool_use", "part": {"state": {"status": "completed"}}},
                {"type": "text", "part": {"text": response}},
                {"type": "step_finish", "part": {"reason": "stop"}},
            ]))
            completed = module.subprocess.CompletedProcess([], 0, events, "")
            args = SimpleNamespace(task="inspect", task_file=None, role="repository-exploration", workdir=str(workdir), provider="sensenova1", sandbox="read-only", timeout=90, no_record=True)
            with patch.object(module, "invoke_codex", return_value=completed):
                payload = module.run_worker(args)
        self.assertEqual(payload["status"], "success")
        self.assertNotIn(str(workdir), payload["summary"])
        self.assertIn("[WORKDIR]", payload["summary"])

    def test_non_git_workdir_snapshot_detects_file_changes(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            workdir = Path(temporary_dir)
            file = workdir / "README.md"
            file.write_text("before\n")
            before = module.workspace_snapshot(workdir)
            file.write_text("after\n")
            after = module.workspace_snapshot(workdir)
        self.assertEqual(module.changed_workspace_paths(before, after), ["README.md"])

    def test_auto_route(self):
        self.assertEqual(module.route("auto", "debug a failing test")["target"], "deepseek")

    def test_general_route_uses_declared_primary_provider(self):
        self.assertEqual(module.route("general-execution")["provider"], "sensenova")

    def test_auto_provider_order_is_role_specific(self):
        self.assertEqual(
            module.build_provider_order("repository-exploration", "sensenova", True),
            ["sensenova", "sensenova1"],
        )
        self.assertEqual(
            module.build_provider_order("implementation", "sensenova", True),
            ["sensenova", "sensenova1"],
        )

    def test_circuit_breaker_persists_across_calls_and_explicit_provider_bypasses(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            with patch.object(module, "CODEX_HOME", Path(temporary_dir)), \
                 patch.object(module, "CIRCUIT_STATE_PATH", Path(temporary_dir) / "circuit.json"), \
                 patch.object(module, "CIRCUIT_LOCK_PATH", Path(temporary_dir) / "circuit.lock"), \
                 patch.object(module, "time") as clock:
                clock.time.return_value = 100.0
                module.circuit_failure("sensenova")
                module.circuit_failure("sensenova")
                self.assertTrue(module.circuit_open("sensenova"))
                self.assertEqual(module.build_provider_order("repository-exploration", "sensenova", False), ["sensenova"])
                clock.time.return_value = 131.0
                self.assertFalse(module.circuit_open("sensenova"))
                module.circuit_success("sensenova1")

    def test_circuit_state_read_write_failure_is_fail_open(self):
        with patch.object(module, "CIRCUIT_LOCK_PATH", Path("/definitely/missing/circuit.lock")):
            self.assertFalse(module.circuit_open("sensenova"))

    def test_first_provider_gets_most_timeout_budget(self):
        self.assertEqual(module.provider_timeouts(75, 2), [55, 20])
        self.assertEqual(module.provider_timeouts(120, 2), [90, 30])
        self.assertEqual(sum(module.provider_timeouts(120, 2)), 120)

    def test_role_aware_timeout_defaults_are_bounded(self):
        self.assertEqual(module.effective_timeout(0, "repository-exploration"), 75)
        self.assertEqual(module.effective_timeout(0, "implementation"), 120)
        self.assertEqual(module.effective_timeout(0, "general-execution"), 120)
        self.assertEqual(module.effective_timeout(300, "repository-exploration"), 300)

    def test_write_workdir_lock_blocks_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as temporary_dir, tempfile.TemporaryDirectory() as codex_home:
            workdir = Path(temporary_dir)
            with patch.object(module, "CODEX_HOME", Path(codex_home)):
                first = module.workdir_lock(workdir)
                try:
                    with self.assertRaises(module.WorkerError):
                        module.workdir_lock(workdir)
                finally:
                    module.fcntl.flock(first.fileno(), module.fcntl.LOCK_UN)
                    first.close()
                second = module.workdir_lock(workdir)
                module.fcntl.flock(second.fileno(), module.fcntl.LOCK_UN)
                second.close()

    def test_stats_reports_statuses_and_full_usage(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            log = Path(temporary_dir) / "runs.jsonl"
            base_time = module.datetime.now(module.UTC).replace(microsecond=0)
            log.write_text("\n".join([
                json.dumps({"timestamp": base_time.isoformat(), "run_type": "external_run", "status": "success", "provider": "sensenova", "role": "repository-exploration", "duration_seconds": 10, "fallback_used": False, "partial_write": False, "stream_retry_count": 0, "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 3, "reasoning_output_tokens": 1}}),
                json.dumps({"timestamp": (base_time - module.timedelta(minutes=1)).isoformat(), "run_type": "native_v1_canary", "status": "partial", "provider": "sensenova1", "role": "implementation", "duration_seconds": 20, "fallback_used": False, "partial_write": True, "stream_retry_count": 1, "usage": {"input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 4, "reasoning_output_tokens": 2}}),
                json.dumps({"timestamp": (base_time - module.timedelta(minutes=2)).isoformat(), "run_type": "external_run", "status": "error", "provider": "sensenova1", "role": "implementation", "duration_seconds": 30, "fallback_used": True, "partial_write": False, "stream_retry_count": 0, "usage": {"input_tokens": 30, "cached_input_tokens": 6, "output_tokens": 5, "reasoning_output_tokens": 3}}),
            ]) + "\n")
            with patch.object(module, "RUN_LOG_PATH", log):
                payload = module.stats(2)
        self.assertEqual(payload["status_counts"], {"success": 1, "partial": 1, "error": 1})
        self.assertEqual(payload["usage"]["cached_input_tokens"], 15)
        self.assertEqual(payload["provider_stats"]["sensenova1"]["success_rate_percent"], 0.0)
        self.assertEqual(payload["by_run_type"]["external_run"]["runs"], 2)
        self.assertEqual(payload["by_run_type"]["native_v1_canary"]["runs"], 1)
        self.assertEqual(payload["role_stats"]["implementation"]["runs"], 2)
        self.assertEqual(payload["reliability"]["p50_duration_seconds"], 20.0)
        self.assertEqual(payload["reliability"]["p95_duration_seconds"], 30.0)
        self.assertEqual(payload["reliability"]["fallback_runs"], 1)
        self.assertEqual(payload["reliability"]["partial_write_runs"], 1)
        self.assertEqual(payload["reliability"]["stream_retry_runs"], 1)
        self.assertEqual(payload["coverage_warnings"], [])

    def test_stats_warns_and_normalizes_missing_attribution(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            log = Path(temporary_dir) / "runs.jsonl"
            timestamp = module.datetime.now(module.UTC).isoformat()
            record = {
                "timestamp": timestamp,
                "run_type": "native_v1_canary",
                "status": "blocked",
                "provider": None,
                "role": None,
                "duration_seconds": 0,
                "usage": {},
            }
            log.write_text(json.dumps(record) + "\n", encoding="utf-8")
            with patch.object(module, "RUN_LOG_PATH", log):
                payload = module.stats(1)
        self.assertIn("unknown", payload["by_provider"])
        self.assertIn("unknown", payload["role_stats"])
        self.assertEqual(len(payload["coverage_warnings"]), 1)

    def test_catalog_default_is_not_global_config(self):
        self.assertNotEqual(module.DEFAULT_CATALOG_PATH, module.CONFIG_PATH)

    def test_native_agent_provider_map_is_guarded(self):
        with self.assertRaises(module.WorkerError):
            module.native_smoke("unknown", Path.cwd(), 1)

    def test_parse_rejects_non_json_agent_message(self):
        stdout = '{"type":"item.completed","item":{"type":"agent_message","text":"I will inspect the files."}}\n'
        result, usage, invalid = module.parse_codex_events(stdout)
        self.assertIsNone(result)
        self.assertEqual(usage, {})
        self.assertTrue(invalid)

    def test_parse_accepts_only_complete_result_schema(self):
        response = '{"status":"success","summary":"done","files_changed":["a.py"],"tests":["pytest"],"risks":[]}'
        stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": response}}) + "\n"
        result, usage, invalid = module.parse_codex_events(stdout)
        self.assertEqual(result["summary"], "done")
        self.assertEqual(usage, {})
        self.assertFalse(invalid)

    def test_parse_accepts_fenced_json_and_normalizes_completed_status(self):
        response = '```json\n{"status":"completed","summary":"done","files_changed":[],"tests":[],"risks":[]}\n```'
        stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": response}}) + "\n"
        result, usage, invalid = module.parse_codex_events(stdout)
        self.assertEqual(result["status"], "success")
        self.assertEqual(usage, {})
        self.assertFalse(invalid)

    def test_parse_accepts_prose_wrapped_valid_json(self):
        response = 'Result follows: {"status":"done","summary":"done","files_changed":[],"tests":[],"risks":[]} End.'
        stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": response}}) + "\n"
        result, _usage, invalid = module.parse_codex_events(stdout)
        self.assertEqual(result["status"], "success")
        self.assertFalse(invalid)

    def test_parse_normalizes_safe_deepseek_empty_list_variants(self):
        response = '{"status":"ok","summary":"done","files_changed":0,"tests":"none","risks":"NONE"}'
        stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": response}}) + "\n"
        result, _usage, invalid = module.parse_codex_events(stdout)
        self.assertEqual(
            result,
            {"status": "success", "summary": "done", "files_changed": [], "tests": [], "risks": []},
        )
        self.assertFalse(invalid)

    def test_parse_rejects_unsafe_list_coercions(self):
        response = '{"status":"ok","summary":"done","files_changed":false,"tests":0,"risks":[]}'
        stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": response}}) + "\n"
        result, _usage, invalid = module.parse_codex_events(stdout)
        self.assertIsNone(result)
        self.assertTrue(invalid)

    def test_parse_rejects_unknown_status_even_when_json_is_wrapped(self):
        response = '```json\n{"status":"working","summary":"not final","files_changed":[],"tests":[],"risks":[]}\n```'
        stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": response}}) + "\n"
        result, _usage, invalid = module.parse_codex_events(stdout)
        self.assertIsNone(result)
        self.assertTrue(invalid)

    def test_parse_opencode_events_requires_and_reports_tool_activity(self):
        response = '```json\n{"status":"completed","summary":"inspected","files_changed":[],"tests":[],"risks":[]}\n```'
        events = [
            {"type": "tool_use", "part": {"tool": "bash", "state": {"status": "completed"}}},
            {"type": "text", "part": {"text": response}},
            {"type": "step_finish", "part": {"reason": "stop", "tokens": {"input": 10, "output": 4, "reasoning": 2, "cache": {"read": 3, "write": 1}}}},
        ]
        result, usage, invalid, activity = module.parse_opencode_events("\n".join(map(json.dumps, events)))
        self.assertEqual(result["status"], "success")
        self.assertEqual(usage["input_tokens"], 10)
        self.assertEqual(usage["cached_input_tokens"], 3)
        self.assertFalse(invalid)
        self.assertTrue(activity)

    def test_opencode_stream_diagnostics_detects_failure_reasons(self):
        length = json.dumps({"type": "step_finish", "part": {"reason": "length"}})
        missing_finish = json.dumps({"type": "text", "part": {"text": "partial"}})
        no_text = json.dumps({"type": "step_finish", "part": {"reason": "stop"}})
        self.assertEqual(module.opencode_stream_diagnostics(length)["failure"], "OpenCode stream ended with finish reason length")
        self.assertEqual(module.opencode_stream_diagnostics(missing_finish)["failure"], "OpenCode stream ended without a step_finish event")
        self.assertEqual(module.opencode_stream_diagnostics(no_text)["failure"], "OpenCode stream ended with zero usable text output")

    def test_opencode_stream_diagnostics_exposes_only_bounded_failure_categories(self):
        error = json.dumps({"type": "error", "part": {}})
        missing_finish = json.dumps({"type": "text", "part": {"text": "partial"}})
        length = json.dumps({"type": "step_finish", "part": {"reason": "length"}})
        self.assertEqual(module.opencode_stream_diagnostics(error)["failure_category"], "opencode_error_event")
        self.assertEqual(module.opencode_stream_diagnostics(missing_finish)["failure_category"], "missing_step_finish")
        self.assertEqual(module.opencode_stream_diagnostics(length)["failure_category"], "finish_reason:length")

    def test_attempt_failure_category_prefers_safety_and_stream_classifications(self):
        completed = module.subprocess.CompletedProcess([], 1, "", "raw sensitive error")
        self.assertEqual(
            module.attempt_failure_category(completed, {"failure_category": "opencode_error_event"}, None, True, False, False),
            "opencode_error_event",
        )
        self.assertEqual(
            module.attempt_failure_category(completed, {"failure_category": None}, None, True, False, True),
            "declared_diff_mismatch",
        )

    def test_native_contract_rejects_opening_prose(self):
        stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "我会先检查目录。"}}) + "\n"
        result, invalid = module.native_result_contract(stdout)
        self.assertIsNone(result)
        self.assertTrue(invalid)

    def test_native_contract_accepts_exact_final_json(self):
        response = '{"status":"success","summary":"listed root names","files_changed":[],"tests":["ls: passed"],"risks":[]}'
        stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": response}}) + "\n"
        result, invalid = module.native_result_contract(stdout)
        self.assertEqual(result["status"], "success")
        self.assertFalse(invalid)

    def test_desktop_binary_is_preferred_over_path_codex(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            desktop = Path(temporary_dir) / "codex"
            desktop.touch()
            os.chmod(desktop, 0o700)
            with patch.object(module, "DESKTOP_CODEX_PATH", desktop), patch.object(module.shutil, "which", return_value="/usr/local/bin/codex"):
                self.assertEqual(module.desktop_codex_binary(), desktop)

    def test_v1_catalog_changes_only_parent_protocol(self):
        payload = {
            "models": [
                {"slug": "gpt-parent", "multi_agent_version": "v2"},
                {"slug": "deepseek-v4-flash", "multi_agent_version": "v2"},
                {"slug": "other", "multi_agent_version": "v2"},
            ]
        }
        with tempfile.TemporaryDirectory() as temporary_dir:
            source = Path(temporary_dir) / "source.json"
            output = Path(temporary_dir) / "output.json"
            source.write_text(json.dumps(payload))
            module.build_v1_canary_catalog(source, output, "gpt-parent")
            models = {item["slug"]: item for item in json.loads(output.read_text())["models"]}
        self.assertEqual(models["gpt-parent"]["multi_agent_version"], "v1")
        self.assertEqual(models["deepseek-v4-flash"]["multi_agent_version"], "v2")
        self.assertEqual(models["other"]["multi_agent_version"], "v2")

    def test_cliproxy_catalog_forces_parent_v2_and_requires_alias(self):
        payload = {
            "models": [
                {"slug": "gpt-parent", "multi_agent_version": "v1"},
                {"slug": module.CLIPROXY_ALIAS, "use_responses_lite": True, "tool_mode": "code_mode_only"},
            ]
        }
        with tempfile.TemporaryDirectory() as temporary_dir:
            source = Path(temporary_dir) / "source.json"
            output = Path(temporary_dir) / "output.json"
            source.write_text(json.dumps(payload))
            module.build_cliproxy_canary_catalog(source, output, "gpt-parent")
            models = {item["slug"]: item for item in json.loads(output.read_text())["models"]}
        self.assertEqual(models["gpt-parent"]["multi_agent_version"], "v2")
        self.assertEqual(models[module.CLIPROXY_ALIAS]["multi_agent_version"], "v2")
        self.assertFalse(models[module.CLIPROXY_ALIAS]["use_responses_lite"])
        self.assertNotIn("tool_mode", models[module.CLIPROXY_ALIAS])
        self.assertIn(module.CLIPROXY_ALIAS, models)

    def test_child_rollout_evidence_detects_task_tools_and_parallel_calls(self):
        child_id = "019ffake-child"
        nonce = "nonce123"
        events = [
            {"type": "event_msg", "payload": {"message": nonce}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "1"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "2"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "1", "output": nonce}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "2", "output": "/tmp"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps({"status": "success", "summary": f"cliproxy-native-canary:{nonce}", "files_changed": [], "tests": ["passed"], "risks": []})}]}},
        ]
        with tempfile.TemporaryDirectory() as temporary_dir:
            rollout = Path(temporary_dir) / f"rollout-{child_id}.jsonl"
            rollout.write_text("\n".join(map(json.dumps, events)))
            evidence = module.child_rollout_evidence(
                Path(temporary_dir),
                child_id,
                nonce,
                expected_summary=f"cliproxy-native-canary:{nonce}",
                expected_outputs=(nonce, "/tmp"),
            )
        self.assertTrue(evidence["rollout_found"])
        self.assertTrue(evidence["task_payload_present"])
        self.assertEqual(evidence["tool_calls"], ["exec_command", "exec_command"])
        self.assertEqual(evidence["tool_outputs"], 2)
        self.assertTrue(evidence["expected_outputs_verified"])
        self.assertTrue(evidence["parallel_tool_calls_observed"])
        self.assertTrue(evidence["final_result_valid"])
        self.assertTrue(evidence["final_result_nonce_verified"])

    def test_find_isolated_native_child_uses_metadata_time_and_nonce(self):
        nonce = "unique-nonce"
        with tempfile.TemporaryDirectory() as temporary_dir:
            home = Path(temporary_dir)
            rollout = home / "rollout-child.jsonl"
            rollout.write_text(json.dumps({"payload": {"text": nonce}}))
            database = sqlite3.connect(home / "state_1.sqlite")
            database.execute(
                "CREATE TABLE threads (id TEXT, rollout_path TEXT, model_provider TEXT, model TEXT, "
                "reasoning_effort TEXT, agent_role TEXT, created_at INTEGER, created_at_ms INTEGER)"
            )
            database.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("child", str(rollout), "cliproxy-deepseek", module.CLIPROXY_ALIAS, "medium", "explorer", 10, 10000),
            )
            database.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("wrong-role", str(rollout), "cliproxy-deepseek", module.CLIPROXY_ALIAS, "medium", "worker", 10, 10000),
            )
            database.commit()
            database.close()
            result = module.find_isolated_native_child(
                home, "cliproxy-deepseek", module.CLIPROXY_ALIAS, "explorer", 9000, nonce
            )
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["child"]["id"], "child")

    def test_find_isolated_native_child_rejects_ambiguous_matches(self):
        nonce = "same-nonce"
        with tempfile.TemporaryDirectory() as temporary_dir:
            home = Path(temporary_dir)
            database = sqlite3.connect(home / "state_1.sqlite")
            database.execute(
                "CREATE TABLE threads (id TEXT, rollout_path TEXT, model_provider TEXT, model TEXT, "
                "reasoning_effort TEXT, agent_role TEXT, created_at INTEGER, created_at_ms INTEGER)"
            )
            for child_id in ("child-a", "child-b"):
                rollout = home / f"rollout-{child_id}.jsonl"
                rollout.write_text(nonce)
                database.execute(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (child_id, str(rollout), "cliproxy-deepseek", module.CLIPROXY_ALIAS, "medium", "explorer", 10, 10000),
                )
            database.commit()
            database.close()
            result = module.find_isolated_native_child(
                home, "cliproxy-deepseek", module.CLIPROXY_ALIAS, "explorer", 9000, nonce
            )
        self.assertEqual(result["candidate_count"], 2)
        self.assertIsNone(result["child"])

    def test_native_v1_result_uses_direct_wait_child_message(self):
        nonce = "abc123"
        contract = {
            "status": "success",
            "summary": f"native-v1-canary:{nonce}",
            "files_changed": [],
            "tests": ["received bounded native V1 task"],
            "risks": [],
        }
        events = [
            {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "spawn_agent", "status": "completed", "receiver_thread_ids": ["child"]}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(contract)}},
            {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait", "status": "completed", "agents_states": {"child": {"message": json.dumps(contract)}}}},
        ]
        result, child_id, spawned, waited, verified = module.native_v1_child_result("\n".join(map(json.dumps, events)), nonce)
        self.assertEqual(result, contract)
        self.assertEqual(child_id, "child")
        self.assertTrue(spawned)
        self.assertTrue(waited)
        self.assertTrue(verified)

    def test_native_v1_result_rejects_parent_only_contract(self):
        nonce = "abc123"
        contract = {"status": "success", "summary": f"native-v1-canary:{nonce}", "files_changed": [], "tests": [], "risks": []}
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(contract)}},
            {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait", "status": "completed", "agents_states": {}}},
        ]
        result, child_id, _spawned, waited, verified = module.native_v1_child_result("\n".join(map(json.dumps, events)), nonce)
        self.assertIsNone(result)
        self.assertIsNone(child_id)
        self.assertTrue(waited)
        self.assertFalse(verified)

    def test_native_v1_result_rejects_wrong_nonce(self):
        contract = {"status": "success", "summary": "native-v1-canary:wrong", "files_changed": [], "tests": [], "risks": []}
        event = {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait", "status": "completed", "agents_states": [{"message": contract}]}}
        result, child_id, _spawned, waited, verified = module.native_v1_child_result(json.dumps(event), "expected")
        self.assertEqual(result, contract)
        self.assertIsNone(child_id)
        self.assertTrue(waited)
        self.assertFalse(verified)

    def test_native_result_recovers_child_id_from_v2_wait_state(self):
        nonce = "v2nonce"
        contract = {"status": "success", "summary": f"native-v1-canary:{nonce}", "files_changed": [], "tests": [], "risks": []}
        event = {
            "type": "item.completed",
            "item": {
                "type": "collab_tool_call",
                "tool": "wait",
                "status": "completed",
                "agents_states": {"child-v2": {"message": contract}},
            },
        }
        result, child_id, spawned, waited, verified = module.native_v1_child_result(json.dumps(event), nonce)
        self.assertEqual(result, contract)
        self.assertEqual(child_id, "child-v2")
        self.assertTrue(spawned)
        self.assertTrue(waited)
        self.assertTrue(verified)

    def test_native_event_diagnostics_exposes_only_bounded_summary(self):
        events = [
            {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "spawn_agent"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "x" * 500}},
        ]
        diagnostics = module.native_event_diagnostics("\n".join(map(json.dumps, events)))
        self.assertEqual(diagnostics["collab_tools"], ["spawn_agent"])
        self.assertEqual(diagnostics["item_types"], ["agent_message", "collab_tool_call"])
        self.assertEqual(len(diagnostics["parent_message"]), 300)

    def test_bridge_completion_requires_expected_provider(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            log = Path(temporary_dir) / "bridge.log"
            log.write_text("completed backend=opencode-go\n")
            self.assertFalse(module.bridge_provider_completed(log, 0, "sensenova"))
            self.assertTrue(module.bridge_provider_completed(log, 0, "opencode-go"))

    def test_native_v1_command_forces_official_v1_without_ephemeral_mode(self):
        command = module.native_v1_command(Path("/desktop/codex"), Path("/tmp/catalog.json"), "gpt-parent", "prompt")
        self.assertNotIn("--ephemeral", command)
        self.assertIn('model_provider="openai-chatgpt"', command)
        self.assertIn("features.multi_agent=true", command)
        self.assertIn("features.multi_agent_v2=false", command)
        self.assertIn("features.remote_plugin=false", command)
        self.assertIn("features.plugins=false", command)
        self.assertEqual(command[0], "/desktop/codex")

    def test_native_v1_prompt_requires_actual_wait_agent_tool(self):
        prompt = module.native_v1_prompt("explorer", "child payload")
        self.assertIn("call wait_agent", prompt)
        self.assertIn("agent_type to explorer", prompt)
        self.assertIn("<canary-task>child payload</canary-task>", prompt)

    def test_invalid_result_uses_next_auto_provider(self):
        invalid = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Starting work."}}) + "\n"
        valid_response = '{"status":"success","summary":"done","files_changed":[],"tests":["passed"],"risks":[]}'
        valid = "\n".join(
            map(
                json.dumps,
                [
                    {"type": "item.completed", "item": {"type": "command_execution", "command": "pwd", "status": "completed"}},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": valid_response}},
                ],
            )
        )
        results = [module.subprocess.CompletedProcess([], 0, invalid, ""), module.subprocess.CompletedProcess([], 0, valid, "")]
        with tempfile.TemporaryDirectory() as workdir, tempfile.TemporaryDirectory() as circuit_dir:
            args = SimpleNamespace(
                task="inspect files",
                task_file=None,
                role="repository-exploration",
                workdir=workdir,
                provider="auto",
                sandbox="auto",
                timeout=90,
                no_record=True,
            )
            with patch.object(module, "invoke_codex", side_effect=results) as invoke, \
                 patch.object(module, "CIRCUIT_STATE_PATH", Path(circuit_dir) / "circuit.json"), \
                 patch.object(module, "CIRCUIT_LOCK_PATH", Path(circuit_dir) / "circuit.lock"):
                payload = module.run_worker(args)
        self.assertEqual(invoke.call_count, 2)
        fallback = module.RUNTIME_CONFIG["fallback"]["read_only"]
        self.assertIn(f"{fallback[1]}/deepseek-v4-flash", invoke.call_args_list[1].args[0])
        self.assertEqual(payload["provider"], fallback[1])
        self.assertIn("provider fallback was used after an earlier DeepSeek route failed", payload["risks"])

    def test_explicit_busy_provider_returns_fast_blocked_terminal(self):
        with tempfile.TemporaryDirectory() as workdir:
            args = SimpleNamespace(
                task="inspect files",
                task_file=None,
                role="repository-exploration",
                workdir=workdir,
                provider="sensenova",
                sandbox="read-only",
                timeout=90,
                no_record=True,
            )
            with patch.object(module, "provider_lease", side_effect=module.WorkerError("provider sensenova is already executing another Relay task")), \
                 patch.object(module, "invoke_codex") as invoke:
                payload = module.run_worker(args)
        invoke.assert_not_called()
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["files_changed"], [])
        self.assertIn("currently busy", payload["summary"])

    def test_length_finish_retries_same_provider_once_with_output_limit(self):
        truncated = "\n".join(map(json.dumps, [
            {"type": "tool_use", "part": {"state": {"status": "completed"}}},
            {"type": "text", "part": {"text": "partial result"}},
            {"type": "step_finish", "part": {"reason": "length"}},
        ]))
        response = '{"status":"success","summary":"done","files_changed":[],"tests":[],"risks":[]}'
        valid = "\n".join(map(json.dumps, [
            {"type": "tool_use", "part": {"state": {"status": "completed"}}},
            {"type": "text", "part": {"text": response}},
            {"type": "step_finish", "part": {"reason": "stop"}},
        ]))
        results = [
            module.subprocess.CompletedProcess([], 0, truncated, ""),
            module.subprocess.CompletedProcess([], 0, valid, ""),
        ]
        with tempfile.TemporaryDirectory() as workdir:
            args = SimpleNamespace(task="inspect", task_file=None, role="repository-exploration", workdir=workdir, provider="sensenova", sandbox="read-only", timeout=90, no_record=True)
            with patch.object(module, "invoke_codex", side_effect=results) as invoke:
                payload = module.run_worker(args)
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(invoke.call_args_list[0].args[0], invoke.call_args_list[1].args[0])
        self.assertEqual(invoke.call_args_list[0].kwargs["env"]["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"], "131072")
        self.assertEqual(payload["status"], "success")
        self.assertIn("bounded same-provider stream retry was used", payload["risks"])
        self.assertEqual(payload["stream_finish_reason"], "stop")
        self.assertEqual(payload["stream_retry_count"], 1)

    def test_no_text_stream_recovers_contract_in_same_session_without_replaying_task(self):
        initial = "\n".join(map(json.dumps, [
            {"type": "tool_use", "sessionID": "session_abcdefgh", "part": {"state": {"status": "completed"}}},
            {"type": "step_finish", "sessionID": "session_abcdefgh", "part": {"reason": "stop", "tokens": {"input": 10, "output": 3}}},
        ]))
        response = '{"status":"success","summary":"done","files_changed":[],"tests":[],"risks":[]}'
        recovery = "\n".join(map(json.dumps, [
            {"type": "text", "sessionID": "session_abcdefgh", "part": {"text": response}},
            {"type": "step_finish", "sessionID": "session_abcdefgh", "part": {"reason": "stop", "tokens": {"input": 2, "output": 1}}},
        ]))
        results = [
            module.subprocess.CompletedProcess([], 0, initial, ""),
            module.subprocess.CompletedProcess([], 0, recovery, ""),
        ]
        with tempfile.TemporaryDirectory() as workdir:
            args = SimpleNamespace(task="inspect", task_file=None, role="repository-exploration", workdir=workdir, provider="sensenova", sandbox="read-only", timeout=90, no_record=True)
            with patch.object(module, "invoke_codex", side_effect=results) as invoke:
                payload = module.run_worker(args)
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(invoke.call_args_list[1].args[0][invoke.call_args_list[1].args[0].index("--session") + 1], "session_abcdefgh")
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["finalization_recovery_count"], 1)
        self.assertEqual(payload["usage"], {"input_tokens": 12, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 4, "reasoning_output_tokens": 0})
        self.assertEqual(payload["accepted_usage"], {"input_tokens": 2, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0})

    def test_finalization_change_stays_partial_and_never_falls_back(self):
        initial = "\n".join(map(json.dumps, [
            {"type": "tool_use", "sessionID": "session_abcdefgh", "part": {"state": {"status": "completed"}}},
            {"type": "step_finish", "sessionID": "session_abcdefgh", "part": {"reason": "stop"}},
        ]))
        recovery = "\n".join(map(json.dumps, [
            {"type": "tool_use", "sessionID": "session_abcdefgh", "part": {"state": {"status": "completed"}}},
            {"type": "step_finish", "sessionID": "session_abcdefgh", "part": {"reason": "stop"}},
        ]))
        snapshots = [{}, {}, {"README.md": "changed"}, {"README.md": "changed"}]
        with tempfile.TemporaryDirectory() as workdir:
            args = SimpleNamespace(task="edit README", task_file=None, role="documentation", workdir=workdir, provider="auto", sandbox="auto", timeout=90, no_record=True)
            with patch.object(module, "workspace_snapshot", side_effect=snapshots), patch.object(module, "invoke_codex", side_effect=[module.subprocess.CompletedProcess([], 0, initial, ""), module.subprocess.CompletedProcess([], 0, recovery, "")]) as invoke:
                payload = module.run_worker(args)
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["files_changed"], ["README.md"])
        self.assertEqual(payload["finalization_recovery_count"], 1)

    def test_finalization_tool_use_is_not_accepted_as_a_second_execution(self):
        initial = "\n".join(map(json.dumps, [
            {"type": "tool_use", "sessionID": "session_abcdefgh", "part": {"state": {"status": "completed"}}},
            {"type": "step_finish", "sessionID": "session_abcdefgh", "part": {"reason": "stop"}},
        ]))
        response = '{"status":"success","summary":"done","files_changed":[],"tests":[],"risks":[]}'
        recovery = "\n".join(map(json.dumps, [
            {"type": "tool_use", "sessionID": "session_abcdefgh", "part": {"state": {"status": "completed"}}},
            {"type": "text", "sessionID": "session_abcdefgh", "part": {"text": response}},
            {"type": "step_finish", "sessionID": "session_abcdefgh", "part": {"reason": "stop"}},
        ]))
        with tempfile.TemporaryDirectory() as workdir:
            args = SimpleNamespace(task="inspect", task_file=None, role="repository-exploration", workdir=workdir, provider="sensenova", sandbox="read-only", timeout=90, no_record=True)
            with patch.object(module, "invoke_codex", side_effect=[module.subprocess.CompletedProcess([], 0, initial, ""), module.subprocess.CompletedProcess([], 0, recovery, "")]):
                payload = module.run_worker(args)
        self.assertEqual(payload["status"], "error")
        self.assertIn("finalization_used_tools", payload["attempt_failure_categories"])

    def test_stream_failure_with_workspace_change_does_not_retry(self):
        truncated = "\n".join(map(json.dumps, [
            {"type": "tool_use", "part": {"state": {"status": "completed"}}},
            {"type": "text", "part": {"text": "partial result"}},
            {"type": "step_finish", "part": {"reason": "length"}},
        ]))
        completed = module.subprocess.CompletedProcess([], 0, truncated, "")
        snapshots = [
            {"README.md": "before"},
            {"README.md": "after"},
            {"README.md": "after"},
        ]
        with tempfile.TemporaryDirectory() as workdir:
            args = SimpleNamespace(task="edit README", task_file=None, role="documentation", workdir=workdir, provider="auto", sandbox="auto", timeout=90, no_record=True)
            with patch.object(module, "workspace_snapshot", side_effect=snapshots), patch.object(module, "invoke_codex", return_value=completed) as invoke:
                payload = module.run_worker(args)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(payload["status"], "partial")
        self.assertIn("finish reason length", payload["risks"][1])

    def test_usage_is_aggregated_across_provider_fallback(self):
        invalid_events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Starting work."}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
        ]
        valid_response = '{"status":"success","summary":"done","files_changed":[],"tests":[],"risks":[]}'
        valid_events = [
            {"type": "item.completed", "item": {"type": "command_execution", "command": "pwd", "status": "completed"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": valid_response}},
            {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 3}},
        ]
        results = [
            module.subprocess.CompletedProcess([], 0, "\n".join(map(json.dumps, invalid_events)), ""),
            module.subprocess.CompletedProcess([], 0, "\n".join(map(json.dumps, valid_events)), ""),
        ]
        with tempfile.TemporaryDirectory() as workdir:
            args = SimpleNamespace(task="inspect", task_file=None, role="repository-exploration", workdir=workdir, provider="auto", sandbox="auto", timeout=90, no_record=True)
            with patch.object(module, "invoke_codex", side_effect=results):
                payload = module.run_worker(args)
        self.assertEqual(payload["usage"], {"input_tokens": 30, "output_tokens": 5})
        self.assertEqual(payload["accepted_usage"], {"input_tokens": 20, "output_tokens": 3})

    def test_partial_contract_usage_is_not_counted_as_accepted_success_usage(self):
        response = '{"status":"partial","summary":"needs review","files_changed":[],"tests":[],"risks":["incomplete"]}'
        events = [
            {"type": "item.completed", "item": {"type": "command_execution", "command": "pwd", "status": "completed"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": response}},
            {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 3}},
        ]
        completed = module.subprocess.CompletedProcess([], 0, "\n".join(map(json.dumps, events)), "")
        with tempfile.TemporaryDirectory() as workdir:
            args = SimpleNamespace(task="inspect", task_file=None, role="repository-exploration", workdir=workdir, provider="sensenova", sandbox="auto", timeout=90, no_record=True)
            with patch.object(module, "invoke_codex", return_value=completed):
                payload = module.run_worker(args)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["usage"], {"input_tokens": 20, "output_tokens": 3})
        self.assertEqual(payload["accepted_usage"], {})

    def test_valid_json_without_task_activity_is_not_accepted(self):
        response = '{"status":"success","summary":"guessed","files_changed":[],"tests":[],"risks":[]}'
        no_tools = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": response}}) + "\n"
        completed = module.subprocess.CompletedProcess([], 0, no_tools, "")
        with tempfile.TemporaryDirectory() as workdir:
            args = SimpleNamespace(task="inspect", task_file=None, role="repository-exploration", workdir=workdir, provider="auto", sandbox="auto", timeout=90, no_record=True)
            with patch.object(module, "invoke_codex", return_value=completed) as invoke:
                payload = module.run_worker(args)
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(payload["status"], "error")
        self.assertTrue(all("no task tool activity" in risk for risk in payload["risks"][1:]))

    def test_invalid_write_result_stops_fallback_after_workspace_change(self):
        invalid = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "I changed README but cannot format the result."}}) + "\n"
        completed = module.subprocess.CompletedProcess([], 0, invalid, "")
        snapshots = [
            {"README.md": "before"},
            {"README.md": "after"},
            {"README.md": "after"},
        ]
        with tempfile.TemporaryDirectory() as workdir:
            args = SimpleNamespace(task="edit README", task_file=None, role="documentation", workdir=workdir, provider="auto", sandbox="auto", timeout=90, no_record=True)
            with patch.object(module, "workspace_snapshot", side_effect=snapshots), patch.object(module, "invoke_codex", return_value=completed) as invoke:
                payload = module.run_worker(args)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["files_changed"], ["README.md"])
        self.assertIn("provider fallback stopped after workspace changes to avoid duplicate writes", payload["risks"])


if __name__ == "__main__":
    unittest.main()
