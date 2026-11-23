"""Canonical, content-addressed identifiers.

Every id in the system is derived deterministically from the inputs that
define it.  That is what makes retries, replays and duplicate deliveries
idempotent: the same logical work produces the same ids, so it can be written
again without creating a second logical result.

Contract (documented in docs/decisions/0006-run-contract.md; algorithm
semantics arrive in Sprint 4):

    run_id    = "run_" + sha256("sentinel-run-v1:" + idempotency_key)[:32]
    chunk_id  = "chk-" + sha256(run_id|index|chunk_sha256)[:16]
    job_id    = "job-" + sha256(run_id|chunk_id|config_hash|artifact_id)[:20]
    line_id   = "L"   + sha256(source_sha256|line_no)[:16]
    alert_id  = "alt-" + sha256(run_id|job_id|ip|bucket|service|rule)[:24]
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    """Stable JSON encoding used for fingerprints and hashes."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def config_hash(detector: dict[str, Any]) -> str:
    """Hash of the detector configuration (patterns, threshold, algorithm)."""
    return fingerprint(detector)


def run_id_for(idempotency_key: str) -> str:
    """Must stay byte-identical to the control API's ``derive_run_id``."""
    return "run_" + sha256_hex(
        f"sentinel-run-v1:{idempotency_key}".encode()
    )[:32]


def chunk_id_for(run_id: str, index: int, chunk_sha256: str) -> str:
    return "chk-" + sha256_hex(
        f"{run_id}|{index}|{chunk_sha256}".encode()
    )[:16]


def job_id_for(
    run_id: str, chunk_id: str, detector_hash: str, artifact_id: str
) -> str:
    return "job-" + sha256_hex(
        f"{run_id}|{chunk_id}|{detector_hash}|{artifact_id}".encode()
    )[:20]


def line_id_for(source_sha256: str, line_no: int) -> str:
    return "L" + sha256_hex(f"{source_sha256}|{line_no}".encode())[:16]


def alert_id_for(
    run_id: str, job_id: str, ip: int, bucket: int, service: int, rule: str
) -> str:
    return "alt-" + sha256_hex(
        f"{run_id}|{job_id}|{ip}|{bucket}|{service}|{rule}".encode()
    )[:24]


# Artifact identity placeholder until the S7 artifact pipeline pins builds.
DEFAULT_ARTIFACT_ID = "cpu-ref-v1"
