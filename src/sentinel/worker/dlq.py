"""Operator-facing DLQ inspection and explicit, bounded replay."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .config import Capability, WorkerSettings
from .errors import MessageError
from .store import WorkerStore


@dataclass(frozen=True)
class InspectedMessage:
    """Safe projection of a DLQ message (never includes raw log bytes)."""

    message_id: str
    job_id: str
    run_id: str
    chunk_id: str
    receive_count: int
    error: dict[str, Any]
    chunk: dict[str, Any]
    manifest: dict[str, Any]
    replay: dict[str, Any]
    receipt_handle: str
    body: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "chunk_id": self.chunk_id,
            "receive_count": self.receive_count,
            "error": self.error,
            "chunk": self.chunk,
            "manifest": self.manifest,
            "replay": self.replay,
        }


@dataclass
class ReplayReport:
    backend: str
    reason: str
    inspected: int = 0
    replayed: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)
    jobs_requeued: int = 0
    message_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "reason": self.reason,
            "inspected": self.inspected,
            "replayed": self.replayed,
            "skipped": self.skipped,
            "jobs_requeued": self.jobs_requeued,
            "message_ids": self.message_ids,
        }


# SQS accepts at most ten messages per ReceiveMessage call.
_SQS_MAX_BATCH = 10


def _receive(
    store: WorkerStore, backend: Capability, limit: int, *, visibility: int
) -> list[dict[str, Any]]:
    response = store.sqs.receive_message(
        QueueUrl=store.dlq_url(backend),
        MaxNumberOfMessages=min(max(limit, 1), _SQS_MAX_BATCH),
        VisibilityTimeout=visibility,
        MessageSystemAttributeNames=["ApproximateReceiveCount"],
    )
    return response.get("Messages", [])


def inspect_dlq(
    store: WorkerStore, settings: WorkerSettings, backend: Capability
) -> list[dict[str, Any]]:
    """Show poison messages without consuming them (visibility timeout 0)."""
    results: list[dict[str, Any]] = []
    for raw in _receive(
        store, backend, settings.dlq_inspect_limit, visibility=0
    ):
        try:
            body = json.loads(raw["Body"])
        except (ValueError, KeyError, TypeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        attributes = raw.get("Attributes", {}) or {}
        results.append(
            InspectedMessage(
                message_id=str(raw.get("MessageId", "")),
                job_id=str(body.get("job_id") or ""),
                run_id=str(body.get("run_id") or ""),
                chunk_id=str(body.get("chunk_id") or ""),
                receive_count=int(attributes.get("ApproximateReceiveCount", 0)),
                error=body.get("last_error") or {},
                chunk=body.get("chunk") or {},
                manifest=body.get("manifest") or {},
                replay=body.get("replay") or {},
                receipt_handle=str(raw["ReceiptHandle"]),
                body=body,
            ).as_dict()
        )
    return results


def replay_dlq(
    store: WorkerStore,
    settings: WorkerSettings,
    backend: Capability,
    *,
    reason: str,
    limit: int | None = None,
    operator: str | None = None,
) -> ReplayReport:
    """Requeue quarantined work with an auditable reason and replay budget."""
    if len(reason.strip()) < settings.replay_reason_min_length:
        raise MessageError(
            "dlq replay requires an operator reason",
            code="replay_reason_required",
            details={"min_length": settings.replay_reason_min_length},
        )
    bounded = limit if limit is not None else settings.dlq_inspect_limit
    if not 1 <= bounded <= 100:
        raise MessageError(
            "replay limit must be in [1, 100]",
            code="invalid_replay_limit",
            details={"limit": bounded},
        )
    operator = operator or os.environ.get("USER") or settings.worker_id
    report = ReplayReport(backend=backend, reason=reason.strip())
    seen: set[str] = set()
    while report.inspected < bounded:
        batch = _receive(
            store, backend, bounded - report.inspected, visibility=60
        )
        fresh = [
            raw for raw in batch if str(raw.get("MessageId", "")) not in seen
        ]
        if not fresh:
            break
        for raw in fresh:
            if report.inspected >= bounded:
                break
            report.inspected += 1
            message_id = str(raw.get("MessageId", ""))
            seen.add(message_id)
            try:
                body = json.loads(raw["Body"])
            except (ValueError, KeyError, TypeError):
                body = {"raw_body": str(raw.get("Body", ""))[:2048]}
            if not isinstance(body, dict):
                body = {"raw_body": str(body)[:2048]}
            replay = body.get("replay") or {}
            count = int(replay.get("count", 0)) if isinstance(replay, dict) else 0
            if count >= settings.replay_max_count:
                report.skipped.append(
                    {
                        "message_id": message_id,
                        "job_id": body.get("job_id"),
                        "reason": "replay_budget_exhausted",
                        "count": count,
                    }
                )
                continue
            job_id = str(body.get("job_id") or "")
            if job_id and store.requeue_for_replay(job_id, reason=reason.strip()):
                report.jobs_requeued += 1
            body["replay"] = {
                "count": count + 1,
                "reason": reason.strip(),
                "replayed_at": _now(),
                "operator": operator,
                "previous_error": body.get("last_error"),
            }
            body["dlq_metadata"] = {
                "replayed": True,
                "replayed_at": _now(),
                "operator": operator,
            }
            body.pop("last_error", None)
            store.send_job(backend, body)
            store.sqs.delete_message(
                QueueUrl=store.dlq_url(backend),
                ReceiptHandle=str(raw["ReceiptHandle"]),
            )
            report.replayed += 1
            report.message_ids.append(message_id)
    return report


def _now() -> str:
    from ..api.contract import utcnow

    return utcnow()
