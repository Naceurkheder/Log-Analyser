"""End-to-end producer behaviour against the moto-backed plane."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sentinel.api.config import Settings
from sentinel.api.store import SentinelStore
from sentinel.ids import job_id_for, line_id_for, sha256_hex
from sentinel.producer import IngestRunner, Producer

HEADERS = {"Idempotency-Key": "producer-key-0001"}


def receive_all(sqs: Any, queue_url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while True:
        response = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=0,
            VisibilityTimeout=1,
        )
        messages = response.get("Messages", [])
        if not messages:
            return items
        for message in messages:
            sqs.delete_message(
                QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"]
            )
        items.extend(messages)


def _ingest_body(api: TestClient, aws: Any, payload: dict[str, Any]) -> dict[str, Any]:
    response = api.post("/v1/runs", json=payload, headers=HEADERS)
    assert response.status_code == 201, response.text
    # Peek without deleting: the queue is the producer's input in these tests.
    response = aws.sqs.receive_message(
        QueueUrl=aws.urls["ingest-queue"],
        MaxNumberOfMessages=1,
        WaitTimeSeconds=0,
        VisibilityTimeout=0,
    )
    messages = response.get("Messages", [])
    assert len(messages) == 1
    return json.loads(messages[0]["Body"])


def _objects(s3: Any, bucket: str, prefix: str = "") -> dict[str, bytes]:
    keys = [
        item["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix
        )
        for item in page.get("Contents", [])
    ]
    return {
        key: s3.get_object(Bucket=bucket, Key=key)["Body"].read() for key in keys
    }


def test_producer_writes_chunks_manifests_and_jobs(
    api: TestClient, aws: Any, make_payload: Any
) -> None:
    body = _ingest_body(api, aws, make_payload())

    results = IngestRunner().poll(max_messages=1)
    assert len(results) == 1
    assert results[0]["status"] == "RUNNING"
    chunk_count = results[0]["chunks"]
    assert chunk_count >= 2
    assert results[0]["source_sha256"] == hashlib.sha256(aws.body).hexdigest()

    run = api.get(f"/v1/runs/{body['run_id']}").json()
    assert run["status"] == "RUNNING"
    assert run["progress"]["jobs_total"] == chunk_count

    objects = _objects(aws.s3, aws.raw_bucket)
    manifests = {
        key: json.loads(value)
        for key, value in objects.items()
        if key.endswith("/manifest.json")
    }
    data_objects = {
        key: value for key, value in objects.items() if key.endswith("/data.bin")
    }
    assert len(manifests) == chunk_count
    assert len(data_objects) == chunk_count

    ordered = sorted(manifests.values(), key=lambda item: item["chunk_index"])
    assert [m["chunk_index"] for m in ordered] == list(range(chunk_count))
    assert b"".join(
        data_objects[m["chunk"]["key"]] for m in ordered
    ) == aws.body

    expected_line = 1
    for manifest in ordered:
        chunk = manifest["chunk"]
        payload = data_objects[chunk["key"]]
        assert chunk["sha256"] == sha256_hex(payload)
        assert chunk["size_bytes"] == len(payload)
        assert chunk["line_number_start"] == expected_line
        assert chunk["line_number_end"] == expected_line + chunk["line_count"] - 1
        assert chunk["line_id_first"] == line_id_for(
            manifest["source"]["sha256"], chunk["line_number_start"]
        )
        assert chunk["line_id_last"] == line_id_for(
            manifest["source"]["sha256"], chunk["line_number_end"]
        )
        expected_line = chunk["line_number_end"] + 1
        assert manifest["source"]["sha256"] == hashlib.sha256(aws.body).hexdigest()
        assert manifest["parser_version"] == "sentinel-parser-v1"
        assert manifest["timestamp_policy"]
        assert manifest["detector_config_hash"] == run["detector_config_hash"]
    assert expected_line - 1 == 4  # four source records

    job_messages = receive_all(aws.sqs, aws.urls["jobs-gpu"])
    assert len(job_messages) == chunk_count
    for raw in job_messages:
        assert "SUPERSECRETLOGPAYLOAD" not in raw["Body"]
        message = json.loads(raw["Body"])
        assert message["message_type"] == "job"
        assert message["backend"] == "gpu"
        assert message["chunk"]["bucket"] == aws.raw_bucket
        assert message["manifest"]["key"].endswith("/manifest.json")
        assert message["execution"]["artifact_id"] == "cpu-ref-v1"

    jobs = aws.dynamodb.scan(TableName="SentinelJobs")["Items"]
    assert len(jobs) == chunk_count
    for item in jobs:
        job_id = item["job_id"]["S"]
        chunk_id = item["chunk_id"]["S"]
        expected = job_id_for(
            body["run_id"], chunk_id, run["detector_config_hash"], "cpu-ref-v1"
        )
        assert job_id == expected
        assert item["status"]["S"] == "PENDING"
        assert item["published"]["BOOL"] is True


def test_replay_does_not_duplicate_jobs_or_messages(
    api: TestClient, aws: Any, make_payload: Any
) -> None:
    body = _ingest_body(api, aws, make_payload())
    producer = Producer()
    first = producer.process(body)
    assert first["status"] == "RUNNING"

    receive_all(aws.sqs, aws.urls["jobs-gpu"])  # drain the first publication
    before = _objects(aws.s3, aws.raw_bucket, prefix=f"chunks/{body['run_id']}/")

    second = producer.process(body)
    assert second.get("replayed") is True
    assert _objects(aws.s3, aws.raw_bucket, prefix=f"chunks/{body['run_id']}/") == before
    assert receive_all(aws.sqs, aws.urls["jobs-gpu"]) == []
    assert len(aws.dynamodb.scan(TableName="SentinelJobs")["Items"]) == first["jobs"]


def test_restart_reuses_completed_progress_without_reading_source(
    api: TestClient, aws: Any, make_payload: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _ingest_body(api, aws, make_payload())
    producer = Producer()
    first = producer.process(body)
    receive_all(aws.sqs, aws.urls["jobs-gpu"])  # drain the first publication

    # Simulate a crash after jobs were published but before the run record was
    # updated: the next delivery must reuse the completed checkpoint.
    settings = Settings.from_env()
    store = SentinelStore(settings)
    store.set_run_state(
        body["run_id"], chunking_complete=False, jobs_published=False
    )

    def _must_not_read(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("producer re-read the source despite complete progress")

    monkeypatch.setattr(store, "open_object", _must_not_read)
    resumed = Producer(settings, store).process(body)

    assert resumed["status"] == "RUNNING"
    assert resumed["chunks"] == first["chunks"]
    assert receive_all(aws.sqs, aws.urls["jobs-gpu"]) == []
    assert len(aws.dynamodb.scan(TableName="SentinelJobs")["Items"]) == first["jobs"]


def test_cpu_backend_routes_to_cpu_queue(
    api: TestClient, aws: Any, make_payload: Any
) -> None:
    body = _ingest_body(api, aws, make_payload(backend_policy="cpu"))
    result = IngestRunner().poll(max_messages=1)
    assert result[0]["status"] == "RUNNING"
    assert receive_all(aws.sqs, aws.urls["jobs-gpu"]) == []
    assert len(receive_all(aws.sqs, aws.urls["jobs-cpu"])) == result[0]["jobs"]


def test_oversized_record_fails_run_and_consumes_message(
    api: TestClient, aws: Any
) -> None:
    key = "datasets/long.log"
    aws.s3.put_object(Bucket=aws.bucket, Key=key, Body=b"ok\n" + b"Y" * 300 + b"\n")
    payload = {
        "schema_version": 1,
        "dataset": {"bucket": "incoming", "key": key},
        "detector": {
            "rules": [{"rule_id": "r1", "pattern": "error"}],
            "max_line_bytes": 256,
        },
        "backend_policy": "gpu",
    }
    body = _ingest_body(api, aws, payload)

    results = IngestRunner().poll(max_messages=1)
    assert results[0]["error"] == "line_too_long"
    assert receive_all(aws.sqs, aws.urls["ingest-queue"]) == []

    run = api.get(f"/v1/runs/{body['run_id']}").json()
    assert run["status"] == "FAILED"
    # The failure detail is retained on the record for operators.
    stored = SentinelStore(Settings.from_env()).get_run(body["run_id"])
    assert stored["error"]["code"] == "line_too_long"
    assert stored["error"]["details"]["line_number"] == 2
