#!/usr/bin/env python3
"""Validate the Relay compatibility manifest and optional live qualification evidence."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "compatibility" / "manifest.json"
PRODUCTION_ROUTES = {
    "sensenova": "sensenova/deepseek-v4-flash",
    "sensenova1": "sensenova1/deepseek-v4-flash",
}
TOP_LEVEL_KEYS = {
    "relay_version",
    "minimum_python",
    "adapter",
    "production_routes",
    "native",
    "qualification",
    "evidence",
}
EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {"type": "string"},
        "read_jobs": {"type": "integer"},
        "write_jobs": {"type": "integer"},
        "read_success_rate": {"type": "number"},
        "write_success_rate": {"type": "number"},
        "duplicate_writes": {"type": "integer"},
    },
    "required": [
        "route",
        "read_jobs",
        "write_jobs",
        "read_success_rate",
        "write_success_rate",
        "duplicate_writes",
    ],
}


class ManifestError(ValueError):
    pass


def require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise ManifestError(f"{label} keys mismatch: missing={missing}, extra={extra}")


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{label} must be a non-empty string")
    return value


def require_integer(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ManifestError(f"{label} must be an integer >= {minimum}")
    return value


def require_number(value: Any, label: str, minimum: float = 0.0, maximum: float = 1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
        raise ManifestError(f"{label} must be a number between {minimum} and {maximum}")
    return float(value)


def validate_record(record: Any, qualification: dict[str, Any], routes: set[str]) -> None:
    if not isinstance(record, dict):
        raise ManifestError("evidence record must be an object")
    require_exact_keys(record, set(EVIDENCE_SCHEMA["required"]), "evidence record")
    route = require_string(record["route"], "evidence.route")
    if route not in routes:
        raise ManifestError(f"evidence route is not a production route: {route}")
    read_jobs = require_integer(record["read_jobs"], "evidence.read_jobs")
    write_jobs = require_integer(record["write_jobs"], "evidence.write_jobs")
    if read_jobs < qualification["sequential_read_jobs"] or write_jobs < qualification["sequential_write_jobs"]:
        raise ManifestError("evidence job counts are below the publication qualification")
    if require_number(record["read_success_rate"], "evidence.read_success_rate") < qualification["min_success_rate"]:
        raise ManifestError("evidence read success rate is below the publication qualification")
    if require_number(record["write_success_rate"], "evidence.write_success_rate") < qualification["min_success_rate"]:
        raise ManifestError("evidence write success rate is below the publication qualification")
    if require_integer(record["duplicate_writes"], "evidence.duplicate_writes") != 0:
        raise ManifestError("evidence reports duplicate writes")


def validate_manifest(payload: Any, require_evidence: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ManifestError("manifest must be an object")
    require_exact_keys(payload, TOP_LEVEL_KEYS, "manifest")
    relay_version = require_string(payload["relay_version"], "relay_version")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", relay_version):
        raise ManifestError("relay_version must be semver-like")
    minimum_python = require_string(payload["minimum_python"], "minimum_python")
    match = re.fullmatch(r"(\d+)\.(\d+)", minimum_python)
    if not match or tuple(map(int, match.groups())) < (3, 11):
        raise ManifestError("minimum_python must be at least 3.11")
    if payload["adapter"] != "opencode":
        raise ManifestError("only the reviewed opencode adapter is supported")

    routes_value = payload["production_routes"]
    if not isinstance(routes_value, list) or len(routes_value) != len(PRODUCTION_ROUTES):
        raise ManifestError("production_routes must contain exactly the reviewed routes")
    routes: set[str] = set()
    for item in routes_value:
        if not isinstance(item, dict):
            raise ManifestError("production route must be an object")
        require_exact_keys(item, {"provider", "model"}, "production route")
        provider = require_string(item["provider"], "route.provider")
        model = require_string(item["model"], "route.model")
        if PRODUCTION_ROUTES.get(provider) != model or provider in routes:
            raise ManifestError("production route provider/model is unsupported or duplicated")
        routes.add(provider)
    if routes != set(PRODUCTION_ROUTES):
        raise ManifestError("production route set is incomplete")

    native = payload["native"]
    if not isinstance(native, dict):
        raise ManifestError("native must be an object")
    require_exact_keys(native, {"status"}, "native")
    if native["status"] != "canary_only":
        raise ManifestError("native promotion is not permitted by this manifest")

    qualification = payload["qualification"]
    if not isinstance(qualification, dict):
        raise ManifestError("qualification must be an object")
    require_exact_keys(
        qualification,
        {"sequential_read_jobs", "sequential_write_jobs", "min_success_rate", "zero_duplicate_writes"},
        "qualification",
    )
    if require_integer(qualification["sequential_read_jobs"], "qualification.sequential_read_jobs") != 100:
        raise ManifestError("sequential_read_jobs must be 100")
    if require_integer(qualification["sequential_write_jobs"], "qualification.sequential_write_jobs") != 100:
        raise ManifestError("sequential_write_jobs must be 100")
    if require_number(qualification["min_success_rate"], "qualification.min_success_rate") < 0.95:
        raise ManifestError("min_success_rate must be at least 0.95")
    if qualification["zero_duplicate_writes"] is not True:
        raise ManifestError("zero_duplicate_writes must be true")

    evidence = payload["evidence"]
    if not isinstance(evidence, dict):
        raise ManifestError("evidence must be an object")
    require_exact_keys(evidence, {"schema_version", "schema", "records"}, "evidence")
    if evidence["schema_version"] != 1 or evidence["schema"] != EVIDENCE_SCHEMA:
        raise ManifestError("unsupported evidence schema")
    records = evidence["records"]
    if not isinstance(records, list):
        raise ManifestError("evidence.records must be a list")
    for record in records:
        validate_record(record, qualification, routes)
    if require_evidence:
        record_routes = [record["route"] for record in records]
        if len(record_routes) != len(set(record_routes)) or set(record_routes) != routes:
            raise ManifestError("publication evidence must contain every production route")
    return {
        "status": "success",
        "manifest": str(DEFAULT_MANIFEST),
        "routes": sorted(routes),
        "records": len(records),
        "evidence_required": require_evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--require-evidence", action="store_true")
    args = parser.parse_args()
    try:
        payload = json.loads(args.manifest.read_text(encoding="utf-8"))
        result = validate_manifest(payload, args.require_evidence)
    except (OSError, json.JSONDecodeError, ManifestError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, separators=(",", ":")))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
