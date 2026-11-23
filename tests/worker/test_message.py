"""Job message validation and authoritative record cross-checks."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from sentinel.worker.errors import DetectorConfigError, MessageError
from sentinel.worker.message import (
    JobMessage,
    ingest_message_from_run,
    job_message_from_record,
    validate_authoritative,
)


def test_valid_message_parses(message: dict[str, Any]) -> None:
    parsed = JobMessage.parse(message, "cpu")
    assert parsed.job_id == message["job_id"]
    assert parsed.backend == "cpu"
    assert parsed.replay is None
    assert parsed.error_context()["run_id"] == message["run_id"]


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda m: m.update(schema_version=2), "unsupported_schema_version"),
        (lambda m: m.update(message_type="ingest"), "invalid_message_type"),
        (lambda m: m.update(backend="gpu"), "backend_capability_mismatch"),
        (
            lambda m: m["execution"].update(backend="gpu"),
            "backend_capability_mismatch",
        ),
        (lambda m: m.pop("job_id"), "invalid_message"),
        (lambda m: m.update(run_id="bad"), "invalid_message"),
        (lambda m: m["chunk"].update(sha256="xyz"), "invalid_message"),
        (lambda m: m["detector"].update(config_hash="abc"), "invalid_message"),
        (lambda m: m["manifest"].pop("key"), "invalid_message"),
    ],
)
def test_invalid_messages_are_permanent(
    message: dict[str, Any], mutate: Any, code: str
) -> None:
    candidate = copy.deepcopy(message)
    mutate(candidate)
    with pytest.raises(MessageError) as exc:
        JobMessage.parse(candidate, "cpu")
    assert exc.value.code == code
    assert exc.value.transient is False


def test_non_object_message(message: dict[str, Any]) -> None:
    with pytest.raises(MessageError):
        JobMessage.parse(["not", "an", "object"], "cpu")


def test_authoritative_check_detects_detector_hash_drift(
    message: dict[str, Any], make_run: Any, make_job: Any
) -> None:
    run = make_run()
    job_fixture = make_job(run)
    parsed = JobMessage.parse(message, "cpu")
    validate_authoritative(parsed, job=job_fixture.job, run=run)
    drifted = copy.deepcopy(parsed)
    drifted.detector["config_hash"] = "0" * 64
    with pytest.raises(MessageError):
        validate_authoritative(drifted, job=job_fixture.job, run=run)


def test_authoritative_check_detects_run_config_drift(
    message: dict[str, Any], make_run: Any, make_job: Any
) -> None:
    run = make_run()
    job_fixture = make_job(run)
    parsed = JobMessage.parse(message, "cpu")
    bad_run = dict(run, detector_config_hash="1" * 64)
    with pytest.raises(DetectorConfigError):
        validate_authoritative(parsed, job=job_fixture.job, run=bad_run)
    with pytest.raises(MessageError):
        validate_authoritative(parsed, job=job_fixture.job, run=None)


def test_reconstructed_messages_match_producer_shape(
    make_run: Any, make_job: Any, settings: Any
) -> None:
    run = make_run()
    fixture = make_job(run, published=False)
    rebuilt = job_message_from_record(fixture.job, settings)
    assert rebuilt == fixture.message
    assert JobMessage.parse(rebuilt, "cpu").job_id == fixture.job["job_id"]
    ingest = ingest_message_from_run(run)
    assert ingest["message_type"] == "ingest"
    assert ingest["request_hash"] == run["request_hash"]
    assert ingest["dataset"] == run["dataset"]
