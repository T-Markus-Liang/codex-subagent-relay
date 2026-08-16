"""Privacy-preserving Relay run telemetry construction and append-only storage."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


VALID_SCOPES = {"production", "diagnostic", "qualification"}


def run_record(payload: dict[str, Any], relay_version: str, run_type: str, telemetry_scope: str, timestamp: str) -> dict[str, Any]:
    """Build the bounded aggregate record; task bodies and raw output are intentionally absent."""
    scope = telemetry_scope if telemetry_scope in VALID_SCOPES else "diagnostic"
    risks = payload.get("risks") or []
    return {
        "timestamp": timestamp,
        "relay_version": relay_version,
        "run_type": run_type,
        "telemetry_scope": scope,
        "status": payload.get("status"),
        "provider": payload.get("provider") or payload.get("expected_provider") or "unknown",
        "requested_provider": payload.get("requested_provider") or "unknown",
        "role": payload.get("role") or payload.get("agent_type") or "unknown",
        "duration_seconds": payload.get("duration_seconds"),
        "stream_finish_reason": payload.get("stream_finish_reason"),
        "stream_retry_count": payload.get("stream_retry_count", 0),
        "finalization_recovery_count": payload.get("finalization_recovery_count", 0),
        "fallback_used": bool(payload.get("fallback_attempted")) or "provider fallback was used after an earlier DeepSeek route failed" in risks,
        "partial_write": payload.get("status") == "partial" and bool(payload.get("files_changed")),
        "attempt_failure_categories": list(payload.get("attempt_failure_categories") or []),
        "usage": payload.get("usage", {}),
        "accepted_usage": payload.get("accepted_usage", {}),
    }


def append_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode())
    finally:
        os.close(fd)
