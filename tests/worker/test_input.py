"""Chunk/manifest checksum and reference validation before execution."""

from __future__ import annotations

from typing import Any

import pytest

from sentinel.worker.errors import ChecksumError, MessageError, TransientError
from sentinel.worker.input import load_job_input
from sentinel.worker.message import JobMessage


def test_valid_input_loads_chunk_and_manifest(
    aws: Any, store: Any, make_run: Any, make_job: Any
) -> None:
    fixture = make_job(make_run())
    parsed = JobMessage.parse(fixture.message, "cpu")
    result = load_job_input(store, parsed, fixture.job)
    assert result.data == fixture.body
    assert result.manifest["chunk_id"] == fixture.job["chunk_id"]
    assert result.as_dict()["size_bytes"] == len(fixture.body)


def test_corrupt_chunk_is_a_permanent_checksum_error(
    aws: Any, store: Any, make_run: Any, make_job: Any
) -> None:
    fixture = make_job(make_run(), corrupt_chunk=True)
    parsed = JobMessage.parse(fixture.message, "cpu")
    with pytest.raises(ChecksumError) as exc:
        load_job_input(store, parsed, fixture.job)
    assert exc.value.code == "checksum_mismatch"


def test_corrupt_manifest_is_rejected(
    aws: Any, store: Any, make_run: Any, make_job: Any
) -> None:
    fixture = make_job(make_run())
    aws.s3.put_object(
        Bucket="raw-chunks", Key=fixture.manifest_key, Body=b"{broken"
    )
    parsed = JobMessage.parse(fixture.message, "cpu")
    with pytest.raises(ChecksumError):
        load_job_input(store, parsed, fixture.job)


def test_missing_chunk_is_transient(
    aws: Any, store: Any, make_run: Any, make_job: Any
) -> None:
    fixture = make_job(make_run())
    aws.s3.delete_object(Bucket="raw-chunks", Key=fixture.chunk_key)
    parsed = JobMessage.parse(fixture.message, "cpu")
    with pytest.raises(TransientError):
        load_job_input(store, parsed, fixture.job)


def test_manifest_detector_hash_drift_is_rejected(
    aws: Any, store: Any, make_run: Any, make_job: Any, settings: Any
) -> None:
    fixture = make_job(make_run())
    from sentinel.ids import canonical_json, sha256_hex

    manifest = __import__("json").loads(
        aws.s3.get_object(Bucket="raw-chunks", Key=fixture.manifest_key)["Body"].read()
    )
    manifest["detector_config_hash"] = "f" * 64
    body = canonical_json(manifest)
    aws.s3.put_object(
        Bucket="raw-chunks",
        Key=fixture.manifest_key,
        Body=body,
        Metadata={"sha256": sha256_hex(body)},
    )
    message = dict(fixture.message)
    message["detector"] = dict(fixture.message["detector"], config_hash="f" * 64)
    parsed = JobMessage.parse(message, "cpu")
    with pytest.raises(ChecksumError):
        load_job_input(store, parsed, fixture.job)
