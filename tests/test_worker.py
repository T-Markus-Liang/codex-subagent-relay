import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import tempfile
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
            with patch.object(module, "JOB_ROOT", Path(jobs)), patch.object(module.secrets, "token_hex", return_value="abcdef123456"), patch.object(module.subprocess, "Popen", return_value=DummyProcess()) as popen:
                launched = module.launch_worker(args)
                self.assertEqual(launched["status"], "running")
                self.assertEqual(popen.call_args.kwargs["stdin"], module.subprocess.DEVNULL)
                job_id = launched["job_id"]
                metadata, stdout, _stderr = module.job_paths(job_id)
                self.assertTrue(metadata.is_file())
                stdout.write_text('{"status":"success","summary":"done","files_changed":[],"tests":[],"risks":[]}\n')
                completed = module.poll_worker(job_id)
                self.assertEqual(completed["status"], "success")
                self.assertEqual(completed["job_state"], "completed")

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
        self.assertEqual(result["provider"], "sensenova1")
        self.assertEqual(result["sandbox"], "read-only")

    def test_worker_route(self):
        result = module.route("implementation")
        self.assertEqual(result["provider"], "sensenova1")
        self.assertEqual(result["sandbox"], "workspace-write")

    def test_worker_prompt_contains_literal_result_contract(self):
        prompt = module.worker_prompt("inspect files", "repository-exploration")
        self.assertIn('{"status":"success","summary":"concise result","files_changed":[],"tests":[],"risks":[]}', prompt)
        self.assertIn("no Markdown fence or surrounding prose", prompt)
        self.assertIn('never use 0, null, or the string "none"', prompt)

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

    def test_general_route_uses_sensenova1(self):
        self.assertEqual(module.route("general-execution")["provider"], "sensenova1")

    def test_auto_provider_order_is_role_specific(self):
        self.assertEqual(
            module.build_provider_order("repository-exploration", "sensenova1", True),
            ["sensenova1", "sensenova"],
        )
        self.assertEqual(
            module.build_provider_order("implementation", "sensenova1", True),
            ["sensenova1", "sensenova"],
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
                json.dumps({"timestamp": base_time.isoformat(), "run_type": "external_run", "status": "success", "provider": "sensenova", "duration_seconds": 10, "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 3, "reasoning_output_tokens": 1}}),
                json.dumps({"timestamp": (base_time - module.timedelta(minutes=1)).isoformat(), "run_type": "native_v1_canary", "status": "partial", "provider": "sensenova1", "duration_seconds": 20, "usage": {"input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 4, "reasoning_output_tokens": 2}}),
                json.dumps({"timestamp": (base_time - module.timedelta(minutes=2)).isoformat(), "run_type": "external_run", "status": "error", "provider": "sensenova1", "duration_seconds": 30, "usage": {"input_tokens": 30, "cached_input_tokens": 6, "output_tokens": 5, "reasoning_output_tokens": 3}}),
            ]) + "\n")
            with patch.object(module, "RUN_LOG_PATH", log):
                payload = module.stats(2)
        self.assertEqual(payload["status_counts"], {"success": 1, "partial": 1, "error": 1})
        self.assertEqual(payload["usage"]["cached_input_tokens"], 15)
        self.assertEqual(payload["provider_stats"]["sensenova1"]["success_rate_percent"], 0.0)
        self.assertEqual(payload["by_run_type"]["external_run"]["runs"], 2)
        self.assertEqual(payload["by_run_type"]["native_v1_canary"]["runs"], 1)

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
        self.assertEqual(command[0], "/desktop/codex")

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
        self.assertIn("sensenova/deepseek-v4-flash", invoke.call_args_list[1].args[0])
        self.assertEqual(payload["provider"], "sensenova")
        self.assertIn("provider fallback was used after an earlier DeepSeek route failed", payload["risks"])

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
