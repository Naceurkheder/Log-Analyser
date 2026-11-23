"""moto-backed plane for producer integration tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

SAMPLE_KEY = "datasets/sample.log"
SAMPLE_BODY = (
    b"2026-11-20T10:00:00Z host sshd[1]: Failed password for root\n"
    b"2026-11-20T10:00:01Z host nginx[2]: 10.0.0.1 - HTTP/1.1 500\r\n"
    b"SUPERSECRETLOGPAYLOAD must never appear in a job message\n"
    b"2026-11-20T10:00:02Z host kernel: SEGFAULT"
)


def _create_tables(client: Any) -> None:
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
        _create_tables(dynamodb)

        s3 = boto3.client("s3", region_name=region)
        for bucket in ("incoming", "raw-chunks"):
            s3.create_bucket(Bucket=bucket)
        s3.put_object(Bucket="incoming", Key=SAMPLE_KEY, Body=SAMPLE_BODY)

        sqs = boto3.client("sqs", region_name=region)
        urls = {
            name: sqs.create_queue(QueueName=name)["QueueUrl"]
            for name in ("ingest-queue", "jobs-gpu", "jobs-cpu")
        }

        for name, value in {
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
            "SENTINEL_INGEST_QUEUE": "ingest-queue",
            "SENTINEL_JOBS_GPU_QUEUE": "jobs-gpu",
            "SENTINEL_JOBS_CPU_QUEUE": "jobs-cpu",
            "SENTINEL_CHUNK_TARGET_BYTES": str(100),
            "SENTINEL_LOG_LEVEL": "WARNING",
        }.items():
            monkeypatch.setenv(name, value)

        yield SimpleNamespace(
            region=region,
            dynamodb=dynamodb,
            s3=s3,
            sqs=sqs,
            urls=urls,
            bucket="incoming",
            raw_bucket="raw-chunks",
            key=SAMPLE_KEY,
            body=SAMPLE_BODY,
        )


@pytest.fixture()
def api(aws: SimpleNamespace) -> Iterator[TestClient]:
    from sentinel.api.main import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture()
def make_payload() -> Any:
    def _make(**overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "dataset": {"bucket": "incoming", "key": SAMPLE_KEY},
            "detector": {
                "rules": [
                    {
                        "rule_id": "auth-failed-password",
                        "pattern": "Failed password",
                        "severity": "high",
                    }
                ]
            },
            "backend_policy": "gpu",
        }
        payload.update(overrides)
        return payload

    return _make
