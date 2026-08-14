"""Pure declarative Relay routing and timeout policy.

This module deliberately has no subprocess, filesystem, Provider credential, or job-store access.
It lets the CLI, MCP facade, and future runtime hosts share one reviewed routing policy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import tomllib


class RuntimeConfigError(ValueError):
    """Raised when a declarative Relay policy is malformed or unsupported."""


def load_runtime_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeConfigError(f"invalid relay configuration: {type(exc).__name__}") from exc
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise RuntimeConfigError("unsupported relay configuration schema")
    providers = config.get("providers")
    roles = config.get("roles")
    timeouts = config.get("timeouts")
    fallback = config.get("fallback")
    if not all(isinstance(value, dict) for value in (providers, roles, timeouts, fallback)):
        raise RuntimeConfigError("relay configuration has missing sections")
    normalized_providers: dict[str, dict[str, Any]] = {}
    for name, metadata in providers.items():
        if not isinstance(name, str) or not isinstance(metadata, dict):
            raise RuntimeConfigError("relay configuration has invalid provider")
        if metadata.get("adapter") != "opencode" or not isinstance(metadata.get("profile"), str) or not isinstance(metadata.get("model"), str):
            raise RuntimeConfigError("relay configuration has invalid provider adapter")
        try:
            port = int(metadata.get("port"))
        except (TypeError, ValueError) as exc:
            raise RuntimeConfigError("relay configuration has invalid provider port") from exc
        if not 1 <= port <= 65535:
            raise RuntimeConfigError("relay configuration has invalid provider port")
        normalized_providers[name] = {**metadata, "port": port}
    normalized_roles: dict[str, set[str]] = {}
    for name in ("read_only", "worker", "sol", "terra"):
        values = roles.get(name)
        if not isinstance(values, list) or not values or not all(isinstance(value, str) and value for value in values):
            raise RuntimeConfigError("relay configuration has invalid role policy")
        normalized_roles[name] = set(values)
    normalized_fallback: dict[str, tuple[str, ...]] = {}
    for name in ("read_only", "worker", "general"):
        values = fallback.get(name)
        if not isinstance(values, list) or not values or any(value not in normalized_providers for value in values):
            raise RuntimeConfigError("relay configuration has invalid fallback policy")
        normalized_fallback[name] = tuple(values)
    normalized_timeouts: dict[str, int] = {}
    for name in ("read_only_seconds", "write_seconds", "general_seconds"):
        try:
            value = int(timeouts.get(name))
        except (TypeError, ValueError) as exc:
            raise RuntimeConfigError("relay configuration has invalid timeout") from exc
        if value < 30:
            raise RuntimeConfigError("relay configuration timeout must be at least 30 seconds")
        normalized_timeouts[name] = value
    return {"providers": normalized_providers, "roles": normalized_roles, "fallback": normalized_fallback, "timeouts": normalized_timeouts}


def normalize_role(
    role: str,
    task: str,
    sol_roles: set[str],
    terra_roles: set[str],
    read_only_roles: set[str],
    worker_roles: set[str],
) -> str:
    value = role.strip().lower().replace("_", "-")
    if value != "auto":
        return value
    lowered = task.lower()
    keyword_groups = [
        (sol_roles, ("architecture", "architect", "规划", "架构", "最终 review", "final review")),
        (terra_roles, ("second opinion", "validation", "review", "复核", "验证", "审查")),
        (read_only_roles, ("grep", "search", "explore", "日志分析", "代码搜索", "仓库探索")),
        (worker_roles, ("implement", "test", "debug", "refactor", "document", "实现", "测试", "调试", "重构", "文档")),
    ]
    for roles, keywords in keyword_groups:
        if any(word in lowered for word in keywords):
            return sorted(roles)[0]
    return "implementation"


def resolve_route(
    role: str,
    task: str,
    runtime_config: dict[str, Any],
) -> dict[str, Any]:
    roles = runtime_config["roles"]
    providers = runtime_config["providers"]
    normalized = normalize_role(role, task, roles["sol"], roles["terra"], roles["read_only"], roles["worker"])
    if normalized in roles["sol"]:
        return {"target": "sol", "role": normalized, "reason": "architecture_or_final_decision"}
    if normalized in roles["terra"]:
        return {"target": "terra", "role": normalized, "reason": "review_or_validation"}
    policy = "read_only" if normalized in roles["read_only"] else "worker" if normalized in roles["worker"] else "general"
    provider = runtime_config["fallback"][policy][0]
    return {
        "target": "deepseek",
        "role": normalized,
        "provider": provider,
        "profile": providers[provider]["profile"],
        "sandbox": "read-only" if policy == "read_only" else "workspace-write",
        "reason": "read_heavy_execution" if policy == "read_only" else "execution_task" if policy == "worker" else "general_execution",
    }


def provider_order(role: str, selected_provider: str, automatic: bool, runtime_config: dict[str, Any]) -> list[str]:
    if not automatic:
        return [selected_provider]
    roles = runtime_config["roles"]
    normalized = normalize_role(role, "", roles["sol"], roles["terra"], roles["read_only"], roles["worker"])
    policy = "read_only" if normalized in roles["read_only"] else "worker" if normalized in roles["worker"] else "general"
    preferred = runtime_config["fallback"][policy]
    return [selected_provider, *(name for name in preferred if name != selected_provider)]


def timeout_split(total_timeout: int, attempts: int) -> list[int]:
    total = max(30, int(total_timeout))
    if attempts <= 1:
        return [total]
    if total < 60:
        base, remainder = divmod(total, attempts)
        return [base + (1 if index < remainder else 0) for index in range(attempts)]
    reserve = min(45, max(20, total // 4))
    first = total - reserve
    fallback, remainder = divmod(reserve, attempts - 1)
    return [first, *(fallback + (1 if index < remainder else 0) for index in range(attempts - 1))]


def default_timeout(requested_timeout: int, role: str, runtime_config: dict[str, Any]) -> int:
    if requested_timeout > 0:
        return max(30, int(requested_timeout))
    roles = runtime_config["roles"]
    normalized = normalize_role(role, "", roles["sol"], roles["terra"], roles["read_only"], roles["worker"])
    timeouts = runtime_config["timeouts"]
    if normalized in roles["read_only"]:
        return timeouts["read_only_seconds"]
    if normalized in roles["worker"]:
        return timeouts["write_seconds"]
    return timeouts["general_seconds"]
