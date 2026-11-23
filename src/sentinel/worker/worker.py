"""Common capability-specific worker runtime."""

from __future__ import annotations

import json
import logging
import random
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from botocore.exceptions import ClientError, EndpointConnectionError

from ..api.contract import utcnow
from .artifact import ArtifactVerifier, ArtifactIdentity
from .config import WorkerSettings
from .errors import (
    ClaimError,
    JobNotFoundError,
    LeaseError,
    MessageError,
    PermanentError,
    PoisonMessageError,
    StaleTokenError,
    StateTransitionError,
    TransientError,
    WorkerError,
)
from .execution import ExecutionResult, execute_job
from .input import load_job_input
from .lease import Lease, lease_expiry
from .message import JobMessage, validate_authoritative
from .metrics import MetricsServer, WorkerMetrics
from .result import CommitOutcome, commit_result
from .state import DEAD_LETTERED, RETRYABLE_FAILED, RUNNING, TERMINAL_STATES
from .store import WorkerStore

log = logging.getLogger("sentinel.worker")

Executor = Callable[..., ExecutionResult]


@dataclass
class ProcessOutcome:
    """Structured result of handling one SQS delivery."""

    status: str
    job_id: str = ""
    message_id: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class Worker:
    """Polls exactly one capability-specific queue and commits fenced results.

    The constructor creates clients but starts no threads or services. Calling
    :meth:`serve` starts the metrics HTTP server and the consumer loop.
    """

    def __init__(
        self,
        settings: WorkerSettings,
        store: WorkerStore | None = None,
        *,
        metrics: WorkerMetrics | None = None,
        executor: Executor | None = None,
        verifier: ArtifactVerifier | None = None,
        start_metrics: bool = True,
    ) -> None:
        self.settings = settings
        self.store = store or WorkerStore(settings.api)
        self.metrics = metrics or WorkerMetrics(
            capability=settings.capability, worker_id=settings.worker_id
        )
        # Resolve the default at call time so failure-injection tests (and
        # operators hot-patching a module during development) are honoured.
        self._executor = executor
        self.verifier = verifier or ArtifactVerifier(settings)
        self.queue_url = self.store.jobs_queue_url(settings.capability)
        self._metrics_server: MetricsServer | None = None
        if start_metrics:
            self._metrics_server = MetricsServer(
                self.metrics, settings.metrics_host, settings.metrics_port
            )
        self._stop = False
        self._paused_reason = ""

    @property
    def executor(self) -> Executor:
        return self._executor if self._executor is not None else execute_job

    @property
    def accepting_work(self) -> bool:
        return not self._stop and not self._paused_reason

    def pause(self, reason: str) -> None:
        """Stop accepting new work after an unrecoverable lease failure."""
        self._paused_reason = reason
        self.metrics.accepting_work = False
        self.metrics.last_error = reason
        log.warning(
            "worker_paused",
            extra={"event_fields": {"reason": reason, "backend": self.settings.capability}},
        )

    def serve(self, *, once: bool = False) -> int:
        """Run the consumer; ``once`` processes at most one batch and exits."""
        if self._metrics_server is not None:
            self._metrics_server.start()
        self._install_signals()
        processed = 0
        try:
            while not self._stop:
                if not self.accepting_work:
                    if once:
                        break
                    time.sleep(self.settings.paused_poll_seconds)
                    continue
                outcome = self.process_once()
                if outcome is not None:
                    processed += 1
                if once:
                    break
                if outcome is None:
                    time.sleep(self.settings.loop_poll_seconds)
        finally:
            self.metrics.active_state = "stopped"
            if self._metrics_server is not None:
                self._metrics_server.close()
        return processed

    def process_once(self) -> ProcessOutcome | None:
        """Receive a bounded batch; return the last outcome or ``None``."""
        try:
            response = self.store.sqs.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=self.settings.max_messages,
                WaitTimeSeconds=self.settings.message_wait_seconds,
                MessageSystemAttributeNames=["ApproximateReceiveCount"],
            )
        except (ClientError, EndpointConnectionError, OSError) as exc:
            log.warning(
                "sqs_receive_failed",
                extra={"event_fields": {"error": type(exc).__name__}},
            )
            time.sleep(min(self.settings.retry_base_seconds, 1.0))
            return None
        messages = response.get("Messages", [])
        last: ProcessOutcome | None = None
        for message in messages:
            if not self.accepting_work:
                break
            last = self.process_message(message)
        return last

    # -- one delivery -------------------------------------------------------
    def process_message(self, sqs_message: dict[str, Any]) -> ProcessOutcome:
        receipt = str(sqs_message["ReceiptHandle"])
        message_id = str(sqs_message.get("MessageId", ""))
        attributes = sqs_message.get("Attributes", {}) or {}
        receive_count = int(attributes.get("ApproximateReceiveCount", 1))
        raw: Any
        try:
            raw = json.loads(sqs_message["Body"])
        except (ValueError, KeyError, TypeError):
            error = WorkerError(
                "queue message is not valid JSON", code="invalid_json"
            )
            self._quarantine(None, receipt, error, receive_count, phase="parse")
            return ProcessOutcome("dlq", message_id=message_id)

        try:
            message = JobMessage.parse(raw, self.settings.capability)
        except PermanentError as exc:
            self._quarantine(
                raw if isinstance(raw, dict) else None,
                receipt,
                exc,
                receive_count,
                phase="validate",
            )
            return ProcessOutcome("dlq", message_id=message_id)

        replay_count = int((message.replay or {}).get("count", 0))
        if replay_count > self.settings.replay_max_count:
            error = PoisonMessageError(
                "message exceeded the operator replay budget",
                details={"replay_count": replay_count},
            )
            self._quarantine(message.raw, receipt, error, receive_count, phase="replay")
            return ProcessOutcome(
                "dlq", job_id=message.job_id, message_id=message_id
            )

        self.metrics.active_state = "claiming"
        try:
            job = self.store.get_job(message.job_id)
            if job is None:
                raise JobNotFoundError(
                    "job record does not exist",
                    details={"job_id": message.job_id},
                )
            run = self.store.get_run(message.run_id)
            validate_authoritative(message, job=job, run=run)
        except PermanentError as exc:
            self._quarantine(
                message.raw,
                receipt,
                exc,
                receive_count,
                phase="authoritative_check",
                job=job if "job" in locals() else None,
            )
            return ProcessOutcome(
                "dlq", job_id=message.job_id, message_id=message_id
            )
        except (ClientError, EndpointConnectionError, OSError) as exc:
            self.metrics.active_state = "idle"
            self.metrics.jobs_retried += 1
            log.warning(
                "job_lookup_transient",
                extra={
                    "event_fields": {
                        **message.error_context(),
                        "error": type(exc).__name__,
                    }
                },
            )
            return ProcessOutcome(
                "transient", job_id=message.job_id, message_id=message_id
            )

        try:
            claim = self.store.claim_job(
                job_id=message.job_id,
                run_id=message.run_id,
                backend=message.backend,
                detector_config_hash=message.detector["config_hash"],
                artifact_id=message.execution["artifact_id"],
                worker_id=self.settings.worker_id,
                attempt_id=uuid.uuid4().hex,
                lease_expires_at=lease_expiry(self.settings.visibility_timeout),
            )
        except StateTransitionError:
            # Terminal job: a crash after commit left this delivery unacked.
            self.metrics.messages_ignored += 1
            self._ack(receipt)
            log.info(
                "worker_duplicate_terminal",
                extra={"event_fields": message.error_context()},
            )
            return ProcessOutcome(
                "duplicate", job_id=message.job_id, message_id=message_id
            )
        except ClaimError as exc:
            current = self.store.get_job(message.job_id) or {}
            status = str(current.get("status", ""))
            if status in TERMINAL_STATES or status == RUNNING:
                self.metrics.messages_ignored += 1
                self._ack(receipt)
                return ProcessOutcome(
                    "duplicate", job_id=message.job_id, message_id=message_id,
                    details={"status": status, "reason": exc.code},
                )
            return ProcessOutcome(
                "claim_conflict", job_id=message.job_id, message_id=message_id,
                details={"status": status},
            )
        except (ClientError, EndpointConnectionError, OSError) as exc:
            self.metrics.active_state = "idle"
            self.metrics.jobs_retried += 1
            log.warning(
                "job_claim_transient",
                extra={
                    "event_fields": {
                        **message.error_context(),
                        "error": type(exc).__name__,
                    }
                },
            )
            return ProcessOutcome(
                "transient", job_id=message.job_id, message_id=message_id
            )

        return self._execute_claim(message, job, run, claim, receipt, receive_count)

    def _execute_claim(
        self,
        message: JobMessage,
        job: dict[str, Any],
        run: dict[str, Any] | None,
        claim: dict[str, Any],
        receipt: str,
        receive_count: int,
    ) -> ProcessOutcome:
        token = int(claim["fencing_token"])
        attempt_id = str(claim["attempt_id"])
        error_context = {
            **message.error_context(),
            "worker_id": self.settings.worker_id,
            "attempt_id": attempt_id,
            "fencing_token": token,
        }
        lease = Lease(
            store=self.store,
            settings=self.settings,
            metrics=self.metrics,
            queue_url=self.queue_url,
            receipt_handle=receipt,
            job_id=message.job_id,
            fencing_token=token,
            attempt_id=attempt_id,
            on_loss=self.pause,
        )
        lease.start()
        log.info("job_started", extra={"event_fields": error_context})
        try:
            lease.check()
            artifact = self.verifier.verify(message.execution["artifact_id"])
            job_input = load_job_input(self.store, message, job)
            self.metrics.active_state = "running"
            output = self.executor(
                message,
                job,
                run,
                job_input.data,
                artifact,
                input_info=job_input.as_dict(),
            )
            if not isinstance(output, ExecutionResult):
                raise PermanentError(
                    "executor returned an unsupported result type",
                    code="invalid_executor_result",
                )
            self._validate_alerts(output.alerts)
            lease.check()
            self.metrics.active_state = "committing"
            outcome = commit_result(
                self.store,
                settings=self.settings,
                message=message,
                job=job,
                execution=output,
                attempt_id=attempt_id,
                input_info=job_input.as_dict(),
                fencing_token=token,
                lease=lease,
            )
            if not outcome.committed:
                self.metrics.messages_ignored += 1
                self._ack(receipt)
                return ProcessOutcome(
                    "duplicate",
                    job_id=message.job_id,
                    details={"reason": "already_committed"},
                )
            self.metrics.jobs_processed += 1
            if outcome.run_finalized:
                self.metrics.run_completions += 1
            self.metrics.alerts_written += outcome.alerts_written
            self._ack(receipt)
            log.info(
                "job_committed",
                extra={
                    "event_fields": {
                        **error_context,
                        "backend": output.backend,
                        "result": outcome.result_key,
                        "alerts": len(output.alerts),
                    }
                },
            )
            return ProcessOutcome(
                "committed",
                job_id=message.job_id,
                details={
                    "attempt_id": attempt_id,
                    "result_key": outcome.result_key,
                    "run_finalized": outcome.run_finalized,
                },
            )
        except LeaseError as exc:
            # No commit, no failure write from a stale worker: redelivery will
            # claim a fresh attempt once the previous lease has expired.
            self.metrics.last_error = exc.code
            self.metrics.active_state = "idle"
            log.warning(
                "worker_lease_abandoned",
                extra={"event_fields": {**error_context, "reason": exc.code}},
            )
            return ProcessOutcome(
                "lease_lost", job_id=message.job_id, details={"error": exc.code}
            )
        except PermanentError as exc:
            self._fail_and_quarantine(
                message, receipt, exc, token, attempt_id, job, receive_count
            )
            return ProcessOutcome(
                "dlq", job_id=message.job_id, details={"error": exc.code}
            )
        except TransientError as exc:
            attempts = int(claim.get("attempts", 1))
            if attempts >= self.settings.max_transient_retries + 1:
                error = PermanentError(
                    "transient failure budget exhausted",
                    code="retry_exhausted",
                    details={"attempts": attempts, "last_error": exc.as_dict()},
                )
                self._fail_and_quarantine(
                    message, receipt, error, token, attempt_id, job, receive_count
                )
                return ProcessOutcome(
                    "dlq", job_id=message.job_id, details={"error": error.code}
                )
            self._retry(message, receipt, exc, token, attempt_id, attempts)
            return ProcessOutcome(
                "retryable", job_id=message.job_id, details={"error": exc.code}
            )
        except (ClientError, EndpointConnectionError, OSError) as exc:
            transient = TransientError(
                "unexpected AWS failure",
                details={"error": type(exc).__name__},
            )
            self._retry(
                message, receipt, transient, token, attempt_id,
                int(claim.get("attempts", 1)),
            )
            return ProcessOutcome(
                "retryable", job_id=message.job_id, details={"error": transient.code}
            )
        except Exception as exc:  # noqa: BLE001 - unknown failures must not ack
            permanent = PermanentError(
                f"unexpected worker failure: {type(exc).__name__}",
                code="unexpected_worker_failure",
                details={"error_type": type(exc).__name__},
            )
            self._fail_and_quarantine(
                message, receipt, permanent, token, attempt_id, job, receive_count
            )
            return ProcessOutcome(
                "dlq", job_id=message.job_id, details={"error": permanent.code}
            )
        finally:
            self.metrics.lease_age_seconds = lease.age_seconds
            if lease.lost:
                self.metrics.lease_age_seconds = 0.0
            self.metrics.heartbeat_age_seconds = lease.heartbeat_age_seconds
            lease.close()
            self.metrics.active_state = "idle"

    # -- terminal handling --------------------------------------------------
    @staticmethod
    def _validate_alerts(alerts: list[dict[str, Any]]) -> None:
        seen: set[str] = set()
        for alert in alerts:
            alert_id = str(alert.get("alert_id") or "")
            if not alert_id:
                raise PermanentError(
                    "detector produced an alert without an id",
                    code="invalid_alert",
                )
            if alert_id in seen:
                raise PermanentError(
                    "detector produced duplicate alert ids",
                    code="duplicate_alert_id",
                    details={"alert_id": alert_id},
                )
            seen.add(alert_id)

    def _ack(self, receipt: str) -> None:
        try:
            self.store.sqs.delete_message(
                QueueUrl=self.queue_url, ReceiptHandle=receipt
            )
        except (ClientError, EndpointConnectionError, OSError) as exc:  # pragma: no cover
            log.warning(
                "sqs_ack_failed",
                extra={"event_fields": {"error": type(exc).__name__}},
            )

    def _retry(
        self,
        message: JobMessage,
        receipt: str,
        error: TransientError,
        token: int,
        attempt_id: str,
        attempts: int,
    ) -> None:
        self.metrics.jobs_retried += 1
        self.metrics.last_error = error.code
        try:
            self.store.fail_job(
                message.job_id,
                fencing_token=token,
                attempt_id=attempt_id,
                status=RETRYABLE_FAILED,
                error=error.as_dict(),
            )
        except (StaleTokenError, ClientError, EndpointConnectionError) as exc:
            log.warning(
                "retry_state_update_failed",
                extra={"event_fields": {**message.error_context(), "error": str(exc)}},
            )
        delay = min(
            self.settings.retry_base_seconds * (2 ** max(attempts - 1, 0)),
            self.settings.retry_max_seconds,
        )
        delay *= 0.75 + random.random() * 0.5
        log.warning(
            "job_retryable",
            extra={
                "event_fields": {
                    **message.error_context(),
                    "attempt": attempts,
                    "delay_seconds": round(delay, 3),
                    "error": error.code,
                }
            },
        )
        time.sleep(delay)

    def _fail_and_quarantine(
        self,
        message: JobMessage,
        receipt: str,
        error: PermanentError,
        token: int,
        attempt_id: str,
        job: dict[str, Any],
        receive_count: int,
    ) -> None:
        self.metrics.last_error = error.code
        error_payload = {
            **error.as_dict(),
            "phase": "execute",
            "worker_id": self.settings.worker_id,
            "attempt_id": attempt_id,
            "fencing_token": token,
            "failed_at": utcnow(),
            "receive_count": receive_count,
        }
        try:
            self.store.fail_job(
                message.job_id,
                fencing_token=token,
                attempt_id=attempt_id,
                status=DEAD_LETTERED,
                error=error_payload,
            )
        except (StaleTokenError, ClientError, EndpointConnectionError) as exc:
            log.warning(
                "failure_state_update_failed",
                extra={"event_fields": {**message.error_context(), "error": str(exc)}},
            )
        self._quarantine(
            message.raw,
            receipt,
            error,
            receive_count,
            phase="execute",
            job=job,
            attempt_id=attempt_id,
            token=token,
            error_payload=error_payload,
        )

    def _quarantine(
        self,
        raw: dict[str, Any] | None,
        receipt: str,
        error: WorkerError,
        receive_count: int,
        *,
        phase: str,
        job: dict[str, Any] | None = None,
        attempt_id: str | None = None,
        token: int | None = None,
        error_payload: dict[str, Any] | None = None,
    ) -> None:
        """Send a structured poison message to this capability's DLQ, then ack."""
        body = dict(raw or {})
        if isinstance(body.get("raw_body"), str):
            body["raw_body"] = body["raw_body"][:2048]
        job_id = str((job or {}).get("job_id") or body.get("job_id") or "")
        run_id = str((job or {}).get("run_id") or body.get("run_id") or "")
        body["last_error"] = error_payload or {
            **error.as_dict(),
            "phase": phase,
            "worker_id": self.settings.worker_id,
            "attempt_id": attempt_id,
            "fencing_token": token,
            "failed_at": utcnow(),
            "receive_count": receive_count,
        }
        body["dlq_metadata"] = {
            "backend": self.settings.capability,
            "enqueued_at": utcnow(),
            "reason": error.code,
            "logical_job_id": job_id or None,
        }
        if job_id:
            try:
                self.store.dead_letter_job(job_id, error=body["last_error"])
            except (StateTransitionError, ClientError, EndpointConnectionError):
                pass
        try:
            self.store.send_dlq(self.settings.capability, body)
            self.metrics.dlq_sends += 1
        except (ClientError, EndpointConnectionError, OSError) as exc:  # pragma: no cover
            log.warning(
                "dlq_send_failed",
                extra={"event_fields": {"job_id": job_id, "error": type(exc).__name__}},
            )
            return  # keep the message for SQS redrive
        self._ack(receipt)
        log.warning(
            "worker_quarantined",
            extra={
                "event_fields": {
                    "job_id": job_id,
                    "run_id": run_id,
                    "error": error.code,
                    "phase": phase,
                }
            },
        )

    def _install_signals(self) -> None:
        def stop(_signum: int, _frame: Any) -> None:
            self._stop = True

        try:
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
        except ValueError:  # pragma: no cover - only valid in the main thread
            pass
