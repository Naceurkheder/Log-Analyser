"""Bounded outbox reconciliation for job publication and ingest handoff.

DynamoDB and SQS are not transactional. The producer persists the logical job
and marks ``published=false`` before sending; the API does the same for the
run's ``ingest_published`` flag. This reconciler scans bounded pages for old
pending records, republishes the *same* logical message and conditionally marks
it sent. A publish-then-crash duplicate is harmless because workers deduplicate
by deterministic job id and terminal state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError, EndpointConnectionError

from .config import WorkerSettings
from .message import JobMessage, ingest_message_from_run, job_message_from_record
from .store import WorkerStore

log = logging.getLogger("sentinel.worker.reconcile")


@dataclass
class ReconcileReport:
    job_candidates: int = 0
    jobs_republished: int = 0
    run_candidates: int = 0
    runs_republished: int = 0
    skipped_too_recent: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    oldest_pending_age_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_candidates": self.job_candidates,
            "jobs_republished": self.jobs_republished,
            "run_candidates": self.run_candidates,
            "runs_republished": self.runs_republished,
            "skipped_too_recent": self.skipped_too_recent,
            "errors": self.errors,
            "oldest_pending_age_seconds": round(
                self.oldest_pending_age_seconds, 3
            ),
        }


def _age_seconds(timestamp: Any, now: datetime) -> float | None:
    if not isinstance(timestamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds()


def reconcile_outbox(
    store: WorkerStore, settings: WorkerSettings, *, metrics: Any | None = None
) -> ReconcileReport:
    """Republish old, still-unpublished jobs and run ingest handoffs."""
    report = ReconcileReport()
    now = datetime.now(timezone.utc)
    page = {
        "page_size": settings.reconcile_page_size,
        "max_pages": settings.reconcile_max_pages,
    }

    for job in store.pending_jobs(**page):
        age = _age_seconds(job.get("updated_at") or job.get("created_at"), now)
        if age is None or age < settings.reconcile_min_age_seconds:
            report.skipped_too_recent += 1
            continue
        report.job_candidates += 1
        report.oldest_pending_age_seconds = max(
            report.oldest_pending_age_seconds, age
        )
        try:
            message = job_message_from_record(job, settings)
            JobMessage.parse(message, str(job["backend"]))  # validate before send
            store.send_job(str(job["backend"]), message)
            if store.mark_job_published(str(job["job_id"])):
                report.jobs_republished += 1
                if metrics is not None:
                    metrics.outbox_republished += 1
        except (ClientError, EndpointConnectionError, OSError, KeyError, ValueError) as exc:
            report.errors.append(
                {
                    "record": "job",
                    "id": job.get("job_id"),
                    "error": type(exc).__name__,
                }
            )
            log.warning(
                "outbox_job_reconcile_failed",
                extra={
                    "event_fields": {
                        "job_id": job.get("job_id"),
                        "error": type(exc).__name__,
                    }
                },
            )

    for run in store.pending_runs(**page):
        age = _age_seconds(run.get("updated_at") or run.get("created_at"), now)
        if age is None or age < settings.reconcile_min_age_seconds:
            report.skipped_too_recent += 1
            continue
        report.run_candidates += 1
        report.oldest_pending_age_seconds = max(
            report.oldest_pending_age_seconds, age
        )
        try:
            store.send_ingest(ingest_message_from_run(run))
            if store.mark_run_ingest_published(str(run["run_id"])):
                report.runs_republished += 1
                if metrics is not None:
                    metrics.outbox_republished += 1
        except (ClientError, EndpointConnectionError, OSError, KeyError, ValueError) as exc:
            report.errors.append(
                {
                    "record": "run",
                    "id": run.get("run_id"),
                    "error": type(exc).__name__,
                }
            )
            log.warning(
                "outbox_run_reconcile_failed",
                extra={
                    "event_fields": {
                        "run_id": run.get("run_id"),
                        "error": type(exc).__name__,
                    }
                },
            )
    return report
