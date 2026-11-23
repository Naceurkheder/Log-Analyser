"""CLI tests with a mocked store; none of these require pyspark."""

from __future__ import annotations

import json
from typing import Any

import pytest

import sentinel.spark.cli as cli
import sentinel.spark.job as job_module
from sentinel.spark.cli import main
from sentinel.spark.core import ChunkCorruptError
from sentinel.spark.job import PySparkUnavailableError

from .conftest import RUN_ID


class MockStore:
    """Bounded in-memory stand-in for SentinelStore.query_jobs/get_run."""

    def __init__(self, run: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
        self.run = run
        self.jobs = jobs
        self.calls: list[int] = []

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.run if run_id == self.run["run_id"] else None

    def query_jobs(
        self, run_id: str, limit: int, start_key: dict[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        offset = int((start_key or {}).get("offset", 0))
        self.calls.append(limit)
        items = self.jobs[offset : offset + limit]
        next_offset = offset + len(items)
        cursor = {"offset": next_offset} if next_offset < len(self.jobs) else None
        return items, cursor


@pytest.fixture()
def mock_store(make_run: Any, make_job: Any) -> MockStore:
    run = make_run()
    jobs = [
        make_job(body=b"one\n", status="PENDING", persisted=False),
        make_job(body=b"two\n", status="SUCCEEDED", persisted=False),
        make_job(body=b"three\n", status="QUEUED", persisted=False),
    ]
    return MockStore(run, jobs)


def test_plan_prints_only_eligible_job_refs(mock_store: MockStore, capsys: Any) -> None:
    code = main(
        ["plan", "--run-id", RUN_ID, "--limit", "2", "--endpoint-url", "http://example"],
        store=mock_store,
    )
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["run_id"] == RUN_ID
    assert payload["backend"] == "spark-cpu"
    assert payload["jobs_selected"] == 2
    assert {ref["status"] for ref in payload["job_refs"]} == {"PENDING", "QUEUED"}
    assert all("chunk_sha256" in ref for ref in payload["job_refs"])
    assert mock_store.calls and max(mock_store.calls) <= 2


def test_run_reports_missing_pyspark_clearly(
    mock_store: MockStore, capsys: Any, monkeypatch: Any
) -> None:
    def unavailable() -> None:
        raise PySparkUnavailableError(
            "pyspark is not installed; install the optional Spark dependencies "
            "with `pip install -r requirements-spark.txt` before running this stage"
        )

    monkeypatch.setattr(job_module, "_import_pyspark", unavailable)
    code = main(["run", "--run-id", RUN_ID], store=mock_store)
    captured = capsys.readouterr()
    assert code == 3
    error = json.loads(captured.err)
    assert error["error_class"] == "pyspark_unavailable"
    assert "requirements-spark.txt" in error["error"]


def test_run_maps_verification_failure_to_nonzero(
    mock_store: MockStore, capsys: Any, monkeypatch: Any
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise ChunkCorruptError("raw-chunks", "key", stage="body")

    monkeypatch.setattr(cli, "run_spark_job", fail)
    code = main(["run", "--run-id", RUN_ID], store=mock_store)
    error = json.loads(capsys.readouterr().err)
    assert code == 2
    assert error["error_class"] == "ChunkCorruptError"
