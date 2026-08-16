"""Reviewed OpenCode command builder for Relay Provider adapters."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class AdapterConfigError(ValueError):
    """Raised when a declared Provider cannot use the reviewed OpenCode adapter."""


READ_ONLY_AGENT = "relay-readonly"
WORKSPACE_WRITE_AGENT = "relay-workspace-write"


def agent_for_sandbox(sandbox: str) -> str:
    return READ_ONLY_AGENT if sandbox == "read-only" else WORKSPACE_WRITE_AGENT


def relay_agent_config() -> dict[str, Any]:
    """Return the process-local OpenCode overlay used by bounded Relay work."""
    return {
        "subagent_depth": 0,
        "agent": {
            READ_ONLY_AGENT: {
                "description": "Bounded Relay read-only executor",
                "mode": "primary",
                "prompt": (
                    "You are a bounded Relay read-only execution worker. Work only inside the assigned "
                    "directory. Do not delegate, ask questions, use network tools, or modify files."
                ),
                "permission": {
                    "*": "deny",
                    "read": "allow",
                    "glob": "allow",
                    "grep": "allow",
                    "lsp": "allow",
                },
            },
            WORKSPACE_WRITE_AGENT: {
                "description": "Bounded Relay workspace-write executor",
                "mode": "primary",
                "prompt": (
                    "You are a bounded Relay workspace-write execution worker. Work only inside the assigned "
                    "directory. Do not delegate, ask questions, or use network tools."
                ),
                "permission": {
                    "*": "deny",
                    "read": "allow",
                    "glob": "allow",
                    "grep": "allow",
                    "lsp": "allow",
                    "edit": "allow",
                    "bash": "allow",
                },
            },
        },
    }


def _merge_mapping(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_mapping(merged[key], value)
        else:
            merged[key] = value
    return merged


def execution_environment(base: dict[str, str] | None, sandbox: str) -> dict[str, str]:
    """Keep Provider settings while overriding only Relay's selected Agent definitions."""
    environment = dict(os.environ if base is None else base)
    existing: dict[str, Any] = {}
    raw_content = environment.get("OPENCODE_CONFIG_CONTENT")
    if raw_content:
        try:
            decoded = json.loads(raw_content)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            existing = decoded
    overlay = relay_agent_config()
    environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(_merge_mapping(existing, overlay), separators=(",", ":"))
    environment["OPENCODE_DISABLE_PROJECT_CONFIG"] = "true"
    environment["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] = "131072"
    return environment


def task_command(
    executable: Path,
    metadata: dict[str, Any] | None,
    workdir: Path,
    role: str,
    sandbox: str,
    prompt: str,
) -> list[str]:
    if metadata is None:
        raise AdapterConfigError("unknown provider")
    if metadata.get("adapter") != "opencode":
        raise AdapterConfigError(f"unsupported provider adapter: {metadata.get('adapter')}")
    model = metadata.get("model")
    if not isinstance(model, str) or not model:
        raise AdapterConfigError("provider adapter has no model")
    return [
        str(executable),
        "run",
        "--pure",
        "--dir",
        str(workdir),
        "--agent",
        agent_for_sandbox(sandbox),
        "--model",
        model,
        "--format",
        "json",
        "--title",
        f"deepseek-worker:{role}",
        prompt,
    ]


def finalization_command(executable: Path, workdir: Path, session_id: str, sandbox: str, prompt: str) -> list[str]:
    """Continue one original session solely to recover its final strict contract."""
    return [
        str(executable),
        "run",
        "--pure",
        "--dir",
        str(workdir),
        "--session",
        session_id,
        "--agent",
        agent_for_sandbox(sandbox),
        "--format",
        "json",
        prompt,
    ]
