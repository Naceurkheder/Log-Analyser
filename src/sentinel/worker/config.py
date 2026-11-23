"""Environment-driven worker settings.

The API :class:`~sentinel.api.config.Settings` is reused for resource names and
credentials; everything specific to worker leases, heartbeats, artifact
identity and reconciliation lives here. Values are validated at construction so
an unsafe visibility/heartbeat combination cannot reach production.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import Literal

from ..api.config import Settings
from ..detector.native import ABI_VERSION

Capability = Literal["cpu", "gpu"]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or not raw.strip() else int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or not raw.strip() else float(raw)


@dataclass(frozen=True)
class WorkerSettings:
    """Immutable snapshot of the worker process configuration."""

    capability: Capability
    api: Settings
    worker_id: str

    results_bucket: str
    visibility_timeout: int
    heartbeat_interval: int
    lease_margin_seconds: int
    message_wait_seconds: int
    max_messages: int
    max_transient_retries: int
    retry_base_seconds: float
    retry_max_seconds: float
    max_receive_count: int

    artifact_path: str | None
    artifact_sha256: str | None
    expected_abi_version: int

    metrics_host: str
    metrics_port: int

    reconcile_min_age_seconds: int
    reconcile_page_size: int
    reconcile_max_pages: int
    loop_poll_seconds: float
    paused_poll_seconds: float

    replay_reason_min_length: int
    replay_max_count: int
    dlq_inspect_limit: int

    @classmethod
    def from_env(cls, capability: str | None = None) -> "WorkerSettings":
        cap = capability or os.environ.get("SENTINEL_WORKER_BACKEND") or "cpu"
        if cap not in {"cpu", "gpu"}:
            raise ValueError("worker capability must be 'cpu' or 'gpu'")
        api = Settings.from_env()
        # The heartbeat must be able to renew several times before visibility
        # expires. The margin absorbs transient LocalStack/API latency.
        heartbeat = _env_int("SENTINEL_WORKER_HEARTBEAT_INTERVAL", 30)
        margin = _env_int("SENTINEL_WORKER_LEASE_MARGIN_SECONDS", 240)
        visibility = _env_int("SENTINEL_WORKER_VISIBILITY_TIMEOUT", 300)
        settings = cls(
            capability=cap,  # type: ignore[arg-type]
            api=api,
            worker_id=os.environ.get("SENTINEL_WORKER_ID")
            or f"{socket.gethostname()}-{os.getpid()}",
            results_bucket=os.environ.get("SENTINEL_RESULTS_BUCKET", "results"),
            visibility_timeout=visibility,
            heartbeat_interval=heartbeat,
            lease_margin_seconds=margin,
            message_wait_seconds=_env_int("SENTINEL_WORKER_WAIT_SECONDS", 20),
            max_messages=_env_int("SENTINEL_WORKER_MAX_MESSAGES", 1),
            max_transient_retries=_env_int("SENTINEL_WORKER_MAX_RETRIES", 3),
            retry_base_seconds=_env_float("SENTINEL_WORKER_RETRY_BASE_SECONDS", 0.5),
            retry_max_seconds=_env_float("SENTINEL_WORKER_RETRY_MAX_SECONDS", 30.0),
            max_receive_count=_env_int("SENTINEL_WORKER_MAX_RECEIVE_COUNT", 3),
            artifact_path=os.environ.get("SENTINEL_WORKER_ARTIFACT_PATH") or None,
            artifact_sha256=os.environ.get("SENTINEL_WORKER_ARTIFACT_SHA256")
            or None,
            expected_abi_version=_env_int(
                "SENTINEL_WORKER_EXPECTED_ABI_VERSION", ABI_VERSION
            ),
            metrics_host=os.environ.get("SENTINEL_WORKER_METRICS_HOST", "127.0.0.1"),
            metrics_port=_env_int(
                "SENTINEL_WORKER_METRICS_PORT", 9100 if cap == "cpu" else 9101
            ),
            reconcile_min_age_seconds=_env_int(
                "SENTINEL_WORKER_RECONCILE_MIN_AGE_SECONDS", 30
            ),
            reconcile_page_size=_env_int("SENTINEL_WORKER_RECONCILE_PAGE_SIZE", 100),
            reconcile_max_pages=_env_int("SENTINEL_WORKER_RECONCILE_MAX_PAGES", 10),
            loop_poll_seconds=_env_float("SENTINEL_WORKER_LOOP_POLL_SECONDS", 0.5),
            paused_poll_seconds=_env_float("SENTINEL_WORKER_PAUSED_POLL_SECONDS", 5.0),
            replay_reason_min_length=_env_int(
                "SENTINEL_WORKER_REPLAY_REASON_MIN_LENGTH", 4
            ),
            replay_max_count=_env_int("SENTINEL_WORKER_REPLAY_MAX_COUNT", 1),
            dlq_inspect_limit=_env_int("SENTINEL_WORKER_DLQ_INSPECT_LIMIT", 50),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.capability not in {"cpu", "gpu"}:
            raise ValueError("capability must be 'cpu' or 'gpu'")
        if self.heartbeat_interval < 1:
            raise ValueError("heartbeat interval must be positive")
        if self.lease_margin_seconds < 0:
            raise ValueError("lease margin must not be negative")
        minimum = 2 * self.heartbeat_interval + self.lease_margin_seconds
        if self.visibility_timeout < minimum:
            raise ValueError(
                "visibility_timeout must be at least "
                f"2*heartbeat_interval + lease_margin_seconds ({minimum}s)"
            )
        if not 1 <= self.max_messages <= 10:
            raise ValueError("max_messages must be in [1, 10]")
        if self.message_wait_seconds < 0:
            raise ValueError("message_wait_seconds must not be negative")
        if self.max_transient_retries < 0:
            raise ValueError("max_transient_retries must not be negative")
        if self.retry_base_seconds <= 0 or self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("invalid retry backoff configuration")
        if self.max_receive_count < 1:
            raise ValueError("max_receive_count must be positive")
        if not 1 <= self.metrics_port <= 65535:
            raise ValueError("metrics_port must be a valid TCP port")
        if not 1 <= self.reconcile_page_size <= 1000:
            raise ValueError("reconcile_page_size must be in [1, 1000]")
        if not 1 <= self.reconcile_max_pages <= 1000:
            raise ValueError("reconcile_max_pages must be in [1, 1000]")
        if self.reconcile_min_age_seconds < 0:
            raise ValueError("reconcile_min_age_seconds must not be negative")
        if self.artifact_sha256 is not None and (
            len(self.artifact_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.artifact_sha256)
        ):
            raise ValueError("SENTINEL_WORKER_ARTIFACT_SHA256 must be a sha256 hex digest")
        if self.replay_max_count < 1:
            raise ValueError("replay_max_count must be positive")
        if not 1 <= self.dlq_inspect_limit <= 100:
            raise ValueError("dlq_inspect_limit must be in [1, 100]")
