#!/usr/bin/env python3
"""Convert two explicit live soak reports into one compatibility evidence record."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_compatibility.py"
spec = importlib.util.spec_from_file_location("validate_compatibility", VALIDATOR)
if spec is None or spec.loader is None:
    raise RuntimeError("unable to load compatibility validator")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)
ManifestError = validator.ManifestError
validate_manifest = validator.validate_manifest


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read report {path}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"report {path} must contain an object")
    return value


def report_success_rate(report: dict[str, Any], label: str) -> float:
    runs = report.get("runs")
    successes = report.get("successes")
    if isinstance(runs, bool) or not isinstance(runs, int) or runs <= 0:
        raise ManifestError(f"{label}.runs must be a positive integer")
    if isinstance(successes, bool) or not isinstance(successes, int) or not 0 <= successes <= runs:
        raise ManifestError(f"{label}.successes must be an integer between zero and runs")
    return successes / runs


def validate_report(report: dict[str, Any], *, label: str, route: str, required_runs: int, minimum_rate: float, write: bool) -> float:
    if report.get("provider") != route:
        raise ManifestError(f"{label}.provider must be explicit route {route}")
    rate = report_success_rate(report, label)
    if report["runs"] < required_runs:
        raise ManifestError(f"{label}.runs is below {required_runs}")
    if rate < minimum_rate:
        raise ManifestError(f"{label} success rate is below {minimum_rate:.2f}")
    if report.get("workspace_verification_failures", 0) != 0:
        raise ManifestError(f"{label} has workspace verification failures")
    if write:
        if report.get("partial_write_runs", 0) != 0:
            raise ManifestError(f"{label} has partial writes")
        if report.get("duplicate_write_incidents") != 0:
            raise ManifestError(f"{label} does not prove zero duplicate writes")
        if report.get("write_safety_violations") != 0:
            raise ManifestError(f"{label} has write safety violations")
    return rate


def build_record(route: str, read_rate: float, write_rate: float, read: dict[str, Any], write: dict[str, Any]) -> dict[str, Any]:
    return {
        "route": route,
        "read_jobs": read["runs"],
        "write_jobs": write["runs"],
        "read_success_rate": round(read_rate, 6),
        "write_success_rate": round(write_rate, 6),
        "duplicate_writes": write["duplicate_write_incidents"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--route", required=True, choices=("sensenova", "sensenova1"))
    parser.add_argument("--read-report", type=Path, required=True)
    parser.add_argument("--write-report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    manifest = load_object(args.manifest)
    validate_manifest(manifest)
    qualification = manifest["qualification"]
    read = load_object(args.read_report)
    write = load_object(args.write_report)
    read_rate = validate_report(read, label="read report", route=args.route, required_runs=qualification["sequential_read_jobs"], minimum_rate=qualification["min_success_rate"], write=False)
    write_rate = validate_report(write, label="write report", route=args.route, required_runs=qualification["sequential_write_jobs"], minimum_rate=qualification["min_success_rate"], write=True)
    output = copy.deepcopy(manifest)
    records = [record for record in output["evidence"]["records"] if record["route"] != args.route]
    records.append(build_record(args.route, read_rate, write_rate, read, write))
    output["evidence"]["records"] = sorted(records, key=lambda item: item["route"])
    validate_manifest(output, require_evidence=len(output["evidence"]["records"]) == len(output["production_routes"]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "success", "route": args.route, "out": str(args.out)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
