"""Ingest consumer: stream a source object into chunks, manifests, and jobs.

The producer is intentionally at-least-once: publishing the ingest message is
retried by the API, SQS may redeliver, and the producer may be restarted.  All
identifiers and object keys are derived from the run, the source checksum and
the chunk contents, so repeated work converges on the same logical chunks and
jobs.  A completed progress checkpoint lets a restart rebuild jobs without
re-reading the source.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from typing import Any

from ..api.config import Settings
from ..api.contract import utcnow
from ..api.store import SentinelStore
from ..ids import job_id_for, sha256_hex
from .chunker import (
    SUPPORTED_ENCODINGS,
    ChunkingError,
    ChunkRecord,
    SourceSpec,
    chunk_stream,
)
from .manifest import (
    build_chunk_manifest,
    chunk_object_key,
    manifest_bytes,
    manifest_object_key,
    progress_object_key,
)

log = logging.getLogger("sentinel.producer")

INGEST_SCHEMA_VERSION = 1
BLOCK_BYTES = 1024 * 1024


class TerminalIngestError(Exception):
    """The ingest message cannot succeed by retrying (bad input/schema)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        run_id: str | None = None,
        details: dict[str, Any] | None = None,
        run_failed: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.run_id = run_id
        self.details = details or {}
        self.run_failed = run_failed


@dataclass
class ProducerMetrics:
    source_bytes_read: int = 0
    chunk_bytes_written: int = 0
    chunks_created: int = 0
    chunks_reused: int = 0
    lines_accepted: int = 0
    lines_rejected: int = 0
    jobs_created: int = 0
    jobs_existing: int = 0
    jobs_published: int = 0

    def event(self, *, run_id: str, elapsed_ms: float) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            run_id=run_id,
            elapsed_ms=round(elapsed_ms, 1),
            throughput_mib_s=round(
                self.source_bytes_read / (1024**2) / max(elapsed_ms / 1000, 1e-9),
                2,
            ),
        )
        return payload


class Producer:
    def __init__(
        self, settings: Settings | None = None, store: SentinelStore | None = None
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.store = store or SentinelStore(self.settings)
        if self.settings.auto_backend not in {"gpu", "cpu"}:
            raise ValueError("SENTINEL_AUTO_BACKEND must be 'gpu' or 'cpu'")
        if self.settings.chunk_oversize_policy not in {"reject"}:
            raise ValueError(
                "SENTINEL_CHUNK_OVERSIZE_POLICY must be 'reject'"
            )
        if self.settings.chunk_target_bytes < 1:
            raise ValueError("SENTINEL_CHUNK_TARGET_BYTES must be positive")

    # -- ingest -------------------------------------------------------------
    def process(self, message: dict[str, Any]) -> dict[str, Any]:
        run_id = self._validate_message(message)
        started = time.perf_counter()
        metrics = ProducerMetrics()

        run = self.store.get_run(run_id)
        if run is None:
            raise TerminalIngestError(
                "run_not_found", f"run '{run_id}' does not exist", run_id=run_id
            )
        if run.get("request_hash") != message.get("request_hash"):
            raise TerminalIngestError(
                "request_hash_mismatch",
                "ingest message does not match the recorded run",
                run_id=run_id,
                details={"run_id": run_id},
            )

        if run.get("status") in {"CANCELLED", "FAILED", "SUCCEEDED"}:
            log.info(
                "producer_skipped",
                extra={"event_fields": {"run_id": run_id, "status": run["status"]}},
            )
            return {"run_id": run_id, "status": run["status"], "skipped": True}

        if run.get("chunking_complete") and run.get("jobs_published"):
            log.info(
                "producer_replay",
                extra={"event_fields": {"run_id": run_id, "status": run["status"]}},
            )
            return {
                "run_id": run_id,
                "status": run["status"],
                "chunks": run.get("chunks_total", 0),
                "jobs": run.get("jobs_total", 0),
                "replayed": True,
            }

        backend = self._resolve_backend(run["backend_policy"])
        source = self._source_spec(run)

        self.store.set_run_state(
            run_id,
            status="CHUNKING",
            chunking_started_at=utcnow(),
            updated_at=utcnow(),
            backend=backend,
        )

        try:
            chunks, source_sha, source = self._chunk(run, source, metrics)
            self._write_manifests(run, source, chunks, source_sha)
            self._create_jobs(
                run, source, chunks, source_sha, backend, metrics
            )
        except ChunkingError as exc:
            metrics.lines_rejected += 1
            error = {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
            self.store.set_run_state(
                run_id,
                status="FAILED",
                error=error,
                updated_at=utcnow(),
            )
            self._log_metrics(run_id, metrics, started)
            raise TerminalIngestError(
                exc.code,
                exc.message,
                run_id=run_id,
                details=exc.details,
                run_failed=True,
            ) from exc

        self._log_metrics(run_id, metrics, started)
        return {
            "run_id": run_id,
            "status": "RUNNING",
            "chunks": len(chunks),
            "jobs": len(chunks),
            "source_sha256": source_sha,
        }

    # -- message validation -------------------------------------------------
    @staticmethod
    def _validate_message(message: dict[str, Any]) -> str:
        if not isinstance(message, dict):
            raise TerminalIngestError("invalid_message", "ingest message is not an object")
        if message.get("schema_version") != INGEST_SCHEMA_VERSION:
            raise TerminalIngestError(
                "unsupported_schema_version",
                "unsupported ingest message schema version",
                details={"schema_version": message.get("schema_version")},
            )
        if message.get("message_type") != "ingest":
            raise TerminalIngestError(
                "invalid_message_type", "expected an ingest message"
            )
        run_id = message.get("run_id")
        if not isinstance(run_id, str) or not run_id.startswith("run_"):
            raise TerminalIngestError("invalid_run_id", "ingest message has no valid run_id")
        return run_id

    def _resolve_backend(self, policy: str) -> str:
        if policy == "auto":
            return self.settings.auto_backend
        if policy in {"gpu", "cpu"}:
            return policy
        raise TerminalIngestError(
            "invalid_backend_policy", f"unsupported backend policy: {policy!r}"
        )

    @staticmethod
    def _source_spec(run: dict[str, Any]) -> SourceSpec:
        dataset = run.get("dataset") or {}
        return SourceSpec(
            bucket=dataset["bucket"],
            key=dataset["key"],
            encoding=dataset.get("encoding", "utf-8"),
            size_bytes=dataset.get("size_bytes"),
            sha256=dataset.get("sha256"),
        )

    # -- chunking -----------------------------------------------------------
    def _chunk(
        self, run: dict[str, Any], source: SourceSpec, metrics: ProducerMetrics
    ) -> tuple[list[ChunkRecord], str, SourceSpec]:
        run_id = run["run_id"]
        if source.encoding not in SUPPORTED_ENCODINGS:
            raise ChunkingError(
                "unsupported_encoding",
                "dataset encoding is not supported by the chunker",
                details={"encoding": source.encoding},
            )
        source_sha = self._reuse_completed_progress(run, source)
        if source_sha is not None:
            progress = self._load_progress(run_id)
            assert progress is not None  # checked by _reuse_completed_progress
            chunks = [ChunkRecord(**item) for item in progress["chunks"]]
            metrics.lines_accepted += sum(c.line_count for c in chunks)
            metrics.source_bytes_read += sum(c.size_bytes for c in chunks)
            saved = progress.get("source") or {}
            source = SourceSpec(
                bucket=source.bucket,
                key=source.key,
                encoding=source.encoding,
                size_bytes=saved.get("size_bytes"),
                sha256=source.sha256,
                etag=str(saved.get("etag") or ""),
            )
            return chunks, source_sha, source

        body, size, etag = self.store.open_object(source.bucket, source.key)
        source = SourceSpec(
            bucket=source.bucket,
            key=source.key,
            encoding=source.encoding,
            size_bytes=size,
            sha256=source.sha256,
            etag=etag,
        )

        chunks_so_far: list[ChunkRecord] = []

        def tracking_callback(record: ChunkRecord, data: bytes) -> None:
            self._write_chunk(run, record, data, metrics)
            self._write_progress(
                run, source, [*chunks_so_far, record], "in-progress"
            )
            chunks_so_far.append(record)

        with closing(body):
            result = chunk_stream(
                body,
                run_id=run_id,
                source=source,
                target_bytes=self.settings.chunk_target_bytes,
                max_line_bytes=int(run["detector"]["max_line_bytes"]),
                oversize_policy=self.settings.chunk_oversize_policy,
                block_size=BLOCK_BYTES,
                on_chunk=tracking_callback,
            )

        if not result.chunks:
            raise ChunkingError("empty_dataset", "the source object has no records")

        metrics.source_bytes_read += result.source_bytes
        metrics.lines_accepted += sum(c.line_count for c in result.chunks)
        # Progress is complete only after manifests and jobs are durable; the
        # in-progress checkpoint is overwritten below by _create_jobs.
        self._write_progress(
            run, source, result.chunks, "in-progress", source_sha=result.source_sha256
        )
        return result.chunks, result.source_sha256, source

    def _write_chunk(
        self,
        run: dict[str, Any],
        record: ChunkRecord,
        data: bytes,
        metrics: ProducerMetrics,
    ) -> None:
        run_id = run["run_id"]
        key = chunk_object_key(
            self.settings.chunk_prefix, run_id, record.chunk_id
        )
        metadata = {
            "sha256": record.sha256,
            "sha256-base64": _b64_sha256(record.sha256),
            "chunk-id": record.chunk_id,
            "run-id": run_id,
            "parser-version": self.settings.producer_parser_version,
        }
        existing = self.store.head_object(self.settings.raw_chunks_bucket, key)
        if existing is not None:
            self._verify_existing(existing, record.size_bytes, record.sha256)
            metrics.chunks_reused += 1
            return

        self.store.put_immutable(
            self.settings.raw_chunks_bucket, key, data, metadata=metadata
        )
        uploaded = self.store.head_object(
            self.settings.raw_chunks_bucket, key
        )
        if uploaded is None:  # pragma: no cover - S3 consistency/emulator failure
            raise ChunkingError(
                "chunk_upload_missing", "chunk object disappeared after upload"
            )
        self._verify_existing(uploaded, record.size_bytes, record.sha256)
        metrics.chunks_created += 1
        metrics.chunk_bytes_written += len(data)

    @staticmethod
    def _verify_existing(
        head: dict[str, Any], size_bytes: int, sha256: str
    ) -> None:
        if int(head.get("ContentLength", -1)) != size_bytes:
            raise ChunkingError(
                "chunk_conflict",
                "an existing immutable chunk has a different size",
                details={
                    "expected_bytes": size_bytes,
                    "actual_bytes": head.get("ContentLength"),
                },
            )
        stored = (head.get("Metadata") or {}).get("sha256")
        if stored != sha256:
            raise ChunkingError(
                "chunk_conflict",
                "an existing immutable chunk has a different checksum",
                details={"expected": sha256, "actual": stored},
            )

    def _write_manifests(
        self,
        run: dict[str, Any],
        source: SourceSpec,
        chunks: list[ChunkRecord],
        source_sha: str,
    ) -> None:
        run_id = run["run_id"]
        source_bytes = sum(c.size_bytes for c in chunks)
        for record in chunks:
            chunk_key = chunk_object_key(
                self.settings.chunk_prefix, run_id, record.chunk_id
            )
            manifest = build_chunk_manifest(
                run_id=run_id,
                source=source,
                source_sha256=source_sha,
                source_bytes=source_bytes,
                record=record,
                chunk_bucket=self.settings.raw_chunks_bucket,
                chunk_key=chunk_key,
                detector_config_hash=run["detector_config_hash"],
                parser_version=self.settings.producer_parser_version,
                label_reference=run.get("label_reference"),
            )
            self._write_manifest(run_id, record, manifest)

    def _write_manifest(
        self, run_id: str, record: ChunkRecord, manifest: dict[str, Any]
    ) -> None:
        body = manifest_bytes(manifest)
        digest = sha256_hex(body)
        key = manifest_object_key(
            self.settings.chunk_prefix, run_id, record.chunk_id
        )
        existing = self.store.head_object(self.settings.raw_chunks_bucket, key)
        if existing is not None:
            if int(existing.get("ContentLength", -1)) != len(body):
                raise ChunkingError(
                    "manifest_conflict", "existing manifest has a different size"
                )
            stored = (existing.get("Metadata") or {}).get("sha256")
            if stored not in {None, digest}:
                raise ChunkingError(
                    "manifest_conflict",
                    "existing manifest has a different checksum",
                    details={"expected": digest, "actual": stored},
                )
            return
        self.store.put_json_immutable(
            self.settings.raw_chunks_bucket,
            key,
            body,
            metadata={"sha256": digest, "chunk-id": record.chunk_id, "run-id": run_id},
        )
        uploaded = self.store.head_object(self.settings.raw_chunks_bucket, key)
        if uploaded is None or int(uploaded.get("ContentLength", -1)) != len(body):
            raise ChunkingError(
                "manifest_upload_missing", "manifest could not be verified"
            )

    # -- jobs ---------------------------------------------------------------
    def _create_jobs(
        self,
        run: dict[str, Any],
        source: SourceSpec,
        chunks: list[ChunkRecord],
        source_sha: str,
        backend: str,
        metrics: ProducerMetrics,
    ) -> None:
        run_id = run["run_id"]
        now = utcnow()
        artifact_id = self.settings.job_artifact_id
        for record in chunks:
            chunk_key = chunk_object_key(
                self.settings.chunk_prefix, run_id, record.chunk_id
            )
            manifest_key = manifest_object_key(
                self.settings.chunk_prefix, run_id, record.chunk_id
            )
            manifest = build_chunk_manifest(
                run_id=run_id,
                source=source,
                source_sha256=source_sha,
                source_bytes=sum(c.size_bytes for c in chunks),
                record=record,
                chunk_bucket=self.settings.raw_chunks_bucket,
                chunk_key=chunk_key,
                detector_config_hash=run["detector_config_hash"],
                parser_version=self.settings.producer_parser_version,
                label_reference=run.get("label_reference"),
            )
            manifest_sha = sha256_hex(manifest_bytes(manifest))
            job_id = job_id_for(
                run_id, record.chunk_id, run["detector_config_hash"], artifact_id
            )
            item = {
                "job_id": job_id,
                "run_id": run_id,
                "schema_version": 1,
                "status": "PENDING",
                "backend": backend,
                "chunk_id": record.chunk_id,
                "chunk_index": record.index,
                "detector_config_hash": run["detector_config_hash"],
                "artifact_id": artifact_id,
                "source_sha256": source_sha,
                "chunk": {
                    "bucket": self.settings.raw_chunks_bucket,
                    "key": chunk_key,
                    "sha256": record.sha256,
                    "size_bytes": record.size_bytes,
                    "line_count": record.line_count,
                    "line_number_start": record.line_start,
                    "line_number_end": record.line_end,
                },
                "manifest": {
                    "bucket": self.settings.raw_chunks_bucket,
                    "key": manifest_key,
                    "sha256": manifest_sha,
                },
                "attempts": 0,
                "published": False,
                "created_at": now,
                "updated_at": now,
            }
            if self.store.create_job(item):
                metrics.jobs_created += 1
                job = item
            else:
                metrics.jobs_existing += 1
                job = self.store.get_job(job_id)
                if job is None:  # pragma: no cover - emulator inconsistency
                    raise TerminalIngestError(
                        "job_lookup_failed", "job record disappeared", run_id=run_id
                    )

            if job.get("published"):
                continue
            self.store.send_job(
                backend,
                {
                    "schema_version": 1,
                    "message_type": "job",
                    "run_id": run_id,
                    "job_id": job_id,
                    "chunk_id": record.chunk_id,
                    "backend": backend,
                    "chunk": item["chunk"],
                    "manifest": item["manifest"],
                    "detector": {
                        "config_hash": run["detector_config_hash"],
                        "parser_version": self.settings.producer_parser_version,
                    },
                    "execution": {
                        "backend": backend,
                        "artifact_id": artifact_id,
                    },
                },
            )
            self.store.update_job(
                job_id, published=True, published_at=utcnow(), updated_at=utcnow()
            )
            metrics.jobs_published += 1

        self.store.set_run_state(
            run_id,
            status="RUNNING",
            backend=backend,
            source_sha256=source_sha,
            chunks_total=len(chunks),
            chunk_bytes_total=sum(c.size_bytes for c in chunks),
            jobs_total=len(chunks),
            chunking_complete=True,
            jobs_published=True,
            chunking_completed_at=utcnow(),
            updated_at=utcnow(),
        )
        self._write_progress(run, source, chunks, "complete", source_sha=source_sha)

    # -- progress checkpoint ------------------------------------------------
    def _load_progress(self, run_id: str) -> dict[str, Any] | None:
        key = progress_object_key(self.settings.chunk_prefix, run_id)
        if (
            self.store.head_object(self.settings.raw_chunks_bucket, key)
            is None
        ):
            return None
        try:
            raw = self.store.get_object_bytes(self.settings.raw_chunks_bucket, key)
            value = json.loads(raw)
        except Exception:  # noqa: BLE001 - a corrupt checkpoint is ignored
            log.warning(
                "producer_progress_unreadable",
                extra={"event_fields": {"run_id": run_id}},
            )
            return None
        if value.get("run_id") != run_id:
            return None
        return value

    def _reuse_completed_progress(
        self, run: dict[str, Any], source: SourceSpec
    ) -> str | None:
        progress = self._load_progress(run["run_id"])
        if not progress or progress.get("status") != "complete":
            return None
        if not progress.get("chunks"):
            return None
        saved = progress.get("source") or {}
        if saved.get("bucket") != source.bucket or saved.get("key") != source.key:
            return None
        head = self.store.head_object(source.bucket, source.key)
        if head is None:
            raise ChunkingError(
                "dataset_not_found", "source object disappeared while resuming"
            )
        etag = str(head.get("ETag", "")).strip('"')
        if saved.get("etag") != etag:
            log.info(
                "producer_progress_discarded",
                extra={
                    "event_fields": {
                        "run_id": run["run_id"],
                        "reason": "source_etag_changed",
                    }
                },
            )
            return None
        if int(head.get("ContentLength", -1)) != int(saved.get("size_bytes", -2)):
            return None
        source_sha = saved.get("sha256")
        if not isinstance(source_sha, str) or len(source_sha) != 64:
            return None
        if source.sha256 is not None and source.sha256 != source_sha:
            raise ChunkingError(
                "source_sha256_mismatch",
                "checkpoint checksum does not match the declared dataset",
                details={"declared": source.sha256, "actual": source_sha},
            )
        log.info(
            "producer_progress_reused",
            extra={
                "event_fields": {
                    "run_id": run["run_id"],
                    "chunks": len(progress["chunks"]),
                }
            },
        )
        return source_sha

    def _write_progress(
        self,
        run: dict[str, Any],
        source: SourceSpec,
        chunks: list[ChunkRecord],
        status: str,
        *,
        source_sha: str | None = None,
    ) -> None:
        run_id = run["run_id"]
        payload = {
            "schema_version": 1,
            "run_id": run_id,
            "status": status,
            "source": {
                "bucket": source.bucket,
                "key": source.key,
                "encoding": source.encoding,
                "size_bytes": source.size_bytes,
                "sha256": source_sha,
                "etag": source.etag,
            },
            "detector_config_hash": run["detector_config_hash"],
            "parser_version": self.settings.producer_parser_version,
            "chunks": [
                {
                    "index": chunk.index,
                    "chunk_id": chunk.chunk_id,
                    "sha256": chunk.sha256,
                    "size_bytes": chunk.size_bytes,
                    "line_count": chunk.line_count,
                    "line_start": chunk.line_start,
                    "line_end": chunk.line_end,
                    "byte_start": chunk.byte_start,
                    "byte_end": chunk.byte_end,
                }
                for chunk in chunks
            ],
        }
        key = progress_object_key(self.settings.chunk_prefix, run_id)
        self.store.put_json_overwrite(
            self.settings.raw_chunks_bucket,
            key,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            metadata={"run-id": run_id, "status": status},
        )

    # -- metrics ------------------------------------------------------------
    @staticmethod
    def _log_metrics(
        run_id: str, metrics: ProducerMetrics, started: float
    ) -> None:
        elapsed_ms = (time.perf_counter() - started) * 1000
        log.info(
            "producer_metrics",
            extra={"event_fields": metrics.event(run_id=run_id, elapsed_ms=elapsed_ms)},
        )


def _b64_sha256(hex_digest: str) -> str:
    import base64

    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")
