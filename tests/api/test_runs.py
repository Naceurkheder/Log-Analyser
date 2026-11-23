"""Run creation, idempotency, and contract validation."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

HEADERS = {"Idempotency-Key": "test-key-0001"}


def _receive(aws: Any, expected: int = 1) -> list[dict[str, Any]]:
    response = aws.sqs.receive_message(
        QueueUrl=aws.queue_url,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=0,
    )
    messages = response.get("Messages", [])
    assert len(messages) == expected
    return messages


def test_create_run_records_and_queues(api: TestClient, aws: Any, make_payload: Any) -> None:
    response = api.post("/v1/runs", json=make_payload(), headers=HEADERS)

    assert response.status_code == 201
    body = response.json()
    assert body["created"] is True
    assert body["status"] == "QUEUED"
    assert body["run_id"].startswith("run_")
    assert response.headers["Location"] == f"/v1/runs/{body['run_id']}"
    assert response.headers["X-Request-ID"]

    status = api.get(body["links"]["run"])
    assert status.status_code == 200
    run = status.json()
    assert run["run_id"] == body["run_id"]
    assert run["dataset"]["size_bytes"] == len(aws.body)
    assert len(run["detector_config_hash"]) == 64
    assert run["progress"] == {
        "jobs_total": 0,
        "jobs_completed": 0,
        "jobs_failed": 0,
        "alerts_total": 0,
    }

    (message,) = _receive(aws)
    queued = json.loads(message["Body"])
    assert queued["message_type"] == "ingest"
    assert queued["run_id"] == body["run_id"]
    assert queued["dataset"]["key"] == aws.key
    assert "detector" in queued
    # Raw log bytes must never travel through SQS.
    assert "SUPERSECRETLOGPAYLOAD" not in message["Body"]


def test_idempotent_replay_returns_same_run(
    api: TestClient, aws: Any, make_payload: Any
) -> None:
    payload = make_payload()
    first = api.post("/v1/runs", json=payload, headers=HEADERS)
    second = api.post("/v1/runs", json=payload, headers=HEADERS)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["run_id"] == first.json()["run_id"]
    _receive(aws, expected=1)  # exactly one ingest message


def test_defaulted_and_explicit_payloads_are_the_same_run(
    api: TestClient, make_payload: Any
) -> None:
    explicit = make_payload(
        detector={
            "rules": [
                {
                    "rule_id": "auth-failed-password",
                    "pattern": "Failed password",
                    "severity": "high",
                }
            ],
            "window_seconds": 60,
            "baseline_windows": 10,
            "z_threshold": 3.0,
            "max_line_bytes": 65536,
        }
    )
    first = api.post("/v1/runs", json=make_payload(), headers=HEADERS)
    second = api.post("/v1/runs", json=explicit, headers=HEADERS)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["run_id"] == first.json()["run_id"]


def test_same_key_different_payload_conflicts(
    api: TestClient, make_payload: Any
) -> None:
    api.post("/v1/runs", json=make_payload(), headers=HEADERS)
    response = api.post(
        "/v1/runs", json=make_payload(backend_policy="cpu"), headers=HEADERS
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_key_reuse"


def test_distinct_keys_create_distinct_runs(
    api: TestClient, aws: Any, make_payload: Any
) -> None:
    first = api.post(
        "/v1/runs", json=make_payload(), headers={"Idempotency-Key": "key-alpha-01"}
    )
    second = api.post(
        "/v1/runs", json=make_payload(), headers={"Idempotency-Key": "key-beta-02"}
    )

    assert first.status_code == second.status_code == 201
    assert first.json()["run_id"] != second.json()["run_id"]
    _receive(aws, expected=2)


def test_publish_failure_is_retryable(
    api: TestClient, make_payload: Any, monkeypatch: Any
) -> None:
    store = api.app.state.store
    original = store.send_ingest

    def boom(_: dict[str, Any]) -> None:
        raise ClientError(
            {"Error": {"Code": "InternalError", "Message": "boom"}}, "SendMessage"
        )

    monkeypatch.setattr(store, "send_ingest", boom)
    failed = api.post("/v1/runs", json=make_payload(), headers=HEADERS)

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "ingest_publish_failed"
    run_id = failed.json()["error"]["details"]["run_id"]
    assert api.get(f"/v1/runs/{run_id}").json()["status"] == "INGEST_FAILED"

    monkeypatch.setattr(store, "send_ingest", original)
    retried = api.post("/v1/runs", json=make_payload(), headers=HEADERS)
    assert retried.status_code == 200
    assert retried.json()["status"] == "QUEUED"
    assert retried.json()["run_id"] == run_id


def test_queue_name_discovery_when_url_is_unset(
    aws: Any, make_payload: Any, monkeypatch: Any
) -> None:
    from sentinel.api.config import Settings
    from sentinel.api.main import create_app

    monkeypatch.delenv("SENTINEL_INGEST_QUEUE_URL")
    monkeypatch.setenv("SENTINEL_INGEST_QUEUE", "ingest-queue")
    settings = Settings.from_env()
    assert settings.ingest_queue_url is None

    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/runs", json=make_payload(), headers=HEADERS)

    assert response.status_code == 201
    assert response.json()["status"] == "QUEUED"
    _receive(aws, expected=1)


def test_missing_queue_fails_visibly(
    aws: Any, make_payload: Any, monkeypatch: Any
) -> None:
    from sentinel.api.config import Settings
    from sentinel.api.main import create_app

    monkeypatch.delenv("SENTINEL_INGEST_QUEUE_URL")
    monkeypatch.setenv("SENTINEL_INGEST_QUEUE", "queue-that-does-not-exist")

    app = create_app(Settings.from_env())
    with TestClient(app) as client:
        response = client.post("/v1/runs", json=make_payload(), headers=HEADERS)

        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "ingest_publish_failed"
        run_id = error["details"]["run_id"]
        assert client.get(f"/v1/runs/{run_id}").json()["status"] == (
            "INGEST_FAILED"
        )


def test_missing_idempotency_key_is_rejected(
    api: TestClient, make_payload: Any
) -> None:
    response = api.post("/v1/runs", json=make_payload())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_required"


def test_invalid_idempotency_key_is_rejected(
    api: TestClient, make_payload: Any
) -> None:
    response = api.post(
        "/v1/runs", json=make_payload(), headers={"Idempotency-Key": "short"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_idempotency_key"


def test_dataset_must_exist(api: TestClient, make_payload: Any) -> None:
    payload = make_payload(dataset={"bucket": "incoming", "key": "datasets/nope.log"})
    response = api.post("/v1/runs", json=payload, headers=HEADERS)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "dataset_not_found"


def test_dataset_size_mismatch_is_rejected(
    api: TestClient, aws: Any, make_payload: Any
) -> None:
    payload = make_payload(
        dataset={
            "bucket": "incoming",
            "key": aws.key,
            "size_bytes": len(aws.body) + 1,
        }
    )
    response = api.post("/v1/runs", json=payload, headers=HEADERS)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "dataset_size_mismatch"
    assert error["details"]["actual_bytes"] == len(aws.body)


def test_empty_dataset_is_rejected(
    api: TestClient, aws: Any, make_payload: Any
) -> None:
    aws.s3.put_object(Bucket="incoming", Key="datasets/empty.log", Body=b"")
    payload = make_payload(dataset={"bucket": "incoming", "key": "datasets/empty.log"})
    response = api.post("/v1/runs", json=payload, headers=HEADERS)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "empty_dataset"


def test_foreign_bucket_is_rejected(api: TestClient, make_payload: Any) -> None:
    payload = make_payload(dataset={"bucket": "artifacts", "key": "x.log"})
    response = api.post("/v1/runs", json=payload, headers=HEADERS)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "dataset_bucket_not_allowed"


def test_dataset_size_limit_is_enforced(
    aws: Any, make_payload: Any
) -> None:
    from sentinel.api.config import Settings
    from sentinel.api.main import create_app

    settings = replace(Settings.from_env(), max_dataset_bytes=10)
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/runs", json=make_payload(), headers=HEADERS)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "dataset_too_large"


def test_raw_log_content_in_body_is_rejected(
    api: TestClient, make_payload: Any
) -> None:
    payload = make_payload()
    payload["content"] = "2026-11-20 host: raw log line"
    response = api.post("/v1/runs", json=payload, headers=HEADERS)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert any(detail["loc"][-1] == "content" for detail in error["details"])


def test_invalid_backend_policy_is_rejected(
    api: TestClient, make_payload: Any
) -> None:
    response = api.post(
        "/v1/runs", json=make_payload(backend_policy="tpu"), headers=HEADERS
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_unsupported_schema_version_is_rejected(
    api: TestClient, make_payload: Any
) -> None:
    response = api.post(
        "/v1/runs", json=make_payload(schema_version=2), headers=HEADERS
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_duplicate_rule_ids_are_rejected(
    api: TestClient, make_payload: Any
) -> None:
    duplicate = make_payload(
        detector={
            "rules": [
                {"rule_id": "same-rule", "pattern": "a"},
                {"rule_id": "same-rule", "pattern": "b"},
            ]
        }
    )
    response = api.post("/v1/runs", json=duplicate, headers=HEADERS)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_unknown_run_returns_404(api: TestClient) -> None:
    response = api.get("/v1/runs/run_does_not_exist")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "run_not_found"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]


def test_run_id_matches_shared_id_contract(
    api: TestClient, make_payload: Any
) -> None:
    from sentinel.ids import run_id_for

    response = api.post(
        "/v1/runs",
        json=make_payload(),
        headers={"Idempotency-Key": "contract-key-0001"},
    )

    assert response.status_code == 201
    assert response.json()["run_id"] == run_id_for("contract-key-0001")
