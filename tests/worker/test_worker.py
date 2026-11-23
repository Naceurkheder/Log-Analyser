"""End-to-end worker behavior and failure injection."""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any

import pytest

from sentinel.api.store import from_item
from sentinel.detector.native import NativeResult
from sentinel.detector.window_native import WindowAlert, WindowResult
from sentinel.worker.config import WorkerSettings
from sentinel.worker.dlq import inspect_dlq, replay_dlq
from sentinel.worker.errors import MessageError, TransientError
from sentinel.worker.execution import ExecutionResult
from sentinel.worker.lease import Lease
from sentinel.worker.metrics import WorkerMetrics
from sentinel.worker.reconcile import reconcile_outbox
from sentinel.worker.state import DEAD_LETTERED, RETRYABLE_FAILED
from sentinel.worker.store import WorkerStore
from sentinel.worker.worker import Worker

from .conftest import drain_queue, get_job_item, get_run_item


def _worker(settings: Any, store: Any, **kwargs: Any) -> Worker:
    return Worker(settings, store, start_metrics=False, **kwargs)


def _result_objects(aws: Any) -> list[str]:
    response = aws.s3.list_objects_v2(Bucket="results", Prefix="runs/")
    return [str(item["Key"]) for item in response.get("Contents", [])]


def test_commit_order_result_alerts_job_run_and_duplicate_noop(
    aws: Any,
    worker: Worker,
    fake_executor: Any,
    enqueue: Any,
    make_run: Any,
    make_job: Any,
    alert: Any,
) -> None:
    run = make_run()
    fixture = make_job(run)
    fake_executor([alert(run["run_id"], fixture.job["chunk_id"])])
    enqueue(fixture.message)

    outcome = worker.process_once()
    assert outcome.status == "committed"
    assert worker.metrics.jobs_processed == 1

    job = get_job_item(aws, fixture.job["job_id"])
    assert job["status"] == "SUCCEEDED"
    assert job["actual_backend"] == "cpu"
    assert job["result"]["sha256"] == job["result_sha256"]
    keys = _result_objects(aws)
    assert len(keys) == 1
    assert keys[0] == job["result_key"]
    head = aws.s3.head_object(Bucket="results", Key=keys[0])
    assert head["Metadata"]["sha256"] == job["result_sha256"]
    assert head["ContentLength"] > 0
    assert aws.dynamodb.scan(TableName="SentinelAlerts")["Count"] == 1
    run_item = get_run_item(aws, run["run_id"])
    assert run_item["status"] == "SUCCEEDED"
    assert run_item["jobs_completed"] == 1
    assert run_item["alerts_total"] == 1
    assert drain_queue(aws, "jobs-cpu") == []

    # Crash after commit / duplicate delivery: terminal job is a no-op ack.
    enqueue(fixture.message)
    second = worker.process_once()
    assert second.status == "duplicate"
    assert aws.dynamodb.scan(TableName="SentinelAlerts")["Count"] == 1
    assert len(_result_objects(aws)) == 1
    assert get_run_item(aws, run["run_id"])["status"] == "SUCCEEDED"
    assert worker.metrics.jobs_processed == 1
    assert drain_queue(aws, "jobs-cpu") == []


def test_two_copies_in_one_batch_are_both_acked(
    aws: Any, worker: Worker, fake_executor: Any, enqueue: Any, make_run: Any, make_job: Any
) -> None:
    run = make_run()
    fixture = make_job(run)
    fake_executor([])
    enqueue(fixture.message)
    enqueue(fixture.message)
    assert worker.process_once().status == "committed"
    assert worker.process_once().status == "duplicate"
    assert worker.metrics.jobs_processed == 1
    assert worker.metrics.messages_ignored >= 1
    assert drain_queue(aws, "jobs-cpu") == []


def test_worker_kill_before_commit_retries_safely(
    aws: Any, worker: Worker, fake_executor: Any, monkeypatch: pytest.MonkeyPatch,
    enqueue: Any, make_run: Any, make_job: Any,
) -> None:
    run = make_run()
    fixture = make_job(run)

    def abandon(*args: Any, **kwargs: Any) -> ExecutionResult:
        raise TransientError("simulated worker kill before commit")

    monkeypatch.setattr("sentinel.worker.worker.execute_job", abandon)
    enqueue(fixture.message)
    assert worker.process_once().status == "retryable"
    aborted = get_job_item(aws, fixture.job["job_id"])
    assert aborted["status"] == RETRYABLE_FAILED
    assert aborted["attempts"] == 1
    assert _result_objects(aws) == []

    fake_executor([])
    enqueue(fixture.message)
    assert worker.process_once().status == "committed"
    recovered = get_job_item(aws, fixture.job["job_id"])
    assert recovered["status"] == "SUCCEEDED"
    assert recovered["attempts"] == 2
    assert recovered["fencing_token"] == 2
    assert len(_result_objects(aws)) == 1


def test_stale_token_from_old_attempt_cannot_commit(
    aws: Any, store: Any, worker: Worker, fake_executor: Any, enqueue: Any,
    make_run: Any, make_job: Any,
) -> None:
    run = make_run()
    fixture = make_job(run)
    old = store.claim_job(
        job_id=fixture.job["job_id"],
        run_id=run["run_id"],
        backend="cpu",
        detector_config_hash=fixture.job["detector_config_hash"],
        artifact_id=fixture.job["artifact_id"],
        worker_id="old-worker",
        attempt_id=uuid.uuid4().hex,
        lease_expires_at="2099-01-01T00:00:00.000Z",
    )
    # A newer attempt has taken ownership; the old token is now stale.
    aws.dynamodb.update_item(
        TableName="SentinelJobs",
        Key={"job_id": {"S": fixture.job["job_id"]}},
        UpdateExpression="SET fencing_token = :token",
        ExpressionAttributeValues={":token": {"N": "9"}},
    )
    fake_executor([])
    enqueue(fixture.message)
    outcome = worker.process_once()
    assert outcome.status == "duplicate"
    assert get_job_item(aws, fixture.job["job_id"])["status"] == "RUNNING"
    assert get_job_item(aws, fixture.job["job_id"])["attempt_id"] == old["attempt_id"]
    assert _result_objects(aws) == []
    assert drain_queue(aws, "jobs-cpu") == []


def test_visibility_loss_forbids_commit_and_pauses_worker(
    aws: Any, worker: Worker, monkeypatch: pytest.MonkeyPatch,
    enqueue: Any, make_run: Any, make_job: Any,
) -> None:
    run = make_run()
    fixture = make_job(run)
    leases: list[Lease] = []

    class CapturingLease(Lease):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            leases.append(self)

    monkeypatch.setattr("sentinel.worker.worker.Lease", CapturingLease)

    def lose_lease_mid_job(*args: Any, **kwargs: Any) -> ExecutionResult:
        leases[0].force_loss("visibility_expired_injection")
        return ExecutionResult(alerts=[], backend="cpu")

    monkeypatch.setattr("sentinel.worker.worker.execute_job", lose_lease_mid_job)
    enqueue(fixture.message)
    outcome = worker.process_once()
    assert outcome.status == "lease_lost"
    assert worker.accepting_work is False
    assert worker.metrics.lease_losses == 1
    job = get_job_item(aws, fixture.job["job_id"])
    assert job["status"] == RETRYABLE_FAILED
    assert job["lease_loss_reason"] == "visibility_expired_injection"
    assert _result_objects(aws) == []
    # Never ack a job whose lease was lost.
    assert len(drain_queue(aws, "jobs-cpu")) == 1


def test_corrupt_chunk_is_quarantined_with_structured_error(
    aws: Any, worker: Worker, fake_executor: Any, enqueue: Any, make_run: Any, make_job: Any
) -> None:
    run = make_run()
    fixture = make_job(run, corrupt_chunk=True)
    fake_executor([])
    enqueue(fixture.message)
    outcome = worker.process_once()
    assert outcome.status == "dlq"
    job = get_job_item(aws, fixture.job["job_id"])
    assert job["status"] == DEAD_LETTERED
    assert job["last_error"]["code"] == "checksum_mismatch"
    assert job["last_error"]["phase"] == "execute"
    dlq = drain_queue(aws, "jobs-cpu-dlq")
    assert len(dlq) == 1
    body = json.loads(dlq[0]["Body"])
    assert body["last_error"]["code"] == "checksum_mismatch"
    assert body["dlq_metadata"]["logical_job_id"] == fixture.job["job_id"]
    assert drain_queue(aws, "jobs-cpu") == []
    assert worker.metrics.dlq_sends == 1


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda m: m.update(schema_version=99), "unsupported_schema_version"),
        (lambda m: m.update(message_type="ingest"), "invalid_message_type"),
        (lambda m: m.update(backend="gpu"), "backend_capability_mismatch"),
    ],
)
def test_malformed_messages_go_to_dlq_without_durable_work(
    aws: Any, worker: Worker, enqueue: Any, mutate: Any, code: str
) -> None:
    from .conftest import DETECTOR

    message = {
        "schema_version": 1,
        "message_type": "job",
        "run_id": "run_" + "a" * 32,
        "job_id": "job-" + "b" * 20,
        "chunk_id": "chk-" + "c" * 16,
        "backend": "cpu",
        "chunk": {
            "bucket": "raw-chunks", "key": "x", "sha256": "0" * 64,
            "size_bytes": 1, "line_count": 1, "line_number_start": 1,
            "line_number_end": 1,
        },
        "manifest": {"bucket": "raw-chunks", "key": "m", "sha256": "0" * 64},
        "detector": {"config_hash": "0" * 64, "parser_version": "v1"},
        "execution": {"backend": "cpu", "artifact_id": "cpu-ref-v1"},
    }
    mutate(message)
    enqueue(message)
    assert worker.process_once().status == "dlq"
    assert aws.dynamodb.scan(TableName="SentinelJobs")["Count"] == 0
    dlq = drain_queue(aws, "jobs-cpu-dlq")
    assert len(dlq) == 1
    assert json.loads(dlq[0]["Body"])["last_error"]["code"] == code
    assert drain_queue(aws, "jobs-cpu") == []


def test_inspect_and_replay_require_reason_and_respect_budget(
    aws: Any, settings: Any, store: Any, make_run: Any, make_job: Any
) -> None:
    run = make_run()
    fixture = make_job(run, status=DEAD_LETTERED)
    body = dict(fixture.message)
    body["last_error"] = {"code": "poison", "message": "bad chunk"}
    body["replay"] = {"count": 0}
    aws.sqs.send_message(
        QueueUrl=aws.urls["jobs-cpu-dlq"], MessageBody=json.dumps(body)
    )

    inspected = inspect_dlq(store, settings, "cpu")
    assert len(inspected) == 1
    assert inspected[0]["job_id"] == fixture.job["job_id"]
    assert inspected[0]["error"]["code"] == "poison"
    # Inspection must not consume the message.
    assert len(inspect_dlq(store, settings, "cpu")) == 1

    with pytest.raises(MessageError) as exc:
        replay_dlq(store, settings, "cpu", reason="  ")
    assert exc.value.code == "replay_reason_required"

    report = replay_dlq(
        store, settings, "cpu", reason="rebuilt corrupt chunk", limit=5
    )
    assert report.replayed == 1
    assert report.jobs_requeued == 1
    assert get_job_item(aws, fixture.job["job_id"])["status"] == "QUEUED"
    requeued = drain_queue(aws, "jobs-cpu")
    assert len(requeued) == 1
    replayed = json.loads(requeued[0]["Body"])
    assert replayed["job_id"] == fixture.job["job_id"]
    assert replayed["replay"]["count"] == 1
    assert replayed["replay"]["reason"] == "rebuilt corrupt chunk"
    assert "last_error" not in replayed

    aws.sqs.send_message(
        QueueUrl=aws.urls["jobs-cpu-dlq"], MessageBody=json.dumps(replayed | {
            "replay": {"count": 1}, "last_error": {"code": "poison"}
        })
    )
    guarded = replay_dlq(store, settings, "cpu", reason="try again")
    assert guarded.replayed == 0
    assert guarded.skipped[0]["reason"] == "replay_budget_exhausted"
    assert drain_queue(aws, "jobs-cpu") == []


def test_outbox_reconciler_republishes_identical_logical_job_and_ingest(
    aws: Any, settings: Any, store: Any, worker: Worker, fake_executor: Any,
    make_run: Any, make_job: Any,
) -> None:
    run = make_run(ingest_published=False)
    fixture = make_job(run, published=False)
    report = reconcile_outbox(store, settings)
    assert report.jobs_republished == 1
    assert report.runs_republished == 1
    assert report.oldest_pending_age_seconds >= 0

    jobs = drain_queue(aws, "jobs-cpu")
    assert len(jobs) == 1
    assert json.loads(jobs[0]["Body"])["job_id"] == fixture.job["job_id"]
    ingests = drain_queue(aws, "ingest-queue")
    assert len(ingests) == 1
    assert json.loads(ingests[0]["Body"])["run_id"] == run["run_id"]
    assert get_job_item(aws, fixture.job["job_id"])["published"] is True
    assert get_run_item(aws, run["run_id"])["ingest_published"] is True
    assert reconcile_outbox(store, settings).jobs_republished == 0

    fake_executor([])
    enqueued_again = jobs[0]
    for _ in range(2):
        aws.sqs.send_message(
            QueueUrl=aws.urls["jobs-cpu"], MessageBody=enqueued_again["Body"]
        )
    assert worker.process_once().status == "committed"
    assert worker.process_once().status == "duplicate"
    assert worker.process_once() is None
    assert get_job_item(aws, fixture.job["job_id"])["status"] == "SUCCEEDED"
    assert aws.dynamodb.scan(TableName="SentinelAlerts")["Count"] == 0


def test_publish_then_crash_duplicate_is_harmless(
    aws: Any, settings: Any, store: Any, worker: Worker, fake_executor: Any,
    enqueue: Any, make_run: Any, make_job: Any,
) -> None:
    run = make_run()
    fixture = make_job(run, published=False)
    enqueue(fixture.message)  # sent before the crash, before published was set
    reconcile_outbox(store, settings)
    assert len(drain_queue(aws, "jobs-cpu")) == 2
    fake_executor([])
    aws.sqs.send_message(
        QueueUrl=aws.urls["jobs-cpu"], MessageBody=json.dumps(fixture.message)
    )
    aws.sqs.send_message(
        QueueUrl=aws.urls["jobs-cpu"], MessageBody=json.dumps(fixture.message)
    )
    assert worker.process_once().status == "committed"
    assert worker.process_once().status == "duplicate"
    assert worker.process_once() is None
    assert len(_result_objects(aws)) == 1
    assert get_run_item(aws, run["run_id"])["jobs_completed"] == 1


def test_gpu_worker_calls_native_abi_and_never_falls_back(
    aws: Any, gpu_settings: Any, monkeypatch: pytest.MonkeyPatch,
    make_run: Any, make_job: Any,
) -> None:
    calls: dict[str, int] = {"k1": 0, "k2": 0}

    def fake_native(*args: Any, **kwargs: Any) -> NativeResult:
        assert kwargs.get("backend") == "gpu"
        calls["k1"] += 1
        return NativeResult(
            line_count=4, line_flags=(0, 0, 0, 0), anomalies=(),
            cells=0, mean=0.0, std=0.0,
        )

    def fake_window(*args: Any, **kwargs: Any) -> WindowResult:
        calls["k2"] += 1
        return WindowResult(features=(), alerts=(), timing={})

    monkeypatch.setattr("sentinel.worker.execution.detect_native", fake_native)
    monkeypatch.setattr("sentinel.worker.execution.window_native.detect_window", fake_window)
    monkeypatch.setattr("sentinel.worker.execution.detect", lambda *a, **k: pytest.fail("CPU fallback"))
    monkeypatch.setattr(
        "sentinel.worker.artifact.load_gpu_library",
        lambda path: __import__("types").SimpleNamespace(sentinel_abi_version=lambda: 2),
    )
    monkeypatch.setattr("sentinel.worker.artifact.assert_abi", lambda lib: "gpu-build-1")

    run = make_run(backend_policy="gpu")
    fixture = make_job(run, backend="gpu", artifact_id="gpu-v1")
    gpu_store = WorkerStore(gpu_settings.api)
    worker = _worker(gpu_settings, gpu_store, metrics=WorkerMetrics("gpu", "gpu-worker"))
    aws.sqs.send_message(
        QueueUrl=aws.urls["jobs-gpu"], MessageBody=json.dumps(fixture.message)
    )
    outcome = worker.process_once()
    assert outcome.status == "committed"
    assert calls == {"k1": 1, "k2": 1}
    assert get_job_item(aws, fixture.job["job_id"])["actual_backend"] == "gpu"


def test_gpu_unavailable_is_quarantined_not_fallback(
    aws: Any, gpu_settings: Any, monkeypatch: pytest.MonkeyPatch,
    make_run: Any, make_job: Any,
) -> None:
    def broken(path: str) -> None:
        raise OSError("no CUDA device")

    monkeypatch.setattr("sentinel.worker.artifact.load_gpu_library", broken)
    monkeypatch.setattr(
        "sentinel.worker.execution.detect", lambda *a, **k: pytest.fail("CPU fallback")
    )
    run = make_run(backend_policy="gpu")
    fixture = make_job(run, backend="gpu", artifact_id="gpu-v1")
    worker = _worker(gpu_settings, WorkerStore(gpu_settings.api))
    aws.sqs.send_message(
        QueueUrl=aws.urls["jobs-gpu"], MessageBody=json.dumps(fixture.message)
    )
    assert worker.process_once().status == "dlq"
    dlq = drain_queue(aws, "jobs-gpu-dlq")
    assert len(dlq) == 1
    assert json.loads(dlq[0]["Body"])["last_error"]["code"] == "gpu_unavailable"
    assert get_job_item(aws, fixture.job["job_id"])["status"] == DEAD_LETTERED


def test_capability_queues_are_isolated(
    aws: Any, settings: Any, gpu_settings: Any, store: Any, make_run: Any, make_job: Any
) -> None:
    run = make_run(backend_policy="gpu")
    gpu_job = make_job(run, backend="gpu", artifact_id="gpu-v1")
    cpu_worker = _worker(settings, store)
    gpu_worker = _worker(gpu_settings, WorkerStore(gpu_settings.api))
    assert cpu_worker.queue_url.endswith("/jobs-cpu")
    assert gpu_worker.queue_url.endswith("/jobs-gpu")
    aws.sqs.send_message(
        QueueUrl=aws.urls["jobs-gpu"], MessageBody=json.dumps(gpu_job.message)
    )
    # The CPU worker cannot even see the GPU queue.
    assert cpu_worker.process_once() is None
    assert get_job_item(aws, gpu_job.job["job_id"])["status"] == "PENDING"
