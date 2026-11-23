"""Driver planning and result committer boundary tests (no pyspark needed)."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from sentinel.spark.config import SparkSettings
from sentinel.spark.core import (
    SPARK_BACKEND,
    AttemptOutput,
    ChunkCorruptError,
    job_refs_from_records,
)
from sentinel.spark.job import (
    LoggingCommitter,
    _raise_task_errors,
    _task_execute,
    plan_jobs,
    run_spark_job,
)

from .conftest import DETECTOR, DETECTOR_HASH, RUN_ID


def test_plan_jobs_pages_bounds_and_filters_status(
    make_run: Any, make_job: Any, store: Any
) -> None:
    make_run()
    make_job(body=b"one Failed password line\n", status="QUEUED")
    make_job(body=b"two Failed password line\n", status="SUCCEEDED")
    make_job(body=b"three Failed password line\n", status="PENDING")
    settings = SparkSettings.from_env().overrides(
        limit=2, page_size=1, max_pages=10
    )

    plan = plan_jobs(RUN_ID, settings=settings, store=store)

    assert plan.run_id == RUN_ID
    assert plan.detector["rules"] == [
        {"rule_id": "auth-failed", "pattern": "Failed password", "severity": "high"}
    ]
    assert {ref.status for ref in plan.refs} == {"PENDING", "QUEUED"}
    assert all(ref.detector_config_hash == DETECTOR_HASH for ref in plan.refs)
    assert len({ref.job_id for ref in plan.refs}) == 2


def test_plan_jobs_refuses_unknown_run(store: Any) -> None:
    from sentinel.spark.core import SparkStageError

    settings = SparkSettings.from_env()
    with pytest.raises(SparkStageError):
        plan_jobs("run_missing", settings=settings, store=store)


def test_task_error_envelope_rebuilds_verification_error(make_job: Any) -> None:
    ref = job_refs_from_records([make_job(chunk_sha256="0" * 64)])[0]
    settings = SparkSettings.from_env()
    envelope = _task_execute(ref, dict(DETECTOR), settings.connection, RUN_ID)
    assert envelope["_spark_error"]["class"] == "ChunkCorruptError"
    with pytest.raises(ChunkCorruptError) as exc:
        _raise_task_errors([envelope])
    assert exc.value.stage == "head-sha256"


class _FakeRdd:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def map(self, function: Any) -> "_FakeRdd":
        return _FakeRdd([function(item) for item in self.items])

    def collect(self) -> list[Any]:
        return list(self.items)


class _FakeSparkContext:
    def parallelize(self, refs: Any, numSlices: int) -> _FakeRdd:
        assert numSlices == len(refs)
        return _FakeRdd(list(refs))


class _FakeSparkSession:
    sparkContext = _FakeSparkContext()


def test_run_spark_job_writes_attempt_output_with_injected_session(
    make_run: Any, make_job: Any, store: Any, aws: Any
) -> None:
    make_run()
    job = make_job(status="QUEUED")
    settings = SparkSettings.from_env()
    summary = run_spark_job(
        RUN_ID, settings=settings, store=store, session=_FakeSparkSession()
    )
    assert summary["jobs_selected"] == 1
    assert summary["jobs_processed"] == 1
    assert summary["attempts_written"] == 1
    assert summary["commits_acknowledged"] == 0  # LoggingCommitter is advisory
    attempt = summary["attempts"][0]
    assert attempt["job_id"] == job["job_id"]
    head = aws.s3.head_object(Bucket="results", Key=attempt["result_key"])
    assert head["Metadata"]["sha256"] == attempt["result_sha256"]


def test_logging_committer_does_not_fake_durable_commit(caplog: Any) -> None:
    result = {
        "run_id": "run_x",
        "job_id": "job_y",
        "attempt_id": "att_z",
        "actual_backend": SPARK_BACKEND,
    }
    output = AttemptOutput(
        bucket="results",
        key="runs/run_x/attempts/job_y/att_z.json",
        sha256="a" * 64,
        size_bytes=10,
    )
    settings = SparkSettings.from_env()
    caplog.set_level(logging.INFO, logger="sentinel.spark.job")
    decision = LoggingCommitter().commit(
        result=result, output=output, settings=settings, store=object()
    )
    assert decision == {"committed": False, "durable": False, "committer": "logging"}
    assert "spark_result_not_committed" in caplog.text
