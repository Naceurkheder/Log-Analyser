"""Conditional DynamoDB operations: claims, fences, commits and outbox."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from sentinel.worker.errors import (
    ClaimError,
    LeaseLostError,
    StateTransitionError,
    StaleTokenError,
)
from sentinel.worker.lease import lease_expiry
from sentinel.worker.state import RETRYABLE_FAILED, SUCCEEDED

from .conftest import get_job_item, get_run_item


def claim(store: Any, fixture: Any, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "job_id": fixture.job["job_id"],
        "run_id": fixture.run["run_id"],
        "backend": fixture.job["backend"],
        "detector_config_hash": fixture.job["detector_config_hash"],
        "artifact_id": fixture.job["artifact_id"],
        "worker_id": "worker-1",
        "attempt_id": uuid.uuid4().hex,
        "lease_expires_at": lease_expiry(30),
    }
    values.update(overrides)
    return store.claim_job(**values)


def test_claim_is_conditional_and_increments_token(
    aws: Any, store: Any, make_run: Any, make_job: Any
) -> None:
    fixture = make_job(make_run())
    claimed = claim(store, fixture)
    assert claimed["status"] == "RUNNING"
    assert claimed["fencing_token"] == 1
    assert claimed["attempts"] == 1
    assert claimed["worker_id"] == "worker-1"
    assert claimed["lease_expires_at"]
    with pytest.raises(StateTransitionError):
        claim(store, fixture)
    item = get_job_item(aws, fixture.job["job_id"])
    assert item["attempts"] == 1


def test_claim_rejects_identity_mismatch(
    aws: Any, store: Any, make_run: Any, make_job: Any
) -> None:
    fixture = make_job(make_run())
    with pytest.raises(ClaimError) as exc:
        claim(store, fixture, artifact_id="wrong-artifact")
    assert exc.value.code == "claim_conflict"
    assert get_job_item(aws, fixture.job["job_id"])["status"] == "PENDING"


def test_retryable_job_can_be_reclaimed_with_new_fence(
    aws: Any, store: Any, make_run: Any, make_job: Any
) -> None:
    fixture = make_job(make_run(), status=RETRYABLE_FAILED)
    first = claim(store, fixture)
    assert first["fencing_token"] == 1
    assert store.fail_job(
        fixture.job["job_id"],
        fencing_token=1,
        attempt_id=first["attempt_id"],
        status=RETRYABLE_FAILED,
        error={"code": "transient_failure"},
    )
    second = claim(store, fixture)
    assert second["fencing_token"] == 2
    assert second["attempts"] == 2


def test_heartbeat_requires_current_fencing_token(
    store: Any, make_run: Any, make_job: Any
) -> None:
    fixture = make_job(make_run())
    claimed = claim(store, fixture)
    assert store.heartbeat(
        fixture.job["job_id"],
        fencing_token=1,
        attempt_id=claimed["attempt_id"],
        lease_expires_at=lease_expiry(60),
    )
    assert (
        store.heartbeat(
            fixture.job["job_id"],
            fencing_token=0,
            attempt_id=claimed["attempt_id"],
            lease_expires_at=lease_expiry(60),
        )
        is None
    )


def test_lease_loss_makes_job_retryable_and_blocks_commit(
    aws: Any, store: Any, make_run: Any, make_job: Any
) -> None:
    fixture = make_job(make_run())
    claimed = claim(store, fixture)
    store.mark_lease_lost(
        fixture.job["job_id"],
        fencing_token=1,
        attempt_id=claimed["attempt_id"],
        reason="visibility_expired",
    )
    item = get_job_item(aws, fixture.job["job_id"])
    assert item["status"] == RETRYABLE_FAILED
    assert item["lease_lost_at"]
    with pytest.raises(LeaseLostError):
        store.commit_job(
            fixture.job["job_id"],
            fencing_token=1,
            attempt_id=claimed["attempt_id"],
            result={},
            actual_backend="cpu",
            result_bucket="results",
            result_key="runs/x/attempts/y/1.json",
            result_sha256="0" * 64,
        )


def test_commit_is_fenced_and_replay_is_a_noop(
    aws: Any, store: Any, make_run: Any, make_job: Any
) -> None:
    fixture = make_job(make_run())
    claimed = claim(store, fixture)
    fields = {
        "fencing_token": 1,
        "attempt_id": claimed["attempt_id"],
        "result": {"key": "runs/r/attempts/j/1.json"},
        "actual_backend": "cpu",
        "result_bucket": "results",
        "result_key": "runs/r/attempts/j/1.json",
        "result_sha256": "a" * 64,
    }
    assert store.commit_job(fixture.job["job_id"], **fields) is True
    item = get_job_item(aws, fixture.job["job_id"])
    assert item["status"] == SUCCEEDED
    assert item["result"]["sha256"] == "a" * 64
    # Same logical result is a harmless duplicate; different content is not.
    assert store.commit_job(fixture.job["job_id"], **fields) is False
    assert (
        store.commit_job(fixture.job["job_id"], **{**fields, "result_sha256": "b" * 64})
        is False
    )
    with pytest.raises(StaleTokenError):
        store.commit_job(
            fixture.job["job_id"], **{**fields, "fencing_token": 0}
        )


def test_fail_job_guards_token_and_records_error(
    aws: Any, store: Any, make_run: Any, make_job: Any
) -> None:
    fixture = make_job(make_run())
    claimed = claim(store, fixture)
    assert (
        store.fail_job(
            fixture.job["job_id"],
            fencing_token=1,
            attempt_id=claimed["attempt_id"],
            status="FAILED_PERMANENT",
            error={"code": "checksum_mismatch"},
        )
        is True
    )
    item = get_job_item(aws, fixture.job["job_id"])
    assert item["status"] == "FAILED_PERMANENT"
    assert item["last_error"]["code"] == "checksum_mismatch"
    with pytest.raises(StaleTokenError):
        store.fail_job(
            fixture.job["job_id"],
            fencing_token=99,
            attempt_id=claimed["attempt_id"],
            status=RETRYABLE_FAILED,
            error={"code": "late"},
        )
    # FAILED_PERMANENT is terminal: execute-phase quarantine writes
    # DEAD_LETTERED directly with the same fencing token instead.
    assert store.dead_letter_job(fixture.job["job_id"], error={"code": "poison"}) is False


def test_alert_writes_are_conditional(aws: Any, store: Any, alert: Any) -> None:
    payload = alert("run_1", "chk-1")
    assert store.put_alert(payload) is True
    assert store.put_alert(payload) is False
    response = aws.dynamodb.scan(TableName="SentinelAlerts")
    assert response["Count"] == 1


def test_run_counters_are_derived_and_finalized_once(
    aws: Any, store: Any, make_run: Any, make_job: Any
) -> None:
    run = make_run()
    fixture = make_job(run)
    claimed = claim(store, fixture)
    store.put_alert(alert := {
        "alert_id": fixture.alert_id,
        "run_id": run["run_id"],
        "chunk_id": fixture.job["chunk_id"],
    })
    assert store.finalize_run(
        run["run_id"], counts=store.run_counts(run["run_id"]), status="RUNNING"
    ) is False
    store.commit_job(
        fixture.job["job_id"],
        fencing_token=1,
        attempt_id=claimed["attempt_id"],
        result={},
        actual_backend="cpu",
        result_bucket="results",
        result_key="runs/r/attempts/j/1.json",
        result_sha256="c" * 64,
    )
    counts = store.run_counts(run["run_id"])
    assert counts == {
        "jobs_total": 1,
        "jobs_completed": 1,
        "jobs_failed": 0,
        "alerts_total": 1,
    }
    assert store.finalize_run(run["run_id"], counts=counts, status="SUCCEEDED") is True
    assert store.finalize_run(run["run_id"], counts=counts, status="FAILED") is False
    assert get_run_item(aws, run["run_id"])["status"] == "SUCCEEDED"


def test_outbox_scan_and_conditional_mark(
    aws: Any, store: Any, make_run: Any, make_job: Any
) -> None:
    run = make_run(ingest_published=False)
    fixture = make_job(run, published=False)
    assert [job["job_id"] for job in store.pending_jobs(page_size=10, max_pages=2)] == [
        fixture.job["job_id"]
    ]
    assert [item["run_id"] for item in store.pending_runs(page_size=10, max_pages=2)] == [
        run["run_id"]
    ]
    assert store.mark_job_published(fixture.job["job_id"]) is True
    assert store.mark_job_published(fixture.job["job_id"]) is False
    assert store.mark_run_ingest_published(run["run_id"]) is True
    assert store.mark_run_ingest_published(run["run_id"]) is False
    assert get_job_item(aws, fixture.job["job_id"])["dispatch_attempts"] == 1
    assert store.pending_jobs(page_size=10, max_pages=2) == []
    assert store.pending_runs(page_size=10, max_pages=2) == []
