"""moto-backed plane and deterministic helpers for worker tests."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import boto3
import pytest
from moto import mock_aws

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sentinel.api.contract import utcnow  # noqa: E402
from sentinel.ids import (  # noqa: E402
    alert_id_for,
    canonical_json,
    chunk_id_for,
    config_hash,
    job_id_for,
    run_id_for,
    sha256_hex,
)
from sentinel.worker.execution import ExecutionResult  # noqa: E402

DETECTOR: dict[str, Any] = {
    "rules": [
        {"rule_id": "auth-failed", "pattern": "Failed password", "severity": "high"}
    ],
    "window_seconds": 60,
    "baseline_windows": 10,
    "z_threshold": 3.0,
    "max_line_bytes": 65536,
}
CHUNK_BODY = (
    b"2026-11-20T10:00:00Z host sshd[1]: Failed password for root\n"
    b"2026-11-20T10:01:00Z host sshd[1]: Failed password for root\n"
    b"2026-11-20T10:02:00Z host sshd[1]: Failed password for root\n"
    b"2026-11-20T10:03:00Z host sshd[1]: Failed password for root\n"
)
ARTIFACT_BYTES = b"fake-native-artifact-v2"


def create_tables(client: Any) -> None:
    client.create_table(
        TableName="SentinelRuns",
        KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    for name, item_key in (("SentinelJobs", "job_id"), ("SentinelAlerts", "alert_id")):
        client.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": item_key, "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": item_key, "AttributeType": "S"},
                {"AttributeName": "run_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "run_id-index",
                    "KeySchema": [
                        {"AttributeName": "run_id", "KeyType": "HASH"},
                        {"AttributeName": item_key, "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )


@pytest.fixture()
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    with mock_aws():
        region = "us-east-1"
        dynamodb = boto3.client("dynamodb", region_name=region)
        create_tables(dynamodb)
        s3 = boto3.client("s3", region_name=region)
        for bucket in ("incoming", "raw-chunks", "artifacts", "results"):
            s3.create_bucket(Bucket=bucket)
        sqs = boto3.client("sqs", region_name=region)
        urls: dict[str, str] = {}
        for name in ("ingest-queue", "jobs-gpu", "jobs-cpu"):
            # Zero visibility in tests makes redelivery immediate; production
            # uses the Terraform value and worker heartbeats extend it.
            urls[name] = sqs.create_queue(
                QueueName=name, Attributes={"VisibilityTimeout": "0"}
            )["QueueUrl"]
        for name in ("jobs-gpu", "jobs-cpu"):
            arn = sqs.get_queue_attributes(
                QueueUrl=urls[name], AttributeNames=["QueueArn"]
            )["Attributes"]["QueueArn"]
            sqs.set_queue_attributes(
                QueueUrl=urls[name],
                Attributes={
                    "RedrivePolicy": json.dumps(
                        {"deadLetterTargetArn": arn, "maxReceiveCount": 3}
                    )
                },
            )
            urls[name + "-dlq"] = sqs.create_queue(QueueName=name + "-dlq")["QueueUrl"]

        for key, value in {
            "AWS_ENDPOINT_URL": "",
            "AWS_REGION": region,
            "AWS_DEFAULT_REGION": region,
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "SENTINEL_RUNS_TABLE": "SentinelRuns",
            "SENTINEL_JOBS_TABLE": "SentinelJobs",
            "SENTINEL_ALERTS_TABLE": "SentinelAlerts",
            "SENTINEL_INCOMING_BUCKET": "incoming",
            "SENTINEL_RAW_CHUNKS_BUCKET": "raw-chunks",
            "SENTINEL_RESULTS_BUCKET": "results",
            "SENTINEL_INGEST_QUEUE": "ingest-queue",
            "SENTINEL_JOBS_CPU_QUEUE": "jobs-cpu",
            "SENTINEL_JOBS_GPU_QUEUE": "jobs-gpu",
            "SENTINEL_WORKER_HEARTBEAT_INTERVAL": "1",
            "SENTINEL_WORKER_LEASE_MARGIN_SECONDS": "1",
            "SENTINEL_WORKER_VISIBILITY_TIMEOUT": "4",
            "SENTINEL_WORKER_WAIT_SECONDS": "0",
            "SENTINEL_WORKER_RETRY_BASE_SECONDS": "0.001",
            "SENTINEL_WORKER_RETRY_MAX_SECONDS": "0.002",
            "SENTINEL_WORKER_MAX_RETRIES": "1",
            "SENTINEL_WORKER_RECONCILE_MIN_AGE_SECONDS": "0",
            "SENTINEL_LOG_LEVEL": "WARNING",
        }.items():
            monkeypatch.setenv(key, value)
        yield SimpleNamespace(
            region=region,
            dynamodb=dynamodb,
            s3=s3,
            sqs=sqs,
            urls=urls,
        )


@pytest.fixture()
def artifact_file(aws: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "libsentinel_cpu.so"
    path.write_bytes(ARTIFACT_BYTES)
    monkeypatch.setenv("SENTINEL_WORKER_ARTIFACT_PATH", str(path))
    monkeypatch.setenv("SENTINEL_WORKER_ARTIFACT_SHA256", sha256_hex(ARTIFACT_BYTES))
    return path


@pytest.fixture()
def settings(aws: SimpleNamespace, artifact_file: Path) -> Any:
    from sentinel.worker.config import WorkerSettings

    return WorkerSettings.from_env("cpu")


@pytest.fixture()
def gpu_settings(aws: SimpleNamespace, artifact_file: Path) -> Any:
    from sentinel.worker.config import WorkerSettings

    return WorkerSettings.from_env("gpu")


@pytest.fixture()
def store(aws: SimpleNamespace, settings: Any) -> Any:
    from sentinel.worker.store import WorkerStore

    return WorkerStore(settings.api)


@dataclass
class JobFixture:
    run: dict[str, Any]
    job: dict[str, Any]
    message: dict[str, Any]
    body: bytes
    chunk_key: str
    manifest_key: str
    detector_hash: str

    @property
    def alert_id(self) -> str:
        return alert_id_for(
            self.run["run_id"], self.job["chunk_id"], 1, 0, 2, "auth-failed"
        )


@pytest.fixture()
def make_run(aws: SimpleNamespace) -> Callable[..., dict[str, Any]]:
    def _make(run_id: str | None = None, **overrides: Any) -> dict[str, Any]:
        rid = run_id or run_id_for("worker-test-key")
        now = utcnow()
        run: dict[str, Any] = {
            "run_id": rid,
            "schema_version": 1,
            "status": "RUNNING",
            "backend_policy": "cpu",
            "created_at": now,
            "updated_at": now,
            "dataset": {"bucket": "incoming", "key": "datasets/sample.log"},
            "detector": DETECTOR,
            "detector_config_hash": config_hash(DETECTOR),
            "request_hash": "request-hash",
            "jobs_total": 1,
            "jobs_completed": 0,
            "jobs_failed": 0,
            "alerts_total": 0,
            "ingest_published": True,
        }
        run.update(overrides)
        aws.dynamodb.put_item(
            TableName="SentinelRuns",
            Item=_to_item(run),
        )
        return run

    return _make


@pytest.fixture()
def make_job(aws: SimpleNamespace, settings: Any) -> Callable[..., JobFixture]:
    def _make(
        run: dict[str, Any],
        *,
        backend: str = "cpu",
        body: bytes | None = None,
        status: str = "PENDING",
        corrupt_chunk: bool = False,
        published: bool = True,
        artifact_id: str = "cpu-ref-v1",
    ) -> JobFixture:
        raw = body if body is not None else CHUNK_BODY
        chunk_sha = sha256_hex(raw)
        chunk_id = chunk_id_for(run["run_id"], 0, chunk_sha)
        chunk_key = f"chunks/{run['run_id']}/{chunk_id}/data.bin"
        manifest_key = f"chunks/{run['run_id']}/{chunk_id}/manifest.json"
        aws.s3.put_object(Bucket="raw-chunks", Key=chunk_key, Body=raw)
        manifest = {
            "schema_version": 1,
            "manifest_type": "chunk",
            "run_id": run["run_id"],
            "chunk_id": chunk_id,
            "detector_config_hash": run["detector_config_hash"],
            "source": {
                "bucket": "incoming",
                "key": "datasets/sample.log",
                "sha256": chunk_sha,
            },
            "chunk": {
                "bucket": "raw-chunks",
                "key": chunk_key,
                "sha256": chunk_sha,
                "size_bytes": len(raw),
                "line_count": 4,
                "line_number_start": 1,
                "line_number_end": 4,
            },
        }
        manifest_bytes = canonical_json(manifest)
        aws.s3.put_object(
            Bucket="raw-chunks",
            Key=manifest_key,
            Body=manifest_bytes,
            Metadata={"sha256": sha256_hex(manifest_bytes)},
        )
        job_id = job_id_for(
            run["run_id"], chunk_id, run["detector_config_hash"], artifact_id
        )
        now = utcnow()
        job: dict[str, Any] = {
            "job_id": job_id,
            "run_id": run["run_id"],
            "schema_version": 1,
            "status": status,
            "backend": backend,
            "chunk_id": chunk_id,
            "detector_config_hash": run["detector_config_hash"],
            "artifact_id": artifact_id,
            "source_sha256": chunk_sha,
            "chunk": {
                "bucket": "raw-chunks",
                "key": chunk_key,
                "sha256": chunk_sha,
                "size_bytes": len(raw),
                "line_count": 4,
                "line_number_start": 1,
                "line_number_end": 4,
            },
            "manifest": {
                "bucket": "raw-chunks",
                "key": manifest_key,
                "sha256": sha256_hex(manifest_bytes),
            },
            "attempts": 0,
            "published": published,
            "created_at": now,
            "updated_at": now,
        }
        aws.dynamodb.put_item(
            TableName="SentinelJobs",
            Item=_to_item(job),
        )
        from sentinel.worker.message import job_message_from_record

        message = job_message_from_record(job, settings)
        if corrupt_chunk:
            aws.s3.put_object(
                Bucket="raw-chunks", Key=chunk_key, Body=b"corrupted payload"
            )
        return JobFixture(
            run=run,
            job=job,
            message=message,
            body=raw,
            chunk_key=chunk_key,
            manifest_key=manifest_key,
            detector_hash=run["detector_config_hash"],
        )

    return _make


def _to_item(value: dict[str, Any]) -> dict[str, Any]:
    from sentinel.api.store import to_item

    return to_item(value)


@pytest.fixture()
def enqueue(aws: SimpleNamespace) -> Callable[..., str]:
    def _enqueue(message: dict[str, Any], *, backend: str = "cpu") -> str:
        response = aws.sqs.send_message(
            QueueUrl=aws.urls["jobs-" + backend],
            MessageBody=json.dumps(message, sort_keys=True),
            MessageAttributes={
                "message_type": {"DataType": "String", "StringValue": "job"}
            },
        )
        return str(response["MessageId"])

    return _enqueue


@pytest.fixture()
def fake_executor(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[dict[str, Any]]]:
    def _install(alerts: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        chosen = alerts if alerts is not None else []

        def execute(*args: Any, **kwargs: Any) -> ExecutionResult:
            backend = args[0].backend if args else "cpu"
            return ExecutionResult(
                alerts=list(chosen),
                metrics={"line_count": 4, "alerts": len(chosen)},
                backend=backend,
                artifact={"artifact_id": "cpu-ref-v1", "digest_verified": True},
            )

        monkeypatch.setattr("sentinel.worker.worker.execute_job", execute)
        return chosen

    return _install


@pytest.fixture()
def message(make_run: Any, make_job: Any) -> dict[str, Any]:
    return make_job(make_run()).message


@pytest.fixture()
def worker(aws: SimpleNamespace, settings: Any, store: Any) -> Any:
    from sentinel.worker.worker import Worker

    return Worker(settings, store, start_metrics=False)


@pytest.fixture()
def alert(aws: SimpleNamespace) -> Callable[..., dict[str, Any]]:
    def _make(run_id: str, chunk_id: str, **overrides: Any) -> dict[str, Any]:
        item = {
            "alert_id": alert_id_for(
                run_id, chunk_id, 1, 0, 2, "auth-failed"
            ),
            "run_id": run_id,
            "chunk_id": chunk_id,
            "rule_id": "auth-failed",
            "line_id_first": "L1",
            "line_ids": ["L1"],
            "window_id": 0,
            "window_seconds": 60,
            "service_id": 2,
            "ip": 1,
            "status": 0,
            "severity": "high",
            "count": 4,
            "mean": 0.0,
            "std": 1.0,
            "score": 4.0,
            "created_at": "2026-11-20T10:00:00Z",
        }
        item.update(overrides)
        return item

    return _make


def get_job_item(aws: SimpleNamespace, job_id: str) -> dict[str, Any] | None:
    from sentinel.api.store import from_item

    response = aws.dynamodb.get_item(
        TableName="SentinelJobs", Key={"job_id": {"S": job_id}}, ConsistentRead=True
    )
    item = response.get("Item")
    return from_item(item) if item else None


def get_run_item(aws: SimpleNamespace, run_id: str) -> dict[str, Any] | None:
    from sentinel.api.store import from_item

    response = aws.dynamodb.get_item(
        TableName="SentinelRuns", Key={"run_id": {"S": run_id}}, ConsistentRead=True
    )
    item = response.get("Item")
    return from_item(item) if item else None


def drain_queue(aws: SimpleNamespace, name: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    while True:
        response = aws.sqs.receive_message(
            QueueUrl=aws.urls[name], MaxNumberOfMessages=10, WaitTimeSeconds=0
        )
        batch = response.get("Messages", [])
        if not batch:
            return messages
        for message in batch:
            messages.append(message)
            aws.sqs.delete_message(
                QueueUrl=aws.urls[name], ReceiptHandle=message["ReceiptHandle"]
            )
