"""Fenced lease with periodic SQS visibility and DynamoDB renewal."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from botocore.exceptions import ClientError, EndpointConnectionError

from .config import WorkerSettings
from .errors import LeaseLostError
from .metrics import WorkerMetrics
from .store import WorkerStore

log = logging.getLogger("sentinel.worker.lease")


def lease_expiry(seconds: int) -> str:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return deadline.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Lease:
    """One active attempt. Losing it permanently forbids committing results."""

    def __init__(
        self,
        *,
        store: WorkerStore,
        settings: WorkerSettings,
        metrics: WorkerMetrics,
        queue_url: str,
        receipt_handle: str,
        job_id: str,
        fencing_token: int,
        attempt_id: str,
        on_loss: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.metrics = metrics
        self.queue_url = queue_url
        self.receipt_handle = receipt_handle
        self.job_id = job_id
        self.fencing_token = fencing_token
        self.attempt_id = attempt_id
        self.on_loss = on_loss
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._deadline = time.monotonic() + settings.visibility_timeout
        self._started = time.monotonic()
        self._last_heartbeat = self._started
        self._thread: threading.Thread | None = None

    @property
    def lost(self) -> bool:
        return self._lost.is_set() or time.monotonic() >= self._deadline

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self._started

    @property
    def heartbeat_age_seconds(self) -> float:
        return time.monotonic() - self._last_heartbeat

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"sentinel-lease-{self.job_id[:12]}",
            daemon=True,
        )
        self._thread.start()

    def check(self) -> None:
        """Called immediately before any durable commit."""
        if self.lost:
            raise LeaseLostError(
                "lease is no longer valid",
                details={"job_id": self.job_id, "token": self.fencing_token},
            )

    def renew(self) -> bool:
        expiry = lease_expiry(self.settings.visibility_timeout)
        try:
            updated = self.store.heartbeat(
                self.job_id,
                fencing_token=self.fencing_token,
                attempt_id=self.attempt_id,
                lease_expires_at=expiry,
            )
            self.store.sqs.change_message_visibility(
                QueueUrl=self.queue_url,
                ReceiptHandle=self.receipt_handle,
                VisibilityTimeout=self.settings.visibility_timeout,
            )
        except (ClientError, EndpointConnectionError, OSError) as exc:
            self._fail(
                "sqs_visibility_renewal_failed",
                details={"error": type(exc).__name__},
            )
            return False
        if updated is None:
            self._fail(
                "lease_renewal_lost",
                details={"token": self.fencing_token},
            )
            return False
        self._deadline = time.monotonic() + self.settings.visibility_timeout
        self._last_heartbeat = time.monotonic()
        self.metrics.heartbeat_age_seconds = 0.0
        return True

    def force_loss(self, reason: str = "operator_injection") -> None:
        """Failure-injection hook: simulate visibility expiry/lost ownership."""
        self._fail(reason)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self.settings.heartbeat_interval, 1) + 1)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.settings.heartbeat_interval):
            if not self.renew():
                return

    def _fail(self, reason: str, *, details: dict[str, Any] | None = None) -> None:
        if self._lost.is_set():
            return
        self._lost.set()
        self._stop.set()
        self.metrics.lease_losses += 1
        self.metrics.heartbeat_failures += 1
        self.metrics.accepting_work = False
        self.metrics.last_error = reason
        log.warning(
            "worker_lease_lost",
            extra={
                "event_fields": {
                    "job_id": self.job_id,
                    "attempt_id": self.attempt_id,
                    "token": self.fencing_token,
                    "reason": reason,
                    "details": details or {},
                }
            },
        )
        try:
            self.store.mark_lease_lost(
                self.job_id,
                fencing_token=self.fencing_token,
                attempt_id=self.attempt_id,
                reason=reason,
            )
        except (ClientError, EndpointConnectionError, OSError) as exc:  # pragma: no cover
            log.warning(
                "lease_loss_record_failed",
                extra={"event_fields": {"job_id": self.job_id, "error": type(exc).__name__}},
            )
        if self.on_loss is not None:
            self.on_loss(reason)
