"""Reviewed OpenCode command builder for Relay Provider adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class AdapterConfigError(ValueError):
    """Raised when a declared Provider cannot use the reviewed OpenCode adapter."""


def agent_for_sandbox(sandbox: str) -> str:
    return "plan" if sandbox == "read-only" else "build"


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
