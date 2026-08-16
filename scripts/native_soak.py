#!/usr/bin/env python3
"""Run opt-in sequential native Agent Team tool-canary research without retaining task secrets."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


V1_EVIDENCE_FIELDS = (
    "spawned",
    "waited",
    "child_result_valid",
    "nonce_verified",
    "task_payload_present",
    "hidden_outputs_verified",
    "parallel_tool_calls_observed",
    "rollout_final_result_valid",
    "provider_completed",
    "child_metadata_valid",
    "state_db_isolated",
    "config_unchanged",
    "state_db_unchanged",
)
V2_EVIDENCE_FIELDS = V1_EVIDENCE_FIELDS + ("proxy_completed",)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live", action="store_true", help="Required: permits native canary calls that consume quota.")
    parser.add_argument("--worker", default="deepseek-worker", help="Worker command to invoke.")
    parser.add_argument("--backend", choices=("v1-tool", "cliproxy-v2"), required=True)
    parser.add_argument("--provider", choices=("sensenova", "sensenova1", "opencode-go"), default="sensenova")
    parser.add_argument("--parent-model", default="gpt-5.6-sol")
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=180, help="Native canary timeout passed to the Worker.")
    parser.add_argument("--deadline", type=float, default=240.0, help="Outer wall-clock bound for one canary process.")
    parser.add_argument("--max-consecutive-failures", type=int, default=3, help="Stop a diagnostic batch after this many non-success results.")
    parser.add_argument("--min-success-rate", type=float, default=0.95)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def error_categories(errors: Any) -> list[str]:
    """Reduce bounded canary errors to fixed labels; never persist raw output or tokens."""
    categories: set[str] = set()
    for error in errors if isinstance(errors, list) else []:
        text = str(error).lower()
        if "unknown model" in text:
            categories.add("unknown_model")
        elif "spawn" in text:
            categories.add("spawn")
        elif "wait" in text:
            categories.add("wait")
        elif "direct child result" in text:
            categories.add("direct_child_result")
        elif "task nonce" in text or "task payload" in text:
            categories.add("task_payload")
        elif "parallel" in text:
            categories.add("parallel_tool_calls")
        elif "shell tool" in text or "tool exchange" in text:
            categories.add("tool_activity")
        elif "hidden" in text or "token" in text:
            categories.add("hidden_output")
        elif "rollout" in text or "final result" in text:
            categories.add("rollout_contract")
        elif "bridge" in text or "proxy" in text:
            categories.add("bridge_completion")
        elif "metadata" in text:
            categories.add("child_metadata")
        elif "state db" in text:
            categories.add("isolated_state")
        elif "config changed" in text:
            categories.add("config_mutated")
        elif text:
            categories.add("process_or_other")
    return sorted(categories)


def evidence_fields(backend: str) -> tuple[str, ...]:
    return V1_EVIDENCE_FIELDS if backend == "v1-tool" else V2_EVIDENCE_FIELDS


def strict_success(payload: dict[str, Any], backend: str) -> bool:
    return payload.get("status") == "success" and all(payload.get(field) is True for field in evidence_fields(backend))


def run_once(args: argparse.Namespace) -> tuple[dict[str, Any], float]:
    command = [args.worker, "--json"]
    if args.backend == "v1-tool":
        command.extend(("native-v1-tool-canary", "--provider", args.provider, "--parent-model", args.parent_model))
    else:
        command.extend(("cliproxy-native-canary", "--parent-model", args.parent_model))
    command.extend(("--workdir", "{workdir}", "--timeout", str(args.timeout)))
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="deepseek-native-soak-") as temporary_dir:
        rendered = [temporary_dir if part == "{workdir}" else part for part in command]
        try:
            completed = subprocess.run(rendered, text=True, capture_output=True, timeout=args.deadline, check=False)
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "errors": ["outer deadline reached"]}, round(time.monotonic() - started, 2)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {"status": "invalid-output", "errors": ["worker did not emit JSON"]}
    return payload if isinstance(payload, dict) else {"status": "invalid-output", "errors": ["worker JSON was not an object"]}, round(time.monotonic() - started, 2)


def record(index: int, payload: dict[str, Any], backend: str, duration: float) -> dict[str, Any]:
    fields = evidence_fields(backend)
    return {
        "run": index,
        "status": str(payload.get("status") or "invalid-output"),
        "strict_success": strict_success(payload, backend),
        "duration_seconds": duration,
        "evidence": {field: payload.get(field) is True for field in fields},
        "error_categories": error_categories(payload.get("errors")),
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))], 2)


def report(args: argparse.Namespace, rows: list[dict[str, Any]], early_stop_reason: str | None) -> dict[str, Any]:
    successes = sum(row["strict_success"] for row in rows)
    category_counts = {
        category: sum(category in row["error_categories"] for row in rows)
        for category in sorted({category for row in rows for category in row["error_categories"]})
    }
    evidence_failures = {
        field: sum(not row["evidence"][field] for row in rows)
        for field in evidence_fields(args.backend)
    }
    rate = successes / len(rows) if rows else 0.0
    promotion_eligible = (
        len(rows) >= 100
        and rate >= args.min_success_rate
        and not evidence_failures["config_unchanged"]
        and not evidence_failures["state_db_unchanged"]
    )
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "backend": args.backend,
        "provider": args.provider if args.backend == "v1-tool" else None,
        "parent_model": args.parent_model,
        "runs": len(rows),
        "requested_runs": args.runs,
        "strict_successes": successes,
        "strict_success_rate_percent": round(rate * 100, 2),
        "p50_duration_seconds": percentile([row["duration_seconds"] for row in rows], 0.5),
        "p95_duration_seconds": percentile([row["duration_seconds"] for row in rows], 0.95),
        "status_counts": {status: sum(row["status"] == status for row in rows) for status in sorted({row["status"] for row in rows})},
        "evidence_failure_counts": evidence_failures,
        "error_category_counts": category_counts,
        "early_stop_reason": early_stop_reason,
        "promotion_eligible": promotion_eligible,
        "promotion_notice": "This report is research evidence only. It never changes native routing, the compatibility manifest, or the Codex root provider.",
        "records": rows,
    }


def main() -> int:
    args = parse_args()
    if not args.confirm_live:
        raise SystemExit("refusing native live calls without --confirm-live")
    if not 1 <= args.runs <= 1000:
        raise SystemExit("--runs must be between 1 and 1000")
    if args.timeout <= 0 or args.deadline <= args.timeout or args.max_consecutive_failures < 1:
        raise SystemExit("timeout must be positive, deadline must exceed timeout, and max-consecutive-failures must be positive")
    if not 0 < args.min_success_rate <= 1:
        raise SystemExit("--min-success-rate must be between 0 and 1")
    rows: list[dict[str, Any]] = []
    early_stop_reason = None
    for index in range(1, args.runs + 1):
        payload, duration = run_once(args)
        rows.append(record(index, payload, args.backend, duration))
        if len(rows) >= args.max_consecutive_failures and all(not row["strict_success"] for row in rows[-args.max_consecutive_failures:]):
            early_stop_reason = f"{args.max_consecutive_failures} consecutive non-success attempts"
            break
    payload = report(args, rows, early_stop_reason)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if payload["promotion_eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
