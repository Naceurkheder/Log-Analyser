"""Fetch and verify the S3 inputs referenced by a small job message."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError, EndpointConnectionError

from ..ids import sha256_hex
from .errors import ChecksumError, MessageError, TransientError
from .message import JobMessage
from .store import WorkerStore

MAX_MANIFEST_BYTES = 1024 * 1024
EXPECTED_MANIFEST_SCHEMA = 1


@dataclass(frozen=True)
class JobInput:
    data: bytes
    manifest: dict[str, Any]
    source_sha256: str

    def as_dict(self) -> dict[str, Any]:
        chunk = self.manifest["chunk"]
        return {
            "sha256": sha256_hex(self.data),
            "size_bytes": len(self.data),
            "line_count": chunk.get("line_count"),
            "line_number_start": chunk.get("line_number_start"),
        }


def _fetch(store: WorkerStore, bucket: str, key: str, *, label: str) -> bytes:
    try:
        return store.get_object_bytes(bucket, key)
    except (ClientError, EndpointConnectionError) as exc:
        raise TransientError(
            f"could not fetch {label} from S3",
            details={"bucket": bucket, "key": key, "error": type(exc).__name__},
        ) from exc


def load_job_input(store: WorkerStore, message: JobMessage, job: dict[str, Any]) -> JobInput:
    """Read the manifest first, verify it, then read the referenced chunk."""
    manifest_ref = message.manifest
    raw_manifest = _fetch(
        store,
        str(manifest_ref["bucket"]),
        str(manifest_ref["key"]),
        label="manifest",
    )
    if len(raw_manifest) > MAX_MANIFEST_BYTES:
        raise ChecksumError(
            "manifest is too large",
            details={"size_bytes": len(raw_manifest), "max": MAX_MANIFEST_BYTES},
        )
    if sha256_hex(raw_manifest) != manifest_ref["sha256"]:
        raise ChecksumError(
            "manifest checksum mismatch",
            details={
                "expected": manifest_ref["sha256"],
                "actual": sha256_hex(raw_manifest),
            },
        )
    try:
        manifest = json.loads(raw_manifest)
    except (ValueError, UnicodeDecodeError) as exc:
        raise MessageError("manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise MessageError("manifest is not an object")
    if manifest.get("schema_version") != EXPECTED_MANIFEST_SCHEMA:
        raise MessageError(
            "unsupported manifest schema version",
            details={"schema_version": manifest.get("schema_version")},
        )
    if manifest.get("manifest_type") != "chunk":
        raise MessageError("manifest is not a chunk manifest")
    if manifest.get("run_id") != message.run_id:
        raise MessageError("manifest run_id mismatch")
    if manifest.get("chunk_id") != message.chunk_id:
        raise MessageError("manifest chunk_id mismatch")
    if manifest.get("detector_config_hash") != message.detector["config_hash"]:
        raise ChecksumError(
            "manifest detector config hash mismatch",
            details={
                "expected": message.detector["config_hash"],
                "actual": manifest.get("detector_config_hash"),
            },
        )
    declared_chunk = manifest.get("chunk") or {}
    for key in ("bucket", "key", "sha256"):
        if declared_chunk.get(key) != message.chunk.get(key):
            raise ChecksumError(
                f"manifest chunk {key} does not match the job message",
                details={
                    "manifest": declared_chunk.get(key),
                    "message": message.chunk.get(key),
                },
            )
    source_sha = str((manifest.get("source") or {}).get("sha256") or "")
    if source_sha != job.get("source_sha256"):
        raise ChecksumError(
            "manifest source_sha256 does not match the job record",
            details={"manifest": source_sha, "job": job.get("source_sha256")},
        )

    data = _fetch(
        store,
        str(message.chunk["bucket"]),
        str(message.chunk["key"]),
        label="chunk",
    )
    digest = sha256_hex(data)
    if digest != message.chunk["sha256"]:
        raise ChecksumError(
            "chunk checksum mismatch",
            details={
                "expected": message.chunk["sha256"],
                "actual": digest,
                "bucket": message.chunk["bucket"],
                "key": message.chunk["key"],
            },
        )
    if len(data) != int(message.chunk["size_bytes"]):
        raise ChecksumError(
            "chunk size mismatch",
            details={
                "expected": message.chunk["size_bytes"],
                "actual": len(data),
            },
        )
    if int(declared_chunk.get("line_count", -1)) != int(message.chunk["line_count"]):
        raise ChecksumError("manifest line_count does not match the message")
    if int(declared_chunk.get("line_number_start", -1)) != int(
        message.chunk["line_number_start"]
    ):
        raise ChecksumError("manifest line_number_start does not match the message")
    return JobInput(data=data, manifest=manifest, source_sha256=source_sha)
