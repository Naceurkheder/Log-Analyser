"""Versioned request/response schemas for the control API.

These models are the platform boundary. They deliberately contain *references*
to datasets, never raw log lines: large inputs travel through object storage.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")

MAX_RULES = 64
MAX_PATTERN_BYTES = 256
MAX_WINDOW_SECONDS = 3600
MAX_BASELINE_WINDOWS = 1440
MAX_LINE_BYTES = 1_048_576

BackendPolicy = Literal["gpu", "cpu", "auto"]
RunStatus = Literal[
    "PENDING",
    "QUEUED",
    "CHUNKING",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "INGEST_FAILED",
]

_RUN_REQUEST_EXAMPLE = {
    "schema_version": 1,
    "dataset": {
        "bucket": "incoming",
        "key": "datasets/2026-11-20/auth.log",
        "size_bytes": 1048576,
        "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "encoding": "utf-8",
        "format": "text",
    },
    "detector": {
        "rules": [
            {
                "rule_id": "auth-failed-password",
                "pattern": "Failed password",
                "severity": "high",
            },
            {
                "rule_id": "http-5xx",
                "pattern": "HTTP/1.1\" 5",
                "severity": "medium",
            },
        ],
        "window_seconds": 60,
        "baseline_windows": 10,
        "z_threshold": 3.0,
        "max_line_bytes": 65536,
    },
    "backend_policy": "gpu",
}


class DatasetRef(BaseModel):
    """Immutable reference to a source object in the ``incoming`` bucket."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [_RUN_REQUEST_EXAMPLE["dataset"]]},
    )

    bucket: str = Field(default="incoming", min_length=1, max_length=63)
    key: str = Field(min_length=1, max_length=1024)
    size_bytes: int | None = Field(default=None, ge=1)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    encoding: Literal["utf-8", "utf-8-sig", "latin-1"] = "utf-8"
    format: Literal["text"] = "text"

    @field_validator("key")
    @classmethod
    def _object_key(cls, value: str) -> str:
        if value.startswith("/"):
            raise ValueError("must be a relative object key")
        if "\\" in value:
            raise ValueError("must use '/' as the object key separator")
        if any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("must not contain empty, '.' or '..' segments")
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("must not contain control characters")
        return value


class DetectorRule(BaseModel):
    """One stable pattern rule; ``rule_id`` is part of the run contract."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    pattern: str = Field(min_length=1)
    severity: Literal["low", "medium", "high", "critical"] = "medium"

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("must be a single-line pattern")
        if len(value.encode("utf-8")) > MAX_PATTERN_BYTES:
            raise ValueError(f"must be at most {MAX_PATTERN_BYTES} UTF-8 bytes")
        return value


class DetectorConfig(BaseModel):
    """Bounded detector configuration hashed into every job id."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [_RUN_REQUEST_EXAMPLE["detector"]]},
    )

    rules: list[DetectorRule] = Field(min_length=1, max_length=MAX_RULES)
    window_seconds: int = Field(default=60, ge=1, le=MAX_WINDOW_SECONDS)
    baseline_windows: int = Field(default=10, ge=1, le=MAX_BASELINE_WINDOWS)
    z_threshold: float = Field(default=3.0, gt=0, le=1000)
    max_line_bytes: int = Field(default=65536, ge=256, le=MAX_LINE_BYTES)

    @model_validator(mode="after")
    def _unique_rule_ids(self) -> "DetectorConfig":
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule_id values must be unique")
        return self


class RunRequest(BaseModel):
    """Control request for a new run (raw log contents are rejected)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [_RUN_REQUEST_EXAMPLE]},
    )

    schema_version: Literal[1] = SCHEMA_VERSION
    dataset: DatasetRef
    detector: DetectorConfig
    backend_policy: BackendPolicy = "auto"


class Links(BaseModel):
    run: str
    jobs: str
    alerts: str


class RunCreatedResponse(BaseModel):
    run_id: str
    schema_version: int
    status: RunStatus
    created: bool = Field(
        description="False when an existing run was returned for the same idempotency key."
    )
    created_at: str
    links: Links


class Progress(BaseModel):
    jobs_total: int = 0
    jobs_completed: int = 0
    jobs_failed: int = 0
    alerts_total: int = 0


class RunStatusResponse(BaseModel):
    run_id: str
    schema_version: int
    status: RunStatus
    backend_policy: BackendPolicy
    created_at: str
    updated_at: str
    dataset: DatasetRef
    detector: DetectorConfig
    detector_config_hash: str
    progress: Progress
    links: Links


class JobSummary(BaseModel):
    """Pagination-safe job projection (attempts are summarised, not embedded)."""

    model_config = ConfigDict(extra="allow")

    job_id: str
    run_id: str
    status: str = "PENDING"
    chunk_id: str | None = None
    backend: str | None = None
    attempts: int = 0
    created_at: str | None = None
    updated_at: str | None = None


class AlertSummary(BaseModel):
    """Alert projection; the full detector schema is owned by Sprint 4."""

    model_config = ConfigDict(extra="allow")

    alert_id: str
    run_id: str
    rule_id: str | None = None
    chunk_id: str | None = None
    line_id: str | None = None
    timestamp: str | None = None
    score: float | None = None
    created_at: str | None = None


class JobPage(BaseModel):
    items: list[JobSummary]
    count: int
    next_cursor: str | None = None


class AlertPage(BaseModel):
    items: list[AlertSummary]
    count: int
    next_cursor: str | None = None


class ErrorInfo(BaseModel):
    code: str
    message: str
    details: object | None = None
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorInfo


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, str]
