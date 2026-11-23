"""S6-backed result committer for the optional Spark stage.

Spark's default committer is advisory only. This module supplies the real
integration boundary: it claims each selected job with the S6 conditional
worker store, executes the deterministic CPU detector, writes the immutable
attempt object, then commits alerts and job state under the claim's fencing
token. A stale or lost attempt can therefore never overwrite a newer worker's
result.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ..api.contract import utcnow
from ..worker.errors import StaleTokenError
from ..worker.result import _finalize_run
from ..worker.state import RETRYABLE_FAILED
from ..worker.store import WorkerStore
from .core import JobRef, SparkStageError, attempt_id_for

log = logging.getLogger("sentinel.spark.commit")


@dataclass(frozen=True)
class SparkClaim:
    """A conditional S6 lease held by one Spark attempt."""

    job_id: str
    attempt_id: str
    fencing_token: int
    worker_id: str
    lease_expires_at: str
    ref: JobRef


def _lease_expiry(seconds: int) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class FencedWorkerCommitter:
    """Commit Spark attempts through the S6 fenced worker store.

    ``prepare`` must be called before the Spark tasks are submitted. It claims
    every planned job and stores the fencing token in memory on the driver.
    ``commit`` then uses that token for the durable result transition.
    """

    name = "worker-fenced"

    def __init__(self, *, worker_id: str | None = None, lease_seconds: int = 300) -> None:
        if lease_seconds < 60:
            raise ValueError("Spark lease_seconds must be at least 60")
        self.worker_id = worker_id or f"spark-{socket.gethostname()}-{os.getpid()}"
        self.lease_seconds = lease_seconds
        self._claims: dict[str, SparkClaim] = {}
        self._store: Any | None = None

    def prepare(self, *, plan: Any, settings: Any, store: Any) -> dict[str, SparkClaim]:
        """Claim all planned jobs; any partial claim is rolled back on failure."""
        if not isinstance(store, WorkerStore):
            raise SparkStageError(
                "worker-fenced committer requires a WorkerStore instance"
            )
        claims: dict[str, SparkClaim] = {}
        try:
            for ref in plan.refs:
                attempt_id = attempt_id_for(
                    ref.job_id, ref.source_sha256, ref.detector_config_hash
                )
                updated = store.claim_job(
                    job_id=ref.job_id,
                    run_id=ref.run_id,
                    backend=ref.backend,
                    detector_config_hash=ref.detector_config_hash,
                    artifact_id=str(ref.artifact_id or ""),
                    worker_id=self.worker_id,
                    attempt_id=attempt_id,
                    lease_expires_at=_lease_expiry(self.lease_seconds),
                )
                token = updated.get("fencing_token")
                if not isinstance(token, int):
                    raise SparkStageError(
                        f"claim for {ref.job_id} returned no fencing token"
                    )
                claims[ref.job_id] = SparkClaim(
                    job_id=ref.job_id,
                    attempt_id=attempt_id,
                    fencing_token=token,
                    worker_id=self.worker_id,
                    lease_expires_at=str(updated.get("lease_expires_at") or ""),
                    ref=ref,
                )
        except Exception:
            self.abort(claims=claims, reason="spark claim preparation failed")
            raise
        self._claims = claims
        self._store = store
        return claims

    def commit(
        self,
        *,
        result: Mapping[str, Any],
        output: Any,
        settings: Any,
        store: Any,
    ) -> dict[str, Any]:
        """Commit one immutable attempt under the prepared fencing token."""
        job_id = str(result.get("job_id") or "")
        claim = self._claims.get(job_id)
        if claim is None:
            raise SparkStageError(f"no Spark claim prepared for job {job_id}")
        if result.get("attempt_id") != claim.attempt_id:
            raise StaleTokenError(
                "Spark attempt identity does not match the claimed attempt",
                details={"job_id": job_id},
            )

        committed = store.commit_job(
            job_id,
            fencing_token=claim.fencing_token,
            attempt_id=claim.attempt_id,
            result=dict(result),
            actual_backend=str(result.get("actual_backend") or "spark-cpu"),
            result_bucket=output.bucket,
            result_key=output.key,
            result_sha256=output.sha256,
        )
        if not committed:
            self._claims.pop(job_id, None)
            return {
                "committed": False,
                "durable": True,
                "committer": self.name,
                "reason": "already committed",
            }

        written = 0
        duplicates = 0
        for alert in result.get("alerts") or []:
            item = dict(alert)
            item["job_id"] = job_id
            if not item.get("created_at"):
                item["created_at"] = utcnow()
            if store.put_alert(item):
                written += 1
            else:
                duplicates += 1

        run_finalized = _finalize_run(store, str(result["run_id"]))
        deleted = self._drain_queues(store, [job_id])
        self._claims.pop(job_id, None)
        log.info(
            "spark_result_committed",
            extra={
                "event_fields": {
                    "service": "spark",
                    "run_id": result["run_id"],
                    "job_id": job_id,
                    "attempt_id": claim.attempt_id,
                    "alerts_written": written,
                    "alerts_duplicate": duplicates,
                    "messages_deleted": deleted,
                }
            },
        )
        return {
            "committed": True,
            "durable": True,
            "committer": self.name,
            "alerts_written": written,
            "alerts_duplicate": duplicates,
            "run_finalized": run_finalized,
            "messages_deleted": deleted,
        }

    def abort(self, *, claims: Mapping[str, SparkClaim] | None = None, reason: str) -> int:
        """Return claimed jobs to a retryable state after a Spark failure."""
        active = dict(self._claims if claims is None else claims)
        failed = 0
        for job_id, claim in active.items():
            try:
                store = getattr(self, "_store", None)
                if store is None:
                    break
                store.fail_job(
                    job_id,
                    fencing_token=claim.fencing_token,
                    attempt_id=claim.attempt_id,
                    status=RETRYABLE_FAILED,
                    error={
                        "code": "spark_attempt_failed",
                        "message": reason[:500],
                        "attempt_id": claim.attempt_id,
                    },
                )
                failed += 1
            except Exception as exc:  # pragma: no cover - defensive cleanup
                log.warning(
                    "spark_claim_abort_failed",
                    extra={"event_fields": {"job_id": job_id, "error": type(exc).__name__}},
                )
        if claims is None:
            self._claims.clear()
        return failed

    def bind_store(self, store: Any) -> "FencedWorkerCommitter":
        """Remember the store used by ``prepare`` so ``abort`` can release claims."""
        self._store = store
        return self

    def _drain_queues(self, store: Any, job_ids: list[str]) -> int:
        """Delete the already-dispatched SQS messages for committed jobs.

        Spark selects work from DynamoDB, so without this step the original
        queue message would later be received by an S6 worker and fail its
        claim against the now-terminal job.
        """
        remaining = set(job_ids)
        deleted = 0
        for queue_name in {
            store.settings.jobs_cpu_queue_name,
            store.settings.jobs_gpu_queue_name,
        }:
            if not remaining:
                break
            try:
                url = store.sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]
                for _ in range(10):
                    response = store.sqs.receive_message(
                        QueueUrl=url,
                        MaxNumberOfMessages=10,
                        WaitTimeSeconds=0,
                        VisibilityTimeout=1,
                    )
                    messages = response.get("Messages") or []
                    if not messages:
                        break
                    for message in messages:
                        try:
                            body = json.loads(message.get("Body") or "{}")
                        except (TypeError, json.JSONDecodeError):
                            continue
                        if not isinstance(body, Mapping):
                            continue
                        job_id = body.get("job_id")
                        if job_id not in remaining:
                            continue
                        store.sqs.delete_message(
                            QueueUrl=url,
                            ReceiptHandle=message["ReceiptHandle"],
                        )
                        remaining.discard(job_id)
                        deleted += 1
            except Exception as exc:  # pragma: no cover - defensive cleanup
                log.warning(
                    "spark_queue_drain_failed",
                    extra={
                        "event_fields": {
                            "queue": queue_name,
                            "error": type(exc).__name__,
                        }
                    },
                )
        return deleted


def committer_for(name: str) -> Any:
    """Return the committer named by configuration."""
    if name == "logging":
        from .job import LoggingCommitter

        return LoggingCommitter()
    if name == "worker-fenced":
        return FencedWorkerCommitter()
    raise SparkStageError(f"unknown result committer: {name}")
