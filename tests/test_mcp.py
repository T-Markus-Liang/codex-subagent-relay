import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SERVER = Path(__file__).resolve().parents[1] / "plugins" / "codex-subagent-relay" / "mcp" / "server.py"
spec = importlib.util.spec_from_file_location("relay_mcp", SERVER)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


class RelayMcpTests(unittest.TestCase):
    def test_initialize_and_list_expose_only_declared_tools(self):
        initialized = module.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        tools = module.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "codex-subagent-relay")
        self.assertEqual(
            [item["name"] for item in tools["result"]["tools"]],
            ["relay_doctor", "relay_launch", "relay_status", "relay_cancel", "relay_stats"],
        )

    def test_launch_validates_scope_before_invoking_worker(self):
        with patch.object(module, "run_worker") as worker:
            result = module.call_tool("relay_launch", {"role": "implementation", "workdir": "relative", "task": "x"})
        self.assertTrue(result["isError"])
        worker.assert_not_called()

    def test_launch_forwards_bounded_arguments_to_worker(self):
        with tempfile.TemporaryDirectory() as workdir:
            with patch.object(module, "run_worker", return_value={"status": "running", "job_id": "1"}) as worker:
                result = module.call_tool(
                    "relay_launch",
                    {"role": "documentation", "workdir": workdir, "task": "write docs", "timeout_seconds": 120, "idempotency_key": "opaque"},
                )
        self.assertNotIn("isError", result)
        forwarded = worker.call_args.args[0]
        self.assertEqual(forwarded[:3], ["launch", "--role", "documentation"])
        self.assertIn("--idempotency-key", forwarded)

    def test_tool_result_is_json_text_not_raw_stderr(self):
        with patch.object(module, "run_worker", return_value={"status": "healthy"}):
            result = module.call_tool("relay_doctor", {})
        self.assertEqual(json.loads(result["content"][0]["text"]), {"status": "healthy"})

    def test_cached_plugin_resolves_the_installed_launcher(self):
        with tempfile.TemporaryDirectory() as temporary_home, tempfile.TemporaryDirectory() as temporary_cache:
            home = Path(temporary_home)
            launcher = home / ".local/bin/deepseek-worker"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("#!/bin/sh\n", encoding="utf-8")
            launcher.chmod(0o755)
            cached_server = Path(temporary_cache) / "marketplace/plugin/0.10.30/mcp/server.py"
            cached_server.parent.mkdir(parents=True)
            cached_server.write_text("# cache placeholder\n", encoding="utf-8")
            with patch("pathlib.Path.home", return_value=home):
                self.assertEqual(module.resolve_worker(cached_server), launcher)

    def test_installed_launcher_is_executed_directly(self):
        with tempfile.TemporaryDirectory() as workdir:
            launcher = Path(workdir) / "deepseek-worker"
            launcher.write_text("#!/bin/sh\n", encoding="utf-8")
            launcher.chmod(0o755)
            completed = __import__("subprocess").CompletedProcess([], 0, '{"status":"healthy"}', "")
            with patch.object(module, "WORKER", launcher), patch("subprocess.run", return_value=completed) as run:
                self.assertEqual(module.run_worker(["doctor"]), {"status": "healthy"})
        self.assertEqual(run.call_args.args[0], [str(launcher), "--json", "doctor"])


if __name__ == "__main__":
    unittest.main()
