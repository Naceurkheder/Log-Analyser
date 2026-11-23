"""Durable result ordering: object -> verification -> alerts -> job -> run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError, EndpointConnectionError

from ..api.contract import utcnow
from ..ids import sha256_hex
from .errors import TransientError
from .execution import ExecutionResult
from .message import JobMessage
from .state import SUCCEEDED
from .store import WorkerStore

RESULT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CommitOutcome:
    committed: bool
    result_bucket: str
    result_key: str
    result_sha256: str
    alerts_written: int = 0
    alerts_duplicate: int = 0
    run_finalized: bool = False


def result_key(run_id: str, job_id: str, attempt_id: str) -> str:
    return f"runs/{run_id}/attempts/{job_id}/{attempt_id}.json"


def _guarded(call: Any, *, action: str) -> Any:
    try:
        return call()
    except (ClientError, EndpointConnectionError) as exc:
        raise TransientError(
            f"{action} failed",
            details={"error": type(exc).__name__, "action": action},
        ) from exc


def commit_result(
    store: WorkerStore,
    *,
    settings: Any,
    message: JobMessage,
    job: dict[str, Any],
    execution: ExecutionResult,
    attempt_id: str,
    input_info: dict[str, Any],
    fencing_token: int,
    lease: Any,
) -> CommitOutcome:
    """Commit one attempt, refusing to ack until every durable write succeeds."""
    bucket = settings.results_bucket
    key = result_key(message.run_id, message.job_id, attempt_id)
    result_document = execution.document(
        message=message, job=job, attempt_id=attempt_id, input_info=input_info
    )
    body = result_document
    digest = sha256_hex(body)
    metadata = {
        "sha256": digest,
        "run-id": message.run_id,
        "job-id": message.job_id,
        "attempt-id": attempt_id,
        "backend": execution.backend,
        "config-hash": message.detector["config_hash"],
    }
    _guarded(
        lambda: store.put_json_immutable(
            bucket, key, body, metadata=metadata
        ),
        action="result_upload",
    )
    head = _guarded(
        lambda: store.head_object(bucket, key), action="result_head"
    )
    fetched = _guarded(
        lambda: store.get_object_bytes(bucket, key), action="result_readback"
    )
    if head is None or int(head.get("ContentLength", -1)) != len(body):
        raise TransientError(
            "result object could not be verified after upload",
            details={"bucket": bucket, "key": key, "stage": "head"},
        )
    if sha256_hex(fetched) != digest:
        raise TransientError(
            "result object checksum changed after upload",
            details={"bucket": bucket, "key": key, "stage": "readback"},
        )
    if (head.get("Metadata") or {}).get("sha256") != digest:
        raise TransientError(
            "result object metadata checksum is missing or wrong",
            details={"bucket": bucket, "key": key, "stage": "metadata"},
        )

    written = 0
    duplicates = 0
    for alert in execution.alerts:
        item = dict(alert)
        item["job_id"] = message.job_id
        if not item.get("created_at"):
            item["created_at"] = utcnow()
        if store.put_alert(item):
            written += 1
        else:
            duplicates += 1

    committed = _guarded(
        lambda: store.commit_job(
            message.job_id,
            fencing_token=fencing_token,
            attempt_id=attempt_id,
            result=result_document,
            actual_backend=execution.backend,
            result_bucket=bucket,
            result_key=key,
            result_sha256=digest,
        ),
        action="job_commit",
    )
    if not committed:
        return CommitOutcome(
            committed=False,
            result_bucket=bucket,
            result_key=key,
            result_sha256=digest,
            alerts_written=written,
            alerts_duplicate=duplicates,
        )

    run_finalized = _finalize_run(store, message.run_id)
    return CommitOutcome(
        committed=True,
        result_bucket=bucket,
        result_key=key,
        result_sha256=digest,
        alerts_written=written,
        alerts_duplicate=duplicates,
        run_finalized=run_finalized,
    )


def _finalize_run(store: WorkerStore, run_id: str) -> bool:
    counts = store.run_counts(run_id)
    statuses = {
        job.get("status")
        for job in store.iter_run_items(store.settings.jobs_table, run_id)
    }
    if counts["jobs_total"] == 0:
        return False
    if statuses == {SUCCEEDED}:
        status = "SUCCEEDED"
    elif statuses & {"FAILED_PERMANENT", "DEAD_LETTERED"}:
        status = "FAILED"
    else:
        status = "RUNNING"
    return store.finalize_run(run_id, counts=counts, status=status)
