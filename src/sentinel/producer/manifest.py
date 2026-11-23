"""Deterministic JSON manifests for immutable chunks.

A manifest is the boundary between the producer and the workers: it names the
chunk object, its checksum, the source identity, the exact record range, and
the parser/timestamp policy used to derive line ids.  Manifests contain no
wall-clock fields, so re-running the producer produces byte-identical output.
"""

from __future__ import annotations

from typing import Any

from ..ids import canonical_json, line_id_for
from .chunker import ChunkRecord, SourceSpec

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_TYPE = "chunk"
TIMESTAMP_POLICY = (
    "preserve-source-bytes; timestamps are interpreted by the detector "
    "configuration in Sprint 4"
)


def chunk_object_key(prefix: str, run_id: str, chunk_id: str) -> str:
    return f"{prefix}/{run_id}/{chunk_id}/data.bin"


def manifest_object_key(prefix: str, run_id: str, chunk_id: str) -> str:
    return f"{prefix}/{run_id}/{chunk_id}/manifest.json"


def progress_object_key(prefix: str, run_id: str) -> str:
    return f"{prefix}/{run_id}/progress.json"


def build_chunk_manifest(
    *,
    run_id: str,
    source: SourceSpec,
    source_sha256: str,
    source_bytes: int,
    record: ChunkRecord,
    chunk_bucket: str,
    chunk_key: str,
    detector_config_hash: str,
    parser_version: str,
    label_reference: str | None = None,
) -> dict[str, Any]:
    line_first = line_id_for(source_sha256, record.line_start)
    line_last = line_id_for(source_sha256, record.line_end)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest_type": MANIFEST_TYPE,
        "run_id": run_id,
        "chunk_id": record.chunk_id,
        "chunk_index": record.index,
        "source": {
            "bucket": source.bucket,
            "key": source.key,
            "encoding": source.encoding,
            "sha256": source_sha256,
            "size_bytes": source_bytes,
            "etag": source.etag,
        },
        "chunk": {
            "bucket": chunk_bucket,
            "key": chunk_key,
            "sha256": record.sha256,
            "size_bytes": record.size_bytes,
            "line_count": record.line_count,
            "line_number_start": record.line_start,
            "line_number_end": record.line_end,
            "line_id_first": line_first,
            "line_id_last": line_last,
            "byte_start": record.byte_start,
            "byte_end": record.byte_end,
        },
        "detector_config_hash": detector_config_hash,
        "parser_version": parser_version,
        "timestamp_policy": TIMESTAMP_POLICY,
        "label_reference": label_reference,
    }


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """Canonical encoding: stable key order and whitespace-free separators."""
    return canonical_json(manifest)
