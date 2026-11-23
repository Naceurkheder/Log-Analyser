"""Health probes and the published OpenAPI contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
OPENAPI_DOCUMENT = REPO / "docs" / "api" / "openapi.json"


def test_liveness(api: TestClient) -> None:
    response = api.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["X-Request-ID"]


def test_readiness_reports_dependencies(api: TestClient) -> None:
    response = api.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {
        "dynamodb.runs": "ok",
        "dynamodb.jobs": "ok",
        "dynamodb.alerts": "ok",
        "s3.incoming": "ok",
        "sqs.ingest": "ok",
    }


def test_readiness_fails_when_a_table_is_missing(
    api: TestClient, aws: Any
) -> None:
    aws.dynamodb.delete_table(TableName="SentinelAlerts")

    response = api.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["dynamodb.alerts"].startswith("missing")
    assert body["checks"]["dynamodb.runs"] == "ok"


def test_openapi_covers_the_contract(api: TestClient) -> None:
    document = api.app.openapi()

    assert {
        "/v1/runs",
        "/v1/runs/{run_id}",
        "/v1/runs/{run_id}/jobs",
        "/v1/runs/{run_id}/alerts",
        "/healthz",
        "/readyz",
    } <= set(document["paths"])

    create = document["paths"]["/v1/runs"]["post"]
    header_names = {
        parameter["name"] for parameter in create.get("parameters", [])
    }
    assert "Idempotency-Key" in header_names
    assert "RunRequest" in document["components"]["schemas"]
    assert "RunCreatedResponse" in document["components"]["schemas"]


def test_openapi_document_is_up_to_date(api: TestClient) -> None:
    assert OPENAPI_DOCUMENT.is_file(), (
        "docs/api/openapi.json is missing; run `make api-openapi`"
    )
    committed = json.loads(OPENAPI_DOCUMENT.read_text(encoding="utf-8"))
    current = json.loads(json.dumps(api.app.openapi(), sort_keys=True))

    assert current == committed, (
        "docs/api/openapi.json is stale; run `make api-openapi` and commit it"
    )
