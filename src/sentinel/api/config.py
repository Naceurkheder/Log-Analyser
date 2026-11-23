"""Runtime configuration for the control API.

Every value comes from the environment so the same image runs against the
local moto plane (`.env.example`) and against a real AWS account without code
changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_ENDPOINT = "http://10.20.0.10:4566"
DEFAULT_MAX_DATASET_BYTES = 5 * 1024**3  # 5 GiB


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    """Immutable API settings snapshot."""

    aws_endpoint_url: str | None
    aws_region: str
    aws_access_key_id: str | None
    aws_secret_access_key: str | None

    runs_table: str
    jobs_table: str
    alerts_table: str
    incoming_bucket: str
    ingest_queue_url: str | None
    ingest_queue_name: str

    max_dataset_bytes: int
    verify_dataset_object: bool
    log_level: str
    api_url: str
    api_host: str
    api_port: int

    @classmethod
    def from_env(cls) -> "Settings":
        endpoint = os.environ.get("AWS_ENDPOINT_URL", DEFAULT_ENDPOINT)
        return cls(
            aws_endpoint_url=endpoint or None,
            aws_region=os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1",
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID") or "test",
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY") or "test",
            runs_table=os.environ.get("SENTINEL_RUNS_TABLE", "SentinelRuns"),
            jobs_table=os.environ.get("SENTINEL_JOBS_TABLE", "SentinelJobs"),
            alerts_table=os.environ.get("SENTINEL_ALERTS_TABLE", "SentinelAlerts"),
            incoming_bucket=os.environ.get("SENTINEL_INCOMING_BUCKET", "incoming"),
            ingest_queue_url=os.environ.get("SENTINEL_INGEST_QUEUE_URL") or None,
            ingest_queue_name=os.environ.get(
                "SENTINEL_INGEST_QUEUE", "ingest-queue"
            ),
            max_dataset_bytes=_env_int(
                "SENTINEL_MAX_DATASET_BYTES", DEFAULT_MAX_DATASET_BYTES
            ),
            verify_dataset_object=_env_bool(
                "SENTINEL_VERIFY_DATASET_OBJECT", True
            ),
            log_level=os.environ.get("SENTINEL_LOG_LEVEL", "INFO"),
            api_url=os.environ.get("SENTINEL_API_URL", "http://127.0.0.1:8000"),
            api_host=os.environ.get("SENTINEL_API_HOST", "127.0.0.1"),
            api_port=_env_int("SENTINEL_API_PORT", 8000),
        )
