#!/usr/bin/env python3
"""Minimal stdio MCP facade for the local Codex Subagent Relay runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]


def resolve_worker(server_path: Path = Path(__file__)) -> Path:
    """Find the installed Relay without assuming a plugin cache mirrors the repo."""
    installed = Path.home() / ".local" / "bin" / "deepseek-worker"
    if installed.is_file() and os.access(installed, os.X_OK):
        return installed
    source_candidate = server_path.resolve().parents[3] / "deepseek-worker"
    if source_candidate.is_file() and os.access(source_candidate, os.X_OK):
        return source_candidate
    return installed


WORKER = resolve_worker()
MAX_TASK_CHARS = 32_768
MAX_TIMEOUT_SECONDS = 900
ROLES = (
    "repository-exploration",
    "search",
    "logs",
    "implementation",
    "test",
    "debug",
    "refactor",
    "documentation",
)
PROVIDERS = ("auto", "sensenova", "sensenova1", "opencode-go")


def tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": schema}


TOOLS = [
    tool("relay_doctor", "Check local Relay prerequisites without exposing Provider credentials.", {"type": "object", "additionalProperties": False, "properties": {}}),
    tool(
        "relay_launch",
        "Launch one bounded DeepSeek execution job and return its durable job ID. It never changes Codex root Provider or conversation metadata.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["role", "workdir", "task"],
            "properties": {
                "role": {"type": "string", "enum": list(ROLES)},
                "workdir": {"type": "string", "description": "Absolute existing repository directory."},
                "task": {"type": "string", "description": "Bounded task contract with scope and acceptance criteria; never include credentials."},
                "provider": {"type": "string", "enum": list(PROVIDERS), "default": "auto"},
                "sandbox": {"type": "string", "enum": ["auto", "read-only", "workspace-write"], "default": "auto"},
                "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_TIMEOUT_SECONDS, "default": 0},
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 256},
            },
        },
    ),
    tool("relay_status", "Read the current or terminal state of a previously launched Relay job.", {"type": "object", "additionalProperties": False, "required": ["job_id"], "properties": {"job_id": {"type": "string"}}}),
    tool("relay_cancel", "Cancel one running Relay job. A job with workspace changes remains partial for human review and is never replayed.", {"type": "object", "additionalProperties": False, "required": ["job_id"], "properties": {"job_id": {"type": "string"}}}),
    tool("relay_stats", "Return local aggregate Relay success, latency, fallback, and token metrics without task bodies.", {"type": "object", "additionalProperties": False, "properties": {"hours": {"type": "number", "minimum": 0.01, "maximum": 8760, "default": 8}}}),
]


def error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def require_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("arguments must be an object")
    return value


def run_worker(arguments: list[str], timeout: int = 15) -> dict[str, Any]:
    try:
        if not WORKER.is_file() or not os.access(WORKER, os.X_OK):
            raise ValueError("Relay launcher is unavailable; run make install-local before enabling the plugin")
        completed = subprocess.run(
            [str(WORKER), "--json", *arguments],
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"Relay command unavailable: {type(exc).__name__}") from exc
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Relay returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Relay returned an invalid payload")
    return payload


def valid_workdir(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("workdir must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("workdir must be an existing absolute directory")
    return str(path.resolve())


def call_tool(name: str, raw_arguments: Any) -> dict[str, Any]:
    arguments = require_object(raw_arguments)
    try:
        if name == "relay_doctor":
            payload = run_worker(["doctor"])
        elif name == "relay_stats":
            hours = arguments.get("hours", 8)
            if not isinstance(hours, (int, float)) or isinstance(hours, bool) or not 0.01 <= hours <= 8760:
                raise ValueError("hours must be between 0.01 and 8760")
            payload = run_worker(["stats", "--hours", str(hours)])
        elif name in {"relay_status", "relay_cancel"}:
            job_id = arguments.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                raise ValueError("job_id is required")
            payload = run_worker([name.removeprefix("relay_"), "--job-id", job_id])
        elif name == "relay_launch":
            role = arguments.get("role")
            task = arguments.get("task")
            provider = arguments.get("provider", "auto")
            sandbox = arguments.get("sandbox", "auto")
            timeout = arguments.get("timeout_seconds", 0)
            if role not in ROLES or provider not in PROVIDERS or sandbox not in {"auto", "read-only", "workspace-write"}:
                raise ValueError("unsupported Relay launch option")
            if not isinstance(task, str) or not task.strip() or len(task) > MAX_TASK_CHARS:
                raise ValueError(f"task must contain 1-{MAX_TASK_CHARS} characters")
            if not isinstance(timeout, int) or isinstance(timeout, bool) or not 0 <= timeout <= MAX_TIMEOUT_SECONDS:
                raise ValueError(f"timeout_seconds must be between 0 and {MAX_TIMEOUT_SECONDS}")
            command = [
                "launch", "--role", role, "--workdir", valid_workdir(arguments.get("workdir")),
                "--provider", provider, "--sandbox", sandbox, "--timeout", str(timeout), "--task", task,
            ]
            key = arguments.get("idempotency_key")
            if key is not None:
                if not isinstance(key, str) or not 1 <= len(key.encode("utf-8")) <= 256:
                    raise ValueError("idempotency_key must contain 1-256 bytes")
                command.extend(["--idempotency-key", key])
            payload = run_worker(command)
        else:
            return error(f"Unknown Relay tool: {name}")
    except ValueError as exc:
        return error(str(exc))
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]}


def response(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    message_id = request.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return response(message_id, {"protocolVersion": "2024-11-05", "serverInfo": {"name": "codex-subagent-relay", "version": "0.10.11"}, "capabilities": {"tools": {}}})
    if method == "ping":
        return response(message_id, {})
    if method == "tools/list":
        return response(message_id, {"tools": TOOLS})
    if method == "tools/call":
        params = request.get("params") or {}
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return response(message_id, error("tools/call requires a tool name"))
        return response(message_id, call_tool(params["name"], params.get("arguments", {})))
    return response(message_id, error("Unsupported MCP method"))


def main() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            result = handle_request(request)
            if result is not None:
                print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
        except (json.JSONDecodeError, ValueError):
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
