#!/usr/bin/env python3
"""Render a source-local seven-day Relay operations report without task content."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


DEFAULT_LOG = Path.home() / ".codex" / "deepseek-worker-runs.jsonl"
USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 2)


def usage_totals(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    totals = {key: 0 for key in USAGE_KEYS}
    for row in rows:
        usage = row.get(field)
        if not isinstance(usage, dict):
            continue
        for key in USAGE_KEYS:
            value = usage.get(key, 0)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] += value
    totals["request_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    totals["context_tokens"] = totals["request_tokens"] + totals["cached_input_tokens"] + totals["cache_write_input_tokens"]
    return totals


def read_rows(path: Path, cutoff: datetime) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    invalid = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows, invalid
    for line in lines:
        try:
            row = json.loads(line)
            timestamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            invalid += 1
            continue
        if not isinstance(row, dict) or timestamp.tzinfo is None:
            invalid += 1
            continue
        if timestamp >= cutoff:
            rows.append({**row, "_timestamp": timestamp.astimezone(UTC)})
    return rows, invalid


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses: dict[str, int] = defaultdict(int)
    attempt_failure_categories: dict[str, int] = defaultdict(int)
    durations: list[float] = []
    success = fallback = partial = retried = finalization_recovered = blocked = accepted_usage_rows = 0
    for row in rows:
        status = str(row.get("status") or "unknown")
        statuses[status] += 1
        success += int(status == "success")
        blocked += int(status == "blocked")
        fallback += int(bool(row.get("fallback_used")))
        partial += int(bool(row.get("partial_write")))
        retried += int(int(row.get("stream_retry_count") or 0) > 0)
        finalization_recovered += int(int(row.get("finalization_recovery_count") or 0) > 0)
        accepted_usage_rows += int("accepted_usage" in row)
        categories = row.get("attempt_failure_categories")
        if isinstance(categories, list):
            for category in categories:
                if isinstance(category, str) and category:
                    attempt_failure_categories[category] += 1
        try:
            durations.append(float(row.get("duration_seconds") or 0))
        except (TypeError, ValueError):
            pass
    return {
        "runs": len(rows),
        "successes": success,
        "success_rate_percent": round(100 * success / len(rows), 2) if rows else 0.0,
        "status_counts": dict(sorted(statuses.items())),
        "p50_duration_seconds": percentile(durations, 0.5),
        "p95_duration_seconds": percentile(durations, 0.95),
        "fallback_runs": fallback,
        "partial_write_runs": partial,
        "stream_retry_runs": retried,
        "finalization_recovery_runs": finalization_recovered,
        "blocked_runs": blocked,
        "attempt_failure_category_counts": dict(sorted(attempt_failure_categories.items())),
        "usage": {
            "attempt_usage": usage_totals(rows, "usage"),
            "accepted_success_usage": usage_totals(rows, "accepted_usage"),
            "accepted_usage_coverage": {
                "rows_with_field": accepted_usage_rows,
                "rows_missing_field": len(rows) - accepted_usage_rows,
                "note": "accepted_success_usage counts only final Provider attempts with status success; it excludes failed, partial, and retried attempts.",
            },
        },
    }


def report(
    log_path: Path,
    days: float,
    now: datetime | None = None,
    relay_version: str | None = None,
    run_type: str | None = None,
    telemetry_scope: str | None = None,
    since: datetime | None = None,
) -> dict[str, Any]:
    if not 0 < days <= 365:
        raise ValueError("days must be between 0 and 365")
    now = now or datetime.now(UTC)
    if since is not None and since > now:
        raise ValueError("since must not be later than the report end time")
    cutoff = max(now - timedelta(days=days), since) if since else now - timedelta(days=days)
    rows, invalid = read_rows(log_path, cutoff)
    source_rows = len(rows)
    if relay_version:
        rows = [row for row in rows if row.get("relay_version") == relay_version]
    if run_type:
        rows = [row for row in rows if row.get("run_type", "external_run") == run_type]
    if telemetry_scope:
        rows = [row for row in rows if row.get("telemetry_scope") == telemetry_scope]
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_run_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_day[row["_timestamp"].date().isoformat()].append(row)
        by_provider[str(row.get("provider") or "unknown")].append(row)
        by_run_type[str(row.get("run_type") or "external_run")].append(row)
    return {
        "status": "success",
        "source": "relay_operational_telemetry",
        "utc_window": {"start": cutoff.isoformat(), "end": now.isoformat(), "days": days},
        "log_present": log_path.is_file(),
        "invalid_records_skipped": invalid,
        "filters": {
            "relay_version": relay_version,
            "run_type": run_type,
            "telemetry_scope": telemetry_scope,
            "since": since.isoformat() if since else None,
        },
        "source_rows_in_window": source_rows,
        "rows_excluded_by_filters": source_rows - len(rows),
        "overall": summarize(rows),
        "daily": {key: summarize(value) for key, value in sorted(by_day.items())},
        "providers": {key: summarize(value) for key, value in sorted(by_provider.items())},
        "run_types": {key: summarize(value) for key, value in sorted(by_run_type.items())},
        "coverage_warning": (
            "Relay telemetry is operational evidence only. attempt_usage includes failed and retried Provider attempts; accepted_success_usage is Relay-local productive usage only when its field is present. Do not add either to Codex Rollout or CC Switch ledgers; use the separate codex-usage-audit workflow for those sources."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--relay-version", help="Include only telemetry recorded by this Relay version.")
    parser.add_argument("--run-type", help="Include only one run type, for example external_run.")
    parser.add_argument("--telemetry-scope", choices=("production", "diagnostic", "qualification"), help="Include only one operational scope.")
    parser.add_argument("--since", help="UTC ISO-8601 lower bound for a clean observation window.")
    args = parser.parse_args()
    try:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00")).astimezone(UTC) if args.since else None
        payload = report(args.log.expanduser(), args.days, relay_version=args.relay_version, run_type=args.run_type, telemetry_scope=args.telemetry_scope, since=since)
    except (TypeError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, separators=(",", ":")))
        return 2
    rendered = json.dumps(payload, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
