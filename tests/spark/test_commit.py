"""S6-backed Spark committer tests (no pyspark required)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sentinel.api.config import Settings
from sentinel.spark.commit import FencedWorkerCommitter
from sentinel.spark.config import SparkSettings
from sentinel.spark.core import ChunkCorruptError, SparkStageError
from sentinel.spark.job import run_spark_job
from sentinel.worker.state import RETRYABLE_FAILED, SUCCEEDED
from sentinel.worker.store import WorkerStore

from .conftest import RUN_ID


class _FakeRdd:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def map(self, function: Any) -> "_FakeRdd":
        return _FakeRdd([function(item) for item in self.items])

    def collect(self) -> list[Any]:
        return list(self.items)


class _FakeSparkContext:
    def parallelize(self, refs: Any, numSlices: int) -> _FakeRdd:
        return _FakeRdd(list(refs))


class _FakeSparkSession:
    sparkContext = _FakeSparkContext()


@pytest.fixture()
def worker_store(aws: Any) -> WorkerStore:
    return WorkerStore(Settings.from_env())


def _queue_url(aws: Any, name: str) -> str:
    return aws.sqs.get_queue_url(QueueName=name)["QueueUrl"]


def test_worker_fenced_committer_commits_and_drains_queue(
    make_run: Any, make_job: Any, worker_store: WorkerStore, aws: Any
) -> None:
    make_run()
    job = make_job(status="QUEUED")
    url = _queue_url(aws, "jobs-cpu")
    aws.sqs.send_message(
        QueueUrl=url,
        MessageBody=json.dumps(
            {"message_type": "job", "run_id": RUN_ID, "job_id": job["job_id"]}
        ),
    )

    settings = SparkSettings.from_env().overrides(committer="worker-fenced")
    summary = run_spark_job(
        RUN_ID,
        settings=settings,
        store=worker_store,
        session=_FakeSparkSession(),
    )

    assert summary["commits_acknowledged"] == 1
    stored = worker_store.get_job(job["job_id"])
    assert stored is not None
    assert stored["status"] == SUCCEEDED
    assert stored["fencing_token"] == 1
    assert stored["actual_backend"] == "spark-cpu"
    assert stored["result_key"].startswith(f"runs/{RUN_ID}/attempts/")
    assert aws.sqs.receive_message(QueueUrl=url, WaitTimeSeconds=0).get("Messages") in (
        [],
        None,
    )


def test_worker_fenced_committer_aborts_claims_on_task_failure(
    make_run: Any, make_job: Any, worker_store: WorkerStore
) -> None:
    make_run()
    job = make_job(status="QUEUED", chunk_sha256="0" * 64)
    settings = SparkSettings.from_env().overrides(committer="worker-fenced")

    with pytest.raises(ChunkCorruptError):
        run_spark_job(
            RUN_ID,
            settings=settings,
            store=worker_store,
            session=_FakeSparkSession(),
        )

    stored = worker_store.get_job(job["job_id"])
    assert stored is not None
    assert stored["status"] == RETRYABLE_FAILED
    assert stored["attempt_state"] == "retryable"
    assert stored["last_error"]["code"] == "spark_attempt_failed"


def test_committer_requires_worker_store(make_run: Any, store: Any) -> None:
    make_run()
    settings = SparkSettings.from_env()
    committer = FencedWorkerCommitter()
    with pytest.raises(SparkStageError):
        committer.prepare(plan=None, settings=settings, store=store)
