"""Core contract tests: projection, verification and deterministic execution."""

from __future__ import annotations

import io
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from sentinel.api.store import to_item
from sentinel.detector.semantics import ALGORITHM_VERSION, DetectorSettings
from sentinel.spark.core import (
    ChunkCorruptError,
    ChunkMissingError,
    DetectorConfigMismatchError,
    JobRefError,
    attempt_id_for,
    job_refs_from_records,
    load_chunk,
    process_job_ref,
)

from .conftest import CHUNK_BODY, DETECTOR, DETECTOR_HASH, RUN_ID, SOURCE_SHA


def _ref(job: dict[str, Any]):
    refs = job_refs_from_records([job])
    assert len(refs) == 1
    return refs[0]


def test_job_refs_from_records_projects_minimal_immutable_ref(make_job: Any) -> None:
    job = make_job()
    ref = _ref(job)
    assert ref.job_id == job["job_id"]
    assert ref.run_id == RUN_ID
    assert ref.chunk_id == job["chunk_id"]
    assert ref.detector_config_hash == DETECTOR_HASH
    assert ref.source_sha256 == SOURCE_SHA
    assert ref.chunk_bucket == "raw-chunks"
    assert ref.chunk_key == job["chunk"]["key"]
    assert ref.chunk_sha256 == job["chunk"]["sha256"]
    assert ref.chunk_size_bytes == len(CHUNK_BODY)
    assert ref.line_number_start == 1
    assert ref.status == "PENDING"
    assert "created_at" not in ref.as_dict()
    with pytest.raises(FrozenInstanceError):
        ref.job_id = "other"  # type: ignore[misc]


def test_job_refs_from_records_accepts_wire_items_and_filters_status(
    make_job: Any,
) -> None:
    pending = to_item(make_job(body=b"one\n", persisted=False))
    queued_job = make_job(body=b"two\n", persisted=False)
    queued_job["status"] = "QUEUED"
    queued = to_item(queued_job)
    done_job = make_job(body=b"three\n", persisted=False)
    done_job["status"] = "SUCCEEDED"
    done = to_item(done_job)

    refs = job_refs_from_records([pending, queued, done])
    assert [ref.status for ref in refs] == ["PENDING", "QUEUED", "SUCCEEDED"]
    eligible = job_refs_from_records(
        [pending, queued, done], statuses=("PENDING", "QUEUED")
    )
    assert [ref.status for ref in eligible] == ["PENDING", "QUEUED"]
    collapsed = job_refs_from_records([pending, pending])
    assert len(collapsed) == 1


def test_job_refs_from_records_rejects_missing_fields() -> None:
    with pytest.raises(JobRefError):
        job_refs_from_records([{"job_id": "job-1"}])


def test_load_chunk_verifies_success_size_and_sha(make_job: Any, store: Any) -> None:
    good = _ref(make_job())
    assert load_chunk(store.s3, good) == CHUNK_BODY
    assert load_chunk(store, good) == CHUNK_BODY  # SentinelStore adapter

    bad_sha = _ref(make_job(chunk_sha256="0" * 64))
    with pytest.raises(ChunkCorruptError) as sha_exc:
        load_chunk(store.s3, bad_sha)
    assert sha_exc.value.stage == "head-sha256"

    bad_size = _ref(make_job(size_bytes=len(CHUNK_BODY) + 1))
    with pytest.raises(ChunkCorruptError) as size_exc:
        load_chunk(store.s3, bad_size)
    assert size_exc.value.stage == "head-size"


def test_load_chunk_missing_object(make_job: Any, store: Any, aws: Any) -> None:
    job = make_job()
    ref = _ref(job)
    aws.s3.delete_object(Bucket=ref.chunk_bucket, Key=ref.chunk_key)
    with pytest.raises(ChunkMissingError) as exc:
        load_chunk(store.s3, ref)
    assert exc.value.key == ref.chunk_key


class _MinimalS3:
    """Fake with only get_object, used when moto is unavailable."""

    def __init__(self, objects: dict[tuple[str, str], bytes]) -> None:
        self.objects = objects

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        if (Bucket, Key) not in self.objects:
            raise KeyError(Key)
        body = self.objects[(Bucket, Key)]
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}


def test_load_chunk_with_minimal_fake_client(make_job: Any) -> None:
    job = make_job(persisted=False)
    ref = _ref(job)
    fake = _MinimalS3({(ref.chunk_bucket, ref.chunk_key): CHUNK_BODY})
    assert load_chunk(fake, ref) == CHUNK_BODY
    with pytest.raises(ChunkMissingError):
        load_chunk(_MinimalS3({}), ref)


def test_process_job_ref_is_deterministic_and_wall_clock_free(
    make_job: Any, detector: DetectorSettings
) -> None:
    ref = _ref(make_job())
    first = process_job_ref(
        ref, detector, source_sha256=SOURCE_SHA, run_id=RUN_ID, data=CHUNK_BODY
    )
    second = process_job_ref(
        ref,
        detector,
        source_sha256=SOURCE_SHA,
        run_id=RUN_ID,
        data=CHUNK_BODY,
    )
    assert first == second
    assert first == dict(sorted(first.items()))
    assert set(first) == {
        "alerts",
        "actual_backend",
        "algorithm_version",
        "attempt_id",
        "chunk_id",
        "job_id",
        "metrics",
        "output_sha256",
        "run_id",
    }
    assert first["actual_backend"] == "spark-cpu"
    assert first["algorithm_version"] == ALGORITHM_VERSION
    assert first["metrics"]["line_count"] == 4
    assert first["metrics"]["matches"] == 4
    assert first["attempt_id"] == attempt_id_for(
        ref.job_id, SOURCE_SHA, DETECTOR_HASH
    )
    assert len(first["output_sha256"]) == 64


def test_process_job_ref_rejects_wrong_config_and_run(
    make_job: Any, detector: DetectorSettings
) -> None:
    ref = _ref(make_job())
    other_config = DetectorSettings.from_dict(
        {**DETECTOR, "rules": DETECTOR["rules"] + [
            {"rule_id": "extra", "pattern": "error", "severity": "low"}
        ]}
    )
    with pytest.raises(DetectorConfigMismatchError):
        process_job_ref(
            ref, other_config, source_sha256=SOURCE_SHA, run_id=RUN_ID,
            data=CHUNK_BODY,
        )
    with pytest.raises(JobRefError):
        process_job_ref(
            ref, detector, source_sha256=SOURCE_SHA, run_id="run_other",
            data=CHUNK_BODY,
        )


def test_process_job_ref_fetches_through_injected_client(
    make_job: Any, store: Any, detector: DetectorSettings
) -> None:
    ref = _ref(make_job())
    from_client = process_job_ref(
        ref, detector, source_sha256=SOURCE_SHA, run_id=RUN_ID, client=store.s3
    )
    from_data = process_job_ref(
        ref, detector, source_sha256=SOURCE_SHA, run_id=RUN_ID, data=CHUNK_BODY
    )
    assert from_client == from_data
