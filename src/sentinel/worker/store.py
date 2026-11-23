"""DynamoDB operations owned by workers.

This is a thin subclass of the API's :class:`SentinelStore`: it reuses the
resource clients and serialisers but adds conditional, fenced job updates.
Every state-changing method first validates the transition in
:mod:`sentinel.worker.state`, then issues a DynamoDB conditional write. The
condition is the concurrency authority; the Python check keeps mistakes loud
and local.
"""

from __future__ import annotations

from typing import Any, Iterable

from botocore.exceptions import ClientError

from ..api.contract import utcnow
from ..api.store import SentinelStore, _clean, _serializer, from_item
from .errors import (
    ClaimError,
    JobNotFoundError,
    LeaseError,
    LeaseLostError,
    StateTransitionError,
    StaleTokenError,
)
from .state import (
    DEAD_LETTERED,
    FAILED_PERMANENT,
    PENDING,
    QUEUED,
    RETRYABLE_FAILED,
    RUNNING,
    SUCCEEDED,
    TERMINAL_STATES,
    assert_transition,
)


class WorkerStore(SentinelStore):
    """Adds fenced leases, terminal-result writes and bounded outbox scans."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._dlq_urls: dict[str, str] = {}

    # -- low-level conditional writes ---------------------------------------
    def _update(
        self,
        table: str,
        key: dict[str, Any],
        *,
        fields: dict[str, Any] | None = None,
        increments: dict[str, int] | None = None,
        removes: Iterable[str] = (),
        condition: str,
        condition_names: dict[str, str] | None = None,
        condition_values: dict[str, Any] | None = None,
        return_values: str = "NONE",
    ) -> dict[str, Any] | None:
        """Run one conditional update; ``None`` means the condition failed."""
        names: dict[str, str] = dict(condition_names or {})
        values: dict[str, Any] = {
            key_: _serializer.serialize(_clean(value))
            for key_, value in (condition_values or {}).items()
        }
        parts: list[str] = []

        set_fields = fields or {}
        if set_fields:
            assignments: list[str] = []
            for index, (name, value) in enumerate(set_fields.items()):
                nk, vk = f"#s{index}", f":s{index}"
                names[nk] = name
                values[vk] = _serializer.serialize(_clean(value))
                assignments.append(f"{nk} = {vk}")
            parts.append("SET " + ", ".join(assignments))

        increments = increments or {}
        if increments:
            additions: list[str] = []
            for index, (name, value) in enumerate(increments.items()):
                nk, vk = f"#i{index}", f":i{index}"
                names[nk] = name
                values[vk] = _serializer.serialize(_clean(value))
                additions.append(f"{nk} {vk}")
            parts.append("ADD " + ", ".join(additions))

        removals = list(removes)
        if removals:
            removed: list[str] = []
            for index, name in enumerate(removals):
                nk = f"#r{index}"
                names[nk] = name
                removed.append(nk)
            parts.append("REMOVE " + ", ".join(removed))

        try:
            response = self.dynamodb.update_item(
                TableName=table,
                Key=key,
                UpdateExpression=" ".join(parts),
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues=return_values,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return None
            raise
        attributes = response.get("Attributes")
        return from_item(attributes) if attributes else response

    @staticmethod
    def _job_key(job_id: str) -> dict[str, Any]:
        return {"job_id": {"S": job_id}}

    def _job(
        self,
        job_id: str,
        *,
        fields: dict[str, Any] | None = None,
        increments: dict[str, int] | None = None,
        condition: str,
        condition_names: dict[str, str] | None = None,
        condition_values: dict[str, Any] | None = None,
        return_values: str = "NONE",
    ) -> dict[str, Any] | None:
        return self._update(
            self.settings.jobs_table,
            self._job_key(job_id),
            fields=fields,
            increments=increments,
            condition=condition,
            condition_names=condition_names,
            condition_values=condition_values,
            return_values=return_values,
        )

    # -- leases -------------------------------------------------------------
    def claim_job(
        self,
        *,
        job_id: str,
        run_id: str,
        backend: str,
        detector_config_hash: str,
        artifact_id: str,
        worker_id: str,
        attempt_id: str,
        lease_expires_at: str,
    ) -> dict[str, Any]:
        """Atomically claim a pending/queued/retryable job.

        The returned item contains the new ``fencing_token``. Every later write
        must present it; a late worker from a previous attempt is rejected.
        """
        job = self.get_job(job_id)
        if job is None:
            raise JobNotFoundError(
                f"job '{job_id}' does not exist", details={"job_id": job_id}
            )
        assert_transition(str(job.get("status")), RUNNING, job_id=job_id)
        actual = {
            "run_id": job.get("run_id"),
            "backend": job.get("backend"),
            "detector_config_hash": job.get("detector_config_hash"),
            "artifact_id": job.get("artifact_id"),
        }
        expected = {
            "run_id": run_id,
            "backend": backend,
            "detector_config_hash": detector_config_hash,
            "artifact_id": artifact_id,
        }
        if actual != expected:
            raise ClaimError(
                "message identity does not match the authoritative job record",
                details={"expected": expected, "actual": actual, "job_id": job_id},
            )

        now = utcnow()
        updated = self._job(
            job_id,
            fields={
                "status": RUNNING,
                "worker_id": worker_id,
                "attempt_id": attempt_id,
                "lease_expires_at": lease_expires_at,
                "lease_started_at": now,
                "heartbeat_at": now,
                "attempt_state": "running",
                "updated_at": now,
            },
            increments={"attempts": 1, "fencing_token": 1},
            condition=(
                "#status IN (:pending, :queued, :retryable) "
                "AND #run_id = :run_id AND #backend = :backend "
                "AND #config = :config AND #artifact = :artifact"
            ),
            condition_names={
                "#status": "status",
                "#run_id": "run_id",
                "#backend": "backend",
                "#config": "detector_config_hash",
                "#artifact": "artifact_id",
            },
            condition_values={
                ":pending": PENDING,
                ":queued": QUEUED,
                ":retryable": RETRYABLE_FAILED,
                ":run_id": run_id,
                ":backend": backend,
                ":config": detector_config_hash,
                ":artifact": artifact_id,
            },
            return_values="ALL_NEW",
        )
        if updated is None:
            raise ClaimError(
                "conditional claim lost",
                details={"job_id": job_id, "status": job.get("status")},
            )
        return updated

    def _running_guard(
        self, job_id: str, fencing_token: int, attempt_id: str
    ) -> tuple[dict[str, str], dict[str, Any]]:
        return (
            {
                "#status": "status",
                "#token": "fencing_token",
                "#attempt": "attempt_id",
                "#lost": "lease_lost_at",
            },
            {
                ":running": RUNNING,
                ":token": fencing_token,
                ":attempt": attempt_id,
            },
        )

    def heartbeat(
        self,
        job_id: str,
        *,
        fencing_token: int,
        attempt_id: str,
        lease_expires_at: str,
    ) -> dict[str, Any] | None:
        """Conditionally extend the DynamoDB lease for the active attempt."""
        names, values = self._running_guard(job_id, fencing_token, attempt_id)
        now = utcnow()
        return self._job(
            job_id,
            fields={
                "lease_expires_at": lease_expires_at,
                "heartbeat_at": now,
                "updated_at": now,
            },
            condition=(
                "#status = :running AND #token = :token "
                "AND #attempt = :attempt AND attribute_not_exists(#lost)"
            ),
            condition_names=names,
            condition_values=values,
            return_values="ALL_NEW",
        )

    def mark_lease_lost(
        self,
        job_id: str,
        *,
        fencing_token: int,
        attempt_id: str,
        reason: str,
    ) -> None:
        """Record an uncertain attempt; this never commits a result."""
        names, values = self._running_guard(job_id, fencing_token, attempt_id)
        now = utcnow()
        self._job(
            job_id,
            fields={
                # A lost lease makes the attempt retryable so redelivery can
                # claim a new fencing token instead of seeing a stranded RUNNING.
                "status": RETRYABLE_FAILED,
                "lease_lost_at": now,
                "lease_loss_reason": reason,
                "attempt_state": "uncertain",
                "updated_at": now,
            },
            condition=(
                "#status = :running AND #token = :token "
                "AND #attempt = :attempt AND attribute_not_exists(#lost)"
            ),
            condition_names=names,
            condition_values=values,
        )

    def _guard_running(
        self, job_id: str, fencing_token: int, attempt_id: str
    ) -> None:
        job = self.get_job(job_id)
        if job is None:
            raise JobNotFoundError(
                f"job '{job_id}' disappeared", details={"job_id": job_id}
            )
        if job.get("fencing_token") != fencing_token:
            raise StaleTokenError(
                "fencing token is no longer current",
                details={
                    "job_id": job_id,
                    "expected": fencing_token,
                    "actual": job.get("fencing_token"),
                },
            )
        if job.get("attempt_id") != attempt_id or job.get("status") != RUNNING:
            raise StaleTokenError(
                "attempt is no longer active",
                details={"job_id": job_id, "status": job.get("status")},
            )
        if job.get("lease_lost_at") is not None:
            raise LeaseLostError(
                "lease was lost; result must not be committed",
                details={"job_id": job_id, "reason": job.get("lease_loss_reason")},
            )

    # -- result / failure commits -------------------------------------------
    def commit_job(
        self,
        job_id: str,
        *,
        fencing_token: int,
        attempt_id: str,
        result: dict[str, Any],
        actual_backend: str,
        result_bucket: str,
        result_key: str,
        result_sha256: str,
    ) -> bool:
        """Transition RUNNING -> SUCCEEDED with the active fencing token.

        Returns ``False`` when the same logical job was already committed by
        another delivery (durable commit followed by a crash before ack).
        """
        job = self.get_job(job_id)
        if job is None:
            raise JobNotFoundError(
                f"job '{job_id}' disappeared", details={"job_id": job_id}
            )
        if job.get("status") == SUCCEEDED:
            if (
                job.get("fencing_token") != fencing_token
                or job.get("attempt_id") != attempt_id
            ):
                raise StaleTokenError(
                    "commit replay does not belong to the committed attempt",
                    details={"job_id": job_id},
                )
            # The durable transition already happened; never rewrite it.
            return False
        # A lost lease is authoritative even if the local worker has not seen
        # the renewal failure yet: such an attempt may never commit.
        if job.get("lease_lost_at") is not None:
            raise LeaseLostError(
                "lease was lost; result must not be committed",
                details={
                    "job_id": job_id,
                    "reason": job.get("lease_loss_reason"),
                },
            )
        self._guard_running(job_id, fencing_token, attempt_id)
        assert_transition(str(job.get("status")), SUCCEEDED, job_id=job_id)
        names, values = self._running_guard(job_id, fencing_token, attempt_id)
        names["#lease"] = "lease_expires_at"
        now = utcnow()
        values[":now"] = now
        result_reference = {
            "bucket": result_bucket,
            "key": result_key,
            "sha256": result_sha256,
            "attempt_id": attempt_id,
        }
        updated = self._job(
            job_id,
            fields={
                "status": SUCCEEDED,
                "result": result_reference,
                "result_bucket": result_bucket,
                "result_key": result_key,
                "result_sha256": result_sha256,
                "actual_backend": actual_backend,
                "completed_at": now,
                "attempt_state": "succeeded",
                "updated_at": now,
            },
            condition=(
                "#status = :running AND #token = :token "
                "AND #attempt = :attempt AND #lease > :now "
                "AND attribute_not_exists(#lost)"
            ),
            condition_names=names,
            condition_values=values,
        )
        if updated is None:
            current = self.get_job(job_id) or {}
            if current.get("status") == SUCCEEDED:
                if (
                    current.get("fencing_token") != fencing_token
                    or current.get("attempt_id") != attempt_id
                ):
                    raise StaleTokenError(
                        "commit lost the fencing token",
                        details={"job_id": job_id},
                    )
                return False
            raise StaleTokenError(
                "commit lost the lease or fencing token",
                details={"job_id": job_id, "status": current.get("status")},
            )
        return True

    def fail_job(
        self,
        job_id: str,
        *,
        fencing_token: int,
        attempt_id: str,
        status: str,
        error: dict[str, Any],
    ) -> bool:
        """Move RUNNING -> a failure state with fenced conditional writes."""
        if status not in {RETRYABLE_FAILED, FAILED_PERMANENT, DEAD_LETTERED}:
            raise StateTransitionError(
                f"unsupported failure state: {status}", details={"job_id": job_id}
            )
        job = self.get_job(job_id)
        if job is None:
            raise JobNotFoundError(
                f"job '{job_id}' disappeared", details={"job_id": job_id}
            )
        if job.get("status") == status:
            return False
        # Check ownership before the state machine: a late worker from an old
        # attempt must see a fencing error, not a misleading transition error.
        self._guard_running(job_id, fencing_token, attempt_id)
        assert_transition(str(job.get("status")), status, job_id=job_id)
        names, values = self._running_guard(job_id, fencing_token, attempt_id)
        now = utcnow()
        updated = self._job(
            job_id,
            fields={
                "status": status,
                "last_error": error,
                "attempt_state": (
                    "retryable" if status == RETRYABLE_FAILED else "failed"
                ),
                "failed_at": now,
                "updated_at": now,
            },
            condition=(
                "#status = :running AND #token = :token "
                "AND #attempt = :attempt AND attribute_not_exists(#lost)"
            ),
            condition_names=names,
            condition_values=values,
        )
        return updated is not None

    def dead_letter_job(self, job_id: str, *, error: dict[str, Any]) -> bool:
        """Quarantine a non-terminal job, e.g. after a validation failure."""
        job = self.get_job(job_id)
        if job is None:
            return False
        status = str(job.get("status"))
        if status in TERMINAL_STATES:
            return False
        assert_transition(status, DEAD_LETTERED, job_id=job_id)
        now = utcnow()
        updated = self._job(
            job_id,
            fields={
                "status": DEAD_LETTERED,
                "last_error": error,
                "dead_lettered_at": now,
                "updated_at": now,
            },
            condition="#status IN (:pending, :queued, :running, :retryable)",
            condition_names={"#status": "status"},
            condition_values={
                ":pending": PENDING,
                ":queued": QUEUED,
                ":running": RUNNING,
                ":retryable": RETRYABLE_FAILED,
            },
        )
        return updated is not None

    def requeue_for_replay(self, job_id: str, *, reason: str) -> bool:
        """Explicit operator override: terminal job back to QUEUED.

        This is deliberately *not* part of the automatic state machine. It is
        only reachable from ``dlq-replay`` with an auditable reason, and it
        bumps the fencing token so the worker that failed the job cannot come
        back and commit a late result.
        """
        job = self.get_job(job_id)
        if job is None:
            return False
        if job.get("status") not in TERMINAL_STATES:
            return False
        updated = self._job(
            job_id,
            fields={
                "status": QUEUED,
                "replay_reason": reason,
                "replayed_at": utcnow(),
                "attempt_state": "requeued",
                "updated_at": utcnow(),
            },
            increments={"fencing_token": 1},
            condition="#status IN (:succeeded, :failed, :dead)",
            condition_names={"#status": "status"},
            condition_values={
                ":succeeded": SUCCEEDED,
                ":failed": FAILED_PERMANENT,
                ":dead": DEAD_LETTERED,
            },
        )
        return updated is not None

    # -- alerts and run aggregation -----------------------------------------
    def put_alert(self, alert: dict[str, Any]) -> bool:
        """Write one deterministic alert; duplicate deliveries are ignored."""
        try:
            self.dynamodb.put_item(
                TableName=self.settings.alerts_table,
                Item=self._alert_item(alert),
                ConditionExpression="attribute_not_exists(alert_id)",
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    @staticmethod
    def _alert_item(alert: dict[str, Any]) -> dict[str, Any]:
        from ..api.store import to_item

        return to_item(alert)

    def iter_run_items(
        self, table: str, run_id: str, *, page_size: int = 200
    ) -> Iterable[dict[str, Any]]:
        """Page a run's GSI; callers derive counters from authoritative rows."""
        start_key: dict[str, Any] | None = None
        while True:
            items, start_key = self._query_run(table, run_id, page_size, start_key)
            yield from items
            if start_key is None:
                return

    def run_counts(self, run_id: str) -> dict[str, int]:
        counts = {
            "jobs_total": 0,
            "jobs_completed": 0,
            "jobs_failed": 0,
            "alerts_total": 0,
        }
        for job in self.iter_run_items(self.settings.jobs_table, run_id):
            counts["jobs_total"] += 1
            status = job.get("status")
            if status == SUCCEEDED:
                counts["jobs_completed"] += 1
            elif status in {FAILED_PERMANENT, DEAD_LETTERED}:
                counts["jobs_failed"] += 1
        for _ in self.iter_run_items(self.settings.alerts_table, run_id):
            counts["alerts_total"] += 1
        return counts

    def finalize_run(
        self, run_id: str, *, counts: dict[str, int], status: str
    ) -> bool:
        """Derive run state from job records; never double-complete a run."""
        fields = dict(counts)
        fields.update(status=status, updated_at=utcnow())
        if status in {SUCCEEDED, "FAILED"}:
            fields["run_completed_at"] = utcnow()
            updated = self._update(
                self.settings.runs_table,
                {"run_id": {"S": run_id}},
                fields=fields,
                condition="attribute_not_exists(#completed)",
                condition_names={"#completed": "run_completed_at"},
            )
            return updated is not None
        self._update(
            self.settings.runs_table,
            {"run_id": {"S": run_id}},
            fields=fields,
            condition="attribute_exists(#id)",
            condition_names={"#id": "run_id"},
        )
        return False

    # -- outbox / reconciliation --------------------------------------------
    def _scan(
        self, table: str, *, condition: str, names: dict[str, str], values: dict[str, Any],
        page_size: int, max_pages: int,
    ) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        start_key: dict[str, Any] | None = None
        for _ in range(max_pages):
            kwargs: dict[str, Any] = {
                "TableName": table,
                "FilterExpression": condition,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": {
                    key: _serializer.serialize(_clean(value))
                    for key, value in values.items()
                },
                "Limit": page_size,
            }
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            response = self.dynamodb.scan(**kwargs)
            found.extend(from_item(item) for item in response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if start_key is None:
                break
        return found

    def pending_jobs(self, *, page_size: int, max_pages: int) -> list[dict[str, Any]]:
        return self._scan(
            self.settings.jobs_table,
            condition="#published = :false",
            names={"#published": "published"},
            values={":false": False},
            page_size=page_size,
            max_pages=max_pages,
        )

    def pending_runs(self, *, page_size: int, max_pages: int) -> list[dict[str, Any]]:
        return self._scan(
            self.settings.runs_table,
            condition="#published = :false OR attribute_not_exists(#published)",
            names={"#published": "ingest_published"},
            values={":false": False},
            page_size=page_size,
            max_pages=max_pages,
        )

    def mark_job_published(self, job_id: str) -> bool:
        updated = self._job(
            job_id,
            fields={
                "published": True,
                "published_at": utcnow(),
                "last_dispatch_at": utcnow(),
                "updated_at": utcnow(),
            },
            increments={"dispatch_attempts": 1},
            condition="#published = :false",
            condition_names={"#published": "published"},
            condition_values={":false": False},
        )
        return updated is not None

    def mark_run_ingest_published(self, run_id: str) -> bool:
        updated = self._update(
            self.settings.runs_table,
            {"run_id": {"S": run_id}},
            fields={
                "ingest_published": True,
                "ingest_published_at": utcnow(),
                "last_dispatch_at": utcnow(),
                "updated_at": utcnow(),
            },
            increments={"dispatch_attempts": 1},
            condition="#published = :false OR attribute_not_exists(#published)",
            condition_names={"#published": "ingest_published"},
            condition_values={":false": False},
        )
        return updated is not None

    # -- queues -------------------------------------------------------------
    def dlq_url(self, backend: str) -> str:
        if backend not in self._dlq_urls:
            if backend == "gpu":
                name = self.settings.jobs_gpu_queue_name + "-dlq"
            elif backend == "cpu":
                name = self.settings.jobs_cpu_queue_name + "-dlq"
            else:
                raise ValueError(f"unknown backend: {backend}")
            self._dlq_urls[backend] = self.sqs.get_queue_url(QueueName=name)[
                "QueueUrl"
            ]
        return self._dlq_urls[backend]

    def send_dlq(self, backend: str, message: dict[str, Any]) -> None:
        self._send(self.dlq_url(backend), message)
