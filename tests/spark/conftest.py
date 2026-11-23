"""moto-backed S3/DynamoDB plane and deterministic fixtures for Spark tests."""

from __future__ import annotations

import sys
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

from sentinel.api.store import SentinelStore, to_item  # noqa: E402
from sentinel.detector.semantics import DetectorSettings  # noqa: E402
from sentinel.ids import chunk_id_for, config_hash, job_id_for, run_id_for, sha256_hex  # noqa: E402

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
SOURCE_SHA = sha256_hex(b"synthetic-source-dataset")
DETECTOR_HASH = config_hash(DETECTOR)
RUN_ID = run_id_for("spark-stage-test")


@pytest.fixture()
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    with mock_aws():
        region = "us-east-1"
        s3 = boto3.client("s3", region_name=region)
        for bucket in ("incoming", "raw-chunks", "artifacts", "results"):
            s3.create_bucket(Bucket=bucket)
        dynamodb = boto3.client("dynamodb", region_name=region)
        dynamodb.create_table(
            TableName="SentinelRuns",
            KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="SentinelJobs",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "job_id", "AttributeType": "S"},
                {"AttributeName": "run_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "run_id-index",
                    "KeySchema": [
                        {"AttributeName": "run_id", "KeyType": "HASH"},
                        {"AttributeName": "job_id", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="SentinelAlerts",
            KeySchema=[{"AttributeName": "alert_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "alert_id", "AttributeType": "S"},
                {"AttributeName": "run_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "run_id-index",
                    "KeySchema": [
                        {"AttributeName": "run_id", "KeyType": "HASH"},
                        {"AttributeName": "alert_id", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        sqs = boto3.client("sqs", region_name=region)
        for name in ("ingest-queue", "jobs-gpu", "jobs-cpu"):
            sqs.create_queue(
                QueueName=name, Attributes={"VisibilityTimeout": "0"}
            )
        for key, value in {
            "AWS_ENDPOINT_URL": "",
            "AWS_REGION": region,
            "AWS_DEFAULT_REGION": region,
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "SENTINEL_RUNS_TABLE": "SentinelRuns",
            "SENTINEL_JOBS_TABLE": "SentinelJobs",
            "SENTINEL_RAW_CHUNKS_BUCKET": "raw-chunks",
            "SENTINEL_RESULTS_BUCKET": "results",
        }.items():
            monkeypatch.setenv(key, value)
        yield SimpleNamespace(region=region, s3=s3, dynamodb=dynamodb, sqs=sqs)


@pytest.fixture()
def detector() -> DetectorSettings:
    return DetectorSettings.from_dict(DETECTOR)


@pytest.fixture()
def store(aws: SimpleNamespace) -> SentinelStore:
    from sentinel.api.config import Settings

    return SentinelStore(Settings.from_env())


@pytest.fixture()
def make_run(aws: SimpleNamespace) -> Callable[..., dict[str, Any]]:
    def _make(run_id: str = RUN_ID, **overrides: Any) -> dict[str, Any]:
        run: dict[str, Any] = {
            "run_id": run_id,
            "schema_version": 1,
            "status": "RUNNING",
            "detector": dict(DETECTOR),
            "detector_config_hash": DETECTOR_HASH,
        }
        run.update(overrides)
        aws.dynamodb.put_item(TableName="SentinelRuns", Item=to_item(run))
        return run

    return _make


@pytest.fixture()
def make_job(aws: SimpleNamespace) -> Callable[..., dict[str, Any]]:
    def _make(
        run_id: str = RUN_ID,
        *,
        body: bytes | None = None,
        status: str = "PENDING",
        chunk_sha256: str | None = None,
        size_bytes: int | None = None,
        source_sha256: str = SOURCE_SHA,
        persisted: bool = True,
    ) -> dict[str, Any]:
        payload = CHUNK_BODY if body is None else body
        actual_sha = sha256_hex(payload)
        chunk_id = chunk_id_for(run_id, 0, actual_sha)
        chunk_key = f"chunks/{run_id}/{chunk_id}/data.bin"
        aws.s3.put_object(
            Bucket="raw-chunks",
            Key=chunk_key,
            Body=payload,
            Metadata={"sha256": actual_sha, "run-id": run_id},
        )
        job = {
            "job_id": job_id_for(run_id, chunk_id, DETECTOR_HASH, "cpu-ref-v1"),
            "run_id": run_id,
            "schema_version": 1,
            "status": status,
            "backend": "cpu",
            "chunk_id": chunk_id,
            "detector_config_hash": DETECTOR_HASH,
            "artifact_id": "cpu-ref-v1",
            "source_sha256": source_sha256,
            "chunk": {
                "bucket": "raw-chunks",
                "key": chunk_key,
                "sha256": actual_sha if chunk_sha256 is None else chunk_sha256,
                "size_bytes": len(payload) if size_bytes is None else size_bytes,
                "line_count": 4,
                "line_number_start": 1,
                "line_number_end": 4,
            },
            "manifest": {
                "bucket": "raw-chunks",
                "key": f"chunks/{run_id}/{chunk_id}/manifest.json",
                "sha256": sha256_hex(b"manifest"),
            },
            "attempts": 0,
            "published": True,
        }
        if persisted:
            aws.dynamodb.put_item(TableName="SentinelJobs", Item=to_item(job))
        return job

    return _make
