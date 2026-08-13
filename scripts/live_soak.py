#!/usr/bin/env python3
"""Run opt-in sequential DeepSeek Worker soak tests in disposable directories."""

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live", action="store_true", help="Required: permits live Provider calls that consume quota.")
    parser.add_argument("--worker", default="deepseek-worker", help="Worker command to invoke.")
    parser.add_argument("--provider", choices=("sensenova", "sensenova1", "opencode-go"), required=True)
    parser.add_argument("--role", choices=("repository-exploration", "documentation"), required=True)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def task_for(role: str) -> str:
    if role == "repository-exploration":
        return "Use an ordinary task tool to inspect this disposable directory. Do not create, modify, or delete files. Return only the required five-field JSON."
    return "Create exactly one file named soak.txt containing exactly: deepseek-worker soak. Read it back to verify. Do not modify any other file. Return only the required five-field JSON."


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 2)


def main() -> int:
    args = parse_args()
    if not args.confirm_live:
        raise SystemExit("refusing live calls without --confirm-live")
    if args.runs < 1 or args.runs > 1000:
        raise SystemExit("--runs must be between 1 and 1000")
    if not shutil.which(args.worker) and not Path(args.worker).is_file():
        raise SystemExit("worker command not found")

    records = []
    for _ in range(args.runs):
        with tempfile.TemporaryDirectory(prefix="deepseek-worker-soak-") as temporary_dir:
            command = [
                args.worker,
                "--json",
                "run",
                "--role",
                args.role,
                "--workdir",
                temporary_dir,
                "--provider",
                args.provider,
                "--timeout",
                str(args.timeout),
                "--no-record",
                "--task",
                task_for(args.role),
            ]
            started = time.monotonic()
            completed = subprocess.run(command, text=True, capture_output=True, check=False)
            duration = round(time.monotonic() - started, 2)
            try:
                payload = json.loads(completed.stdout)
            except json.JSONDecodeError:
                payload = {"status": "invalid-output"}
            records.append(
                {
                    "status": payload.get("status", "invalid-output"),
                    "duration_seconds": duration,
                    "provider": payload.get("provider"),
                    "stream_finish_reason": payload.get("stream_finish_reason"),
                    "stream_retry_count": payload.get("stream_retry_count", 0),
                    "fallback_used": "provider fallback was used after an earlier DeepSeek route failed" in (payload.get("risks") or []),
                    "partial_write": payload.get("status") == "partial" and bool(payload.get("files_changed")),
                }
            )

    durations = [record["duration_seconds"] for record in records]
    successes = sum(record["status"] == "success" for record in records)
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "worker": args.worker,
        "provider": args.provider,
        "role": args.role,
        "runs": args.runs,
        "successes": successes,
        "success_rate_percent": round(100 * successes / args.runs, 2),
        "p50_duration_seconds": percentile(durations, 0.5),
        "p95_duration_seconds": percentile(durations, 0.95),
        "status_counts": {status: sum(record["status"] == status for record in records) for status in sorted({record["status"] for record in records})},
        "stream_finish_counts": {reason: sum(record["stream_finish_reason"] == reason for record in records) for reason in sorted({record["stream_finish_reason"] for record in records}, key=str)},
        "fallback_runs": sum(record["fallback_used"] for record in records),
        "partial_write_runs": sum(record["partial_write"] for record in records),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, separators=(",", ":")))
    return 0 if successes == args.runs else 1


if __name__ == "__main__":
    raise SystemExit(main())
