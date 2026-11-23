"""Validation and construction of the small, versioned job queue message.

Queue bodies contain references and metadata only. The bytes of a chunk and its
manifest are always fetched from S3 after the message has been validated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import Capability, WorkerSettings
from .errors import DetectorConfigError, MessageError

JOB_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^run_[0-9a-f]{32}$")
_JOB_ID = re.compile(r"^job-[0-9a-f]{20}$")
_CHUNK_ID = re.compile(r"^chk-[0-9a-f]{16}$")


@dataclass(frozen=True)
class JobMessage:
    """A validated job message plus a reference to the original body."""

    schema_version: int
    message_type: str
    run_id: str
    job_id: str
    chunk_id: str
    backend: str
    chunk: dict[str, Any]
    manifest: dict[str, Any]
    detector: dict[str, Any]
    execution: dict[str, Any]
    replay: dict[str, Any] | None
    raw: dict[str, Any]

    @classmethod
    def parse(cls, raw: Any, capability: Capability) -> "JobMessage":
        if not isinstance(raw, dict):
            raise MessageError("job message is not an object")
        version = raw.get("schema_version")
        if version != JOB_SCHEMA_VERSION:
            raise MessageError(
                "unsupported job message schema version",
                code="unsupported_schema_version",
                details={"schema_version": version},
            )
        message_type = raw.get("message_type")
        if message_type != "job":
            raise MessageError(
                "expected a job message",
                code="invalid_message_type",
                details={"message_type": message_type},
            )

        run_id = cls._text(raw, "run_id")
        job_id = cls._text(raw, "job_id")
        chunk_id = cls._text(raw, "chunk_id")
        if not _RUN_ID.fullmatch(run_id):
            raise MessageError("invalid run_id", details={"run_id": run_id})
        if not _JOB_ID.fullmatch(job_id):
            raise MessageError("invalid job_id", details={"job_id": job_id})
        if not _CHUNK_ID.fullmatch(chunk_id):
            raise MessageError("invalid chunk_id", details={"chunk_id": chunk_id})

        backend = raw.get("backend")
        if backend not in {"cpu", "gpu"}:
            raise MessageError(
                "invalid backend", details={"backend": backend}
            )
        if backend != capability:
            raise MessageError(
                f"{capability} worker cannot execute a {backend} job",
                code="backend_capability_mismatch",
                details={"backend": backend, "capability": capability},
            )
        execution = raw.get("execution")
        if not isinstance(execution, dict):
            raise MessageError("execution block is missing")
        if execution.get("backend") != backend:
            raise MessageError(
                "execution backend does not match the message backend",
                code="backend_capability_mismatch",
                details={
                    "backend": backend,
                    "execution_backend": execution.get("backend"),
                },
            )
        artifact_id = cls._text(execution, "artifact_id", parent="execution")
        if not artifact_id:
            raise MessageError("execution.artifact_id is empty")

        chunk = cls._object(raw, "chunk")
        for name in ("bucket", "key"):
            if not cls._text(chunk, name, parent="chunk"):
                raise MessageError(f"chunk.{name} is required")
        cls._digest(chunk.get("sha256"), "chunk.sha256")
        cls._positive_int(chunk.get("size_bytes"), "chunk.size_bytes")
        cls._nonnegative_int(chunk.get("line_count"), "chunk.line_count")
        cls._positive_int(chunk.get("line_number_start"), "chunk.line_number_start")
        cls._nonnegative_int(chunk.get("line_number_end"), "chunk.line_number_end")
        if int(chunk["line_number_end"]) < int(chunk["line_number_start"]):
            raise MessageError("chunk line range is inverted")

        manifest = cls._object(raw, "manifest")
        for name in ("bucket", "key"):
            if not cls._text(manifest, name, parent="manifest"):
                raise MessageError(f"manifest.{name} is required")
        cls._digest(manifest.get("sha256"), "manifest.sha256")

        detector = cls._object(raw, "detector")
        cls._digest(detector.get("config_hash"), "detector.config_hash")
        if not cls._text(detector, "parser_version", parent="detector"):
            raise MessageError("detector.parser_version is required")

        replay = raw.get("replay")
        if replay is not None and not isinstance(replay, dict):
            raise MessageError("replay metadata must be an object")

        return cls(
            schema_version=int(version),
            message_type=str(message_type),
            run_id=run_id,
            job_id=job_id,
            chunk_id=chunk_id,
            backend=backend,  # type: ignore[arg-type]
            chunk=chunk,
            manifest=manifest,
            detector=detector,
            execution=execution,
            replay=replay,
            raw=raw,
        )

    @staticmethod
    def _object(raw: dict[str, Any], name: str) -> dict[str, Any]:
        value = raw.get(name)
        if not isinstance(value, dict):
            raise MessageError(f"{name} block is missing or not an object")
        return value

    @staticmethod
    def _text(value: dict[str, Any], name: str, *, parent: str = "") -> str:
        result = value.get(name)
        if not isinstance(result, str) or not result:
            label = f"{parent}.{name}" if parent else name
            raise MessageError(f"{label} is required")
        return result

    @staticmethod
    def _digest(value: Any, label: str) -> str:
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise MessageError(f"{label} must be a lowercase sha256 hex digest")
        return value

    @staticmethod
    def _positive_int(value: Any, label: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise MessageError(f"{label} must be a positive integer")
        return value

    @staticmethod
    def _nonnegative_int(value: Any, label: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise MessageError(f"{label} must be a non-negative integer")
        return value

    def error_context(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "chunk_id": self.chunk_id,
            "backend": self.backend,
        }


def validate_authoritative(
    message: JobMessage, *, job: dict[str, Any], run: dict[str, Any] | None
) -> None:
    """Cross-check the message against the durable job/run records."""
    actual = {
        "run_id": job.get("run_id"),
        "chunk_id": job.get("chunk_id"),
        "backend": job.get("backend"),
        "detector_config_hash": job.get("detector_config_hash"),
        "artifact_id": job.get("artifact_id"),
    }
    expected = {
        "run_id": message.run_id,
        "chunk_id": message.chunk_id,
        "backend": message.backend,
        "detector_config_hash": message.detector["config_hash"],
        "artifact_id": message.execution["artifact_id"],
    }
    if actual != expected:
        raise MessageError(
            "job message does not match the authoritative job record",
            details={"expected": expected, "actual": actual},
        )
    if run is None:
        raise MessageError(
            "run record is missing",
            details={"run_id": message.run_id},
        )
    if run.get("detector_config_hash") != message.detector["config_hash"]:
        raise DetectorConfigError(
            "detector config hash does not match the run record",
            details={
                "message": message.detector["config_hash"],
                "run": run.get("detector_config_hash"),
            },
        )


def job_message_from_record(
    job: dict[str, Any], settings: WorkerSettings
) -> dict[str, Any]:
    """Rebuild the exact logical message for an unpublished job (outbox)."""
    backend = str(job["backend"])
    return {
        "schema_version": JOB_SCHEMA_VERSION,
        "message_type": "job",
        "run_id": job["run_id"],
        "job_id": job["job_id"],
        "chunk_id": job["chunk_id"],
        "backend": backend,
        "chunk": job["chunk"],
        "manifest": job["manifest"],
        "detector": {
            "config_hash": job["detector_config_hash"],
            "parser_version": job.get("parser_version")
            or settings.api.producer_parser_version,
        },
        "execution": {
            "backend": backend,
            "artifact_id": job["artifact_id"],
        },
    }


def ingest_message_from_run(run: dict[str, Any]) -> dict[str, Any]:
    """Rebuild an ingest handoff message for an unpublished run."""
    return {
        "schema_version": run.get("schema_version", 1),
        "message_type": "ingest",
        "run_id": run["run_id"],
        "request_hash": run["request_hash"],
        "detector_config_hash": run["detector_config_hash"],
        "backend_policy": run["backend_policy"],
        "dataset": run["dataset"],
        "detector": run["detector"],
    }
