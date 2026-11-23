"""Cursor pagination over the run_id-index GSIs."""

from __future__ import annotations

import base64
from typing import Any

import boto3
from fastapi.testclient import TestClient

HEADERS = {"Idempotency-Key": "pagination-key-01"}


def _create_run(api: TestClient, make_payload: Any) -> str:
    response = api.post("/v1/runs", json=make_payload(), headers=HEADERS)
    assert response.status_code == 201
    return response.json()["run_id"]


def _seed(aws: Any, table_name: str, id_field: str, run_id: str, count: int) -> list[str]:
    table = boto3.resource("dynamodb", region_name=aws.region).Table(table_name)
    # Job/alert ids embed the run id in production; mirror that here so two
    # runs seeded with the same count cannot overwrite each other.
    prefix = run_id.removeprefix("run_")[:8]
    ids = [f"{prefix}-{id_field[:-3]}-{index:02d}" for index in range(count)]
    for item_id in ids:
        table.put_item(
            Item={
                id_field: item_id,
                "run_id": run_id,
                "status": "PENDING",
                "created_at": "2026-11-20T10:00:00Z",
            }
        )
    return ids


def _collect(api: TestClient, url: str, limit: int, id_field: str) -> list[str]:
    collected: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = api.get(url, params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["count"] == len(page["items"])
        assert page["count"] <= limit
        collected.extend(item[id_field] for item in page["items"])
        cursor = page["next_cursor"]
        pages += 1
        if cursor is None:
            break
        assert pages < 20, "pagination did not terminate"
    return collected


def test_jobs_paginate_without_duplicates(
    api: TestClient, aws: Any, make_payload: Any
) -> None:
    run_id = _create_run(api, make_payload)
    expected = _seed(aws, "SentinelJobs", "job_id", run_id, 5)

    collected = _collect(api, f"/v1/runs/{run_id}/jobs", limit=2, id_field="job_id")

    assert collected == expected


def test_alerts_paginate_without_duplicates(
    api: TestClient, aws: Any, make_payload: Any
) -> None:
    run_id = _create_run(api, make_payload)
    expected = _seed(aws, "SentinelAlerts", "alert_id", run_id, 5)

    collected = _collect(api, f"/v1/runs/{run_id}/alerts", limit=2, id_field="alert_id")

    assert collected == expected


def test_pagination_is_scoped_to_the_run(
    api: TestClient, aws: Any, make_payload: Any
) -> None:
    first = _create_run(api, make_payload)
    second = api.post(
        "/v1/runs", json=make_payload(), headers={"Idempotency-Key": "pagination-key-02"}
    ).json()["run_id"]
    _seed(aws, "SentinelJobs", "job_id", first, 3)
    _seed(aws, "SentinelJobs", "job_id", second, 4)

    jobs = _collect(api, f"/v1/runs/{first}/jobs", limit=50, id_field="job_id")

    assert len(jobs) == 3


def test_invalid_cursor_is_rejected(
    api: TestClient, make_payload: Any
) -> None:
    run_id = _create_run(api, make_payload)
    cursor = base64.urlsafe_b64encode(b'{"unexpected":"key"}').decode()

    response = api.get(f"/v1/runs/{run_id}/jobs", params={"cursor": cursor})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"


def test_limit_bounds_are_enforced(api: TestClient, make_payload: Any) -> None:
    run_id = _create_run(api, make_payload)

    assert api.get(f"/v1/runs/{run_id}/jobs", params={"limit": 0}).status_code == 422
    assert api.get(f"/v1/runs/{run_id}/jobs", params={"limit": 201}).status_code == 422


def test_unknown_run_pagination_returns_404(api: TestClient) -> None:
    assert api.get("/v1/runs/run_missing/jobs").status_code == 404
    assert api.get("/v1/runs/run_missing/alerts").status_code == 404
