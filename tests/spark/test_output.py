"""Immutable attempt output naming, metadata and verification."""

from __future__ import annotations

from typing import Any

import pytest

from sentinel.detector.semantics import DetectorSettings
from sentinel.ids import canonical_json, sha256_hex
from sentinel.spark.core import (
    AttemptOutputError,
    attempt_output_key,
    job_refs_from_records,
    process_job_ref,
    write_attempt_output,
)

from .conftest import CHUNK_BODY, SOURCE_SHA


def _result(make_job: Any, detector: DetectorSettings, **overrides: Any) -> dict[str, Any]:
    ref = job_refs_from_records([make_job()])[0]
    result = process_job_ref(
        ref, detector, source_sha256=SOURCE_SHA, run_id=ref.run_id,
        data=CHUNK_BODY,
    )
    result.update(overrides)
    return result


def test_attempt_output_key_matches_worker_contract() -> None:
    assert attempt_output_key("run_x", "job_y", "att_z") == (
        "runs/run_x/attempts/job_y/att_z.json"
    )


def test_write_attempt_output_is_immutable_verified_and_idempotent(
    make_job: Any, detector: DetectorSettings, aws: Any
) -> None:
    result = _result(make_job, detector)
    expected_key = attempt_output_key(
        result["run_id"], result["job_id"], result["attempt_id"]
    )
    output = write_attempt_output(aws.s3, "results", result)
    assert output.bucket == "results"
    assert output.key == expected_key
    assert output.existed is False

    body = canonical_json(result) + b"\n"
    assert output.sha256 == sha256_hex(body)
    assert output.size_bytes == len(body)

    head = aws.s3.head_object(Bucket="results", Key=expected_key)
    assert int(head["ContentLength"]) == len(body)
    assert head["Metadata"]["sha256"] == output.sha256
    assert head["Metadata"]["output-sha256"] == result["output_sha256"]
    assert head["Metadata"]["run-id"] == result["run_id"]
    assert head["Metadata"]["job-id"] == result["job_id"]
    assert head["Metadata"]["attempt-id"] == result["attempt_id"]
    assert head["Metadata"]["backend"] == "spark-cpu"
    assert aws.s3.get_object(Bucket="results", Key=expected_key)["Body"].read() == body

    replay = write_attempt_output(aws.s3, "results", result)
    assert replay.existed is True
    assert replay.sha256 == output.sha256

    conflicting = dict(result)
    conflicting["metrics"] = {**result["metrics"], "alerts": 99}
    with pytest.raises(AttemptOutputError) as exc:
        write_attempt_output(aws.s3, "results", conflicting)
    assert exc.value.stage == "existing-body"
