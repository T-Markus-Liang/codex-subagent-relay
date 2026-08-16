"""Durable Relay job-artifact storage with conservative legacy handling."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable


class JobStore:
    """Own job paths and JSON artifacts without exposing task text to list views."""

    def __init__(self, root: Path, is_valid_job_id: Callable[[str], bool]) -> None:
        self.root = root
        self._is_valid_job_id = is_valid_job_id

    def paths(self, job_id: str) -> tuple[Path, Path, Path]:
        if not self._is_valid_job_id(job_id):
            raise ValueError("invalid worker job id")
        directory = self.root / job_id
        return directory / "meta.json", directory / "stdout.jsonl", directory / "stderr.log"

    def artifact_path(self, job_id: str, name: str) -> Path:
        metadata_path, _stdout_path, _stderr_path = self.paths(job_id)
        return metadata_path.parent / name

    @staticmethod
    def write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".worker-job-", delete=False) as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
            temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def read_modern_metadata(self, job_id: str) -> dict[str, Any]:
        metadata_path, _stdout_path, _stderr_path = self.paths(job_id)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"worker job not found: {type(exc).__name__}") from exc
        if not isinstance(metadata, dict) or metadata.get("job_id") != job_id:
            raise ValueError("invalid worker job metadata")
        return metadata

    def modern_metadata(self) -> tuple[list[dict[str, Any]], int]:
        """Return valid modern metadata and a count of malformed job directories."""
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return [], 0
        records: list[dict[str, Any]] = []
        invalid = 0
        for directory in entries:
            if not directory.is_dir() or directory.name in {"idempotency", "legacy"}:
                continue
            try:
                metadata = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                invalid += 1
                continue
            if isinstance(metadata, dict) and metadata.get("job_id") == directory.name:
                records.append(metadata)
            else:
                invalid += 1
        return records, invalid

    def legacy_metadata(self) -> tuple[list[dict[str, Any]], int]:
        """Read earlier flat artifacts as opaque historical records, never runnable jobs."""
        try:
            paths = list(self.root.glob("*.json"))
        except OSError:
            return [], 0
        records: list[dict[str, Any]] = []
        invalid = 0
        for path in paths:
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                invalid += 1
                continue
            job_id = metadata.get("job_id") if isinstance(metadata, dict) else None
            if isinstance(job_id, str) and self._is_valid_job_id(job_id) and path.name == f"{job_id}.json":
                records.append({"job_id": job_id, "path": path, "metadata": metadata})
            else:
                invalid += 1
        return records, invalid

    def archive_legacy(self, record: dict[str, Any]) -> None:
        """Move, never delete, a verified flat artifact into a non-active legacy area."""
        path = record["path"]
        if not isinstance(path, Path) or path.parent != self.root or path.suffix != ".json":
            raise ValueError("invalid legacy job artifact")
        destination = self.root / "legacy" / path.name
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError("legacy archive target already exists")
        os.replace(path, destination)
