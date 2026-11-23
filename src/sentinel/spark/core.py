"""Pure core of the optional Spark stage.

There is deliberately **no pyspark import in this module**. It owns the four
contracts that make a Spark attempt reproducible:

1. projecting DynamoDB job items into small immutable references,
2. fetching a chunk through boto3 and verifying its exact bytes,
3. running the CPU detector and deriving a wall-clock-free attempt id,
4. writing an immutable, attempt-scoped result object and verifying the write.

boto3 is used directly (not Hadoop S3A) so the same code works in Spark local
mode against the moto/S3 endpoint.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from ..detector.pipeline import detect
from ..detector.semantics import DetectorSettings
from ..ids import canonical_json, sha256_hex

SPARK_BACKEND = "spark-cpu"
RESULT_SCHEMA_VERSION = 1
ELIGIBLE_STATUSES = frozenset({"PENDING", "QUEUED"})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound", "NoSuchBucket"})
_EXISTS_CODES = frozenset({"PreconditionFailed", "ConditionalRequestConflict"})


# -- errors -----------------------------------------------------------------


class SparkStageError(Exception):
    """Base class for errors that should fail the Spark stage cleanly."""


class JobRefError(SparkStageError, ValueError):
    """A DynamoDB job item cannot be projected into a valid reference."""


class ChunkMissingError(SparkStageError):
    """The chunk object does not exist at the configured endpoint."""

    def __init__(self, bucket: str, key: str) -> None:
        self.bucket = bucket
        self.key = key
        super().__init__(f"chunk object missing: {bucket}/{key}")


class ChunkCorruptError(SparkStageError):
    """Chunk bytes disagree with the signed size/checksum in the job item."""

    def __init__(
        self,
        bucket: str,
        key: str,
        *,
        stage: str,
        expected_sha256: str | None = None,
        actual_sha256: str | None = None,
        expected_size: int | None = None,
        actual_size: int | None = None,
    ) -> None:
        self.bucket = bucket
        self.key = key
        self.stage = stage
        self.expected_sha256 = expected_sha256
        self.actual_sha256 = actual_sha256
        self.expected_size = expected_size
        self.actual_size = actual_size
        super().__init__(
            f"chunk verification failed at {stage}: {bucket}/{key} "
            f"(expected sha256={expected_sha256}, size={expected_size}; "
            f"actual sha256={actual_sha256}, size={actual_size})"
        )


class DetectorConfigMismatchError(SparkStageError):
    """The detector settings do not match the job's config hash."""

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"detector config hash mismatch: job={expected}, supplied={actual}"
        )


class AttemptOutputError(SparkStageError):
    """The attempt-scoped result object could not be written or verified."""

    def __init__(self, bucket: str, key: str, *, stage: str) -> None:
        self.bucket = bucket
        self.key = key
        self.stage = stage
        super().__init__(f"attempt output verification failed at {stage}: {bucket}/{key}")


# -- immutable job references ------------------------------------------------


@dataclass(frozen=True)
class JobRef:
    """The minimum a Spark task needs to execute one chunk."""

    job_id: str
    run_id: str
    chunk_id: str
    detector_config_hash: str
    source_sha256: str
    chunk_bucket: str
    chunk_key: str
    chunk_sha256: str
    chunk_size_bytes: int
    line_number_start: int
    status: str = "PENDING"
    backend: str = "cpu"
    artifact_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "chunk_id": self.chunk_id,
            "detector_config_hash": self.detector_config_hash,
            "source_sha256": self.source_sha256,
            "chunk_bucket": self.chunk_bucket,
            "chunk_key": self.chunk_key,
            "chunk_sha256": self.chunk_sha256,
            "chunk_size_bytes": self.chunk_size_bytes,
            "line_number_start": self.line_number_start,
            "status": self.status,
            "backend": self.backend,
            "artifact_id": self.artifact_id,
        }


@dataclass(frozen=True)
class S3Connection:
    """Explicit S3 connection details, constructed once on the driver."""

    endpoint_url: str | None
    region: str = "us-east-1"
    access_key_id: str | None = None
    secret_access_key: str | None = None
    max_attempts: int = 5
    addressing_style: str = "path"


@dataclass(frozen=True)
class AttemptOutput:
    """Metadata for the immutable result object written by the driver."""

    bucket: str
    key: str
    sha256: str
    size_bytes: int
    existed: bool = False


def _plain(value: Any) -> Any:
    """Accept both ``SentinelStore`` items and raw DynamoDB wire items."""
    if not isinstance(value, dict) or len(value) != 1:
        return value
    tag, inner = next(iter(value.items()))
    if tag == "S":
        return inner
    if tag == "N":
        return int(inner) if str(inner).lstrip("-").isdigit() else float(inner)
    if tag == "M":
        return {key: _plain(item) for key, item in inner.items()}
    if tag == "L":
        return [_plain(item) for item in inner]
    if tag == "BOOL":
        return bool(inner)
    if tag == "NULL":
        return None
    return value


def _plain_mapping(value: Any) -> Any:
    """Unwrap a DynamoDB item/M mapping and each of its attribute values."""
    unwrapped = _plain(value)
    if not isinstance(unwrapped, Mapping):
        return unwrapped
    return {key: _plain(item) for key, item in unwrapped.items()}


def _text(record: Mapping[str, Any], key: str, position: int) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise JobRefError(f"job record {position} has no valid {key!r}")
    return value


def _digest(record: Mapping[str, Any], key: str, position: int) -> str:
    value = _text(record, key, position)
    if not _SHA256_RE.fullmatch(value):
        raise JobRefError(f"job record {position} has an invalid {key!r} digest")
    return value


def _integer(record: Mapping[str, Any], key: str, position: int, minimum: int) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JobRefError(f"job record {position} has no valid {key!r}")
    number = int(value)
    if number != value or number < minimum:
        raise JobRefError(f"job record {position} has an invalid {key!r}")
    return number


def job_refs_from_records(
    records: Iterable[Mapping[str, Any]],
    *,
    statuses: Iterable[str] | None = None,
) -> tuple[JobRef, ...]:
    """Project DynamoDB job items into minimal immutable references.

    ``SentinelStore.query_jobs`` already deserializes items, but raw wire items
    are accepted too. When ``statuses`` is supplied, items in other states are
    skipped. Duplicate ``job_id`` values are collapsed so a single job is never
    executed twice in one batch.
    """
    allowed = None if statuses is None else frozenset(statuses)
    refs: list[JobRef] = []
    seen: set[str] = set()
    for position, raw in enumerate(records):
        record = _plain_mapping(raw)
        if not isinstance(record, Mapping):
            raise JobRefError(f"job record {position} is not an object")
        status = _text(record, "status", position)
        if allowed is not None and status not in allowed:
            continue
        chunk = _plain_mapping(record.get("chunk"))
        if not isinstance(chunk, Mapping):
            raise JobRefError(f"job record {position} has no chunk descriptor")
        ref = JobRef(
            job_id=_text(record, "job_id", position),
            run_id=_text(record, "run_id", position),
            chunk_id=_text(record, "chunk_id", position),
            detector_config_hash=_digest(record, "detector_config_hash", position),
            source_sha256=_digest(record, "source_sha256", position),
            chunk_bucket=_text(chunk, "bucket", position),
            chunk_key=_text(chunk, "key", position),
            chunk_sha256=_digest(chunk, "sha256", position),
            chunk_size_bytes=_integer(chunk, "size_bytes", position, 0),
            line_number_start=_integer(chunk, "line_number_start", position, 1),
            status=status,
            backend=str(record.get("backend") or "cpu"),
            artifact_id=record.get("artifact_id"),
        )
        if ref.job_id in seen:
            continue
        seen.add(ref.job_id)
        refs.append(ref)
    return tuple(refs)


# -- S3 access ---------------------------------------------------------------


def make_s3_client(connection: S3Connection) -> Any:
    """Build a boto3 S3 client; called independently inside each Spark task."""
    config = Config(
        retries={"max_attempts": connection.max_attempts, "mode": "standard"},
        connect_timeout=3,
        read_timeout=30,
        s3={"addressing_style": connection.addressing_style},
    )
    return boto3.client(
        "s3",
        endpoint_url=connection.endpoint_url or None,
        region_name=connection.region,
        aws_access_key_id=connection.access_key_id,
        aws_secret_access_key=connection.secret_access_key,
        config=config,
    )


def connection_from_env() -> S3Connection:
    """Fallback connection for standalone core use (the wrapper passes its own)."""
    return S3Connection(
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
        region=(
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        ),
        access_key_id=os.environ.get("AWS_ACCESS_KEY_ID") or "test",
        secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY") or "test",
    )


def _s3_client(store_or_client: Any) -> Any:
    candidate = getattr(store_or_client, "s3", None)
    if candidate is not None and hasattr(candidate, "get_object"):
        return candidate
    if hasattr(store_or_client, "get_object"):
        return store_or_client
    raise TypeError("expected a SentinelStore or a boto3 S3 client")


def _is_missing(exc: BaseException) -> bool:
    if isinstance(exc, (FileNotFoundError, KeyError)):
        return True
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        return code in _MISSING_CODES
    return False


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _verify_chunk_bytes(ref: JobRef, data: bytes) -> None:
    actual_sha = sha256_hex(data)
    if len(data) != ref.chunk_size_bytes or actual_sha != ref.chunk_sha256:
        raise ChunkCorruptError(
            ref.chunk_bucket,
            ref.chunk_key,
            stage="body",
            expected_sha256=ref.chunk_sha256,
            actual_sha256=actual_sha,
            expected_size=ref.chunk_size_bytes,
            actual_size=len(data),
        )


def load_chunk(store_or_s3_client: Any, ref: JobRef) -> bytes:
    """Download one chunk and verify it against the job's immutable descriptor.

    A ``head_object`` pre-check bounds the download; a fake client with only
    ``get_object`` is also supported for lightweight tests.
    """
    client = _s3_client(store_or_s3_client)
    if hasattr(client, "head_object"):
        try:
            head = client.head_object(Bucket=ref.chunk_bucket, Key=ref.chunk_key)
        except Exception as exc:
            if _is_missing(exc):
                raise ChunkMissingError(ref.chunk_bucket, ref.chunk_key) from exc
            raise
        head_size = int(head.get("ContentLength", -1))
        if head_size != ref.chunk_size_bytes:
            raise ChunkCorruptError(
                ref.chunk_bucket,
                ref.chunk_key,
                stage="head-size",
                expected_sha256=ref.chunk_sha256,
                expected_size=ref.chunk_size_bytes,
                actual_size=head_size,
            )
        stored_sha = (head.get("Metadata") or {}).get("sha256")
        if stored_sha not in (None, "", ref.chunk_sha256):
            raise ChunkCorruptError(
                ref.chunk_bucket,
                ref.chunk_key,
                stage="head-sha256",
                expected_sha256=ref.chunk_sha256,
                actual_sha256=str(stored_sha),
                expected_size=ref.chunk_size_bytes,
                actual_size=head_size,
            )

    try:
        response = client.get_object(Bucket=ref.chunk_bucket, Key=ref.chunk_key)
    except Exception as exc:
        if _is_missing(exc):
            raise ChunkMissingError(ref.chunk_bucket, ref.chunk_key) from exc
        raise
    body = response["Body"]
    data = body.read() if hasattr(body, "read") else bytes(body)
    if isinstance(data, bytearray) or isinstance(data, memoryview):
        data = bytes(data)
    if not isinstance(data, bytes):  # pragma: no cover - defensive
        raise TypeError("chunk body must be bytes")
    _verify_chunk_bytes(ref, data)
    return data


# -- deterministic execution -------------------------------------------------


def attempt_id_for(job_id: str, source_sha256: str, config_hash: str) -> str:
    """Wall-clock-free attempt identity from job, source and detector config."""
    return "att-" + sha256_hex(
        canonical_json(
            {
                "v": 1,
                "job_id": job_id,
                "source_sha256": source_sha256,
                "detector_config_hash": config_hash,
            }
        )
    )[:24]


def attempt_output_key(run_id: str, job_id: str, attempt_id: str) -> str:
    """Match ``sentinel.worker.result.result_key`` exactly (S6 contract)."""
    return f"runs/{run_id}/attempts/{job_id}/{attempt_id}.json"


def _detector_settings(settings: DetectorSettings | Mapping[str, Any]) -> DetectorSettings:
    if isinstance(settings, DetectorSettings):
        return settings
    if isinstance(settings, Mapping):
        return DetectorSettings.from_dict(settings)
    raise TypeError("settings must be DetectorSettings or a detector config mapping")


def process_job_ref(
    ref: JobRef,
    settings: DetectorSettings | Mapping[str, Any],
    *,
    source_sha256: str,
    run_id: str,
    data: bytes | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Run the CPU detector and return a compact, deterministic result dict.

    ``data`` lets a caller that already fetched the chunk pass verified bytes;
    otherwise the chunk is fetched through ``client`` (or an env-configured
    boto3 client for standalone use). Either way the bytes are checked against
    the job's size and sha256 before detection.
    """
    detector = _detector_settings(settings)
    if not isinstance(source_sha256, str) or not _SHA256_RE.fullmatch(source_sha256):
        raise SparkStageError("source_sha256 must be a 64-character sha256 hex digest")
    if run_id != ref.run_id:
        raise JobRefError(
            f"job {ref.job_id} belongs to run {ref.run_id}, not {run_id}"
        )
    if detector.config_hash != ref.detector_config_hash:
        raise DetectorConfigMismatchError(ref.detector_config_hash, detector.config_hash)

    if data is None:
        chunk = load_chunk(
            client if client is not None else make_s3_client(connection_from_env()),
            ref,
        )
    else:
        chunk = bytes(data)
        _verify_chunk_bytes(ref, chunk)

    detection = detect(
        chunk,
        settings=detector,
        source_sha256=source_sha256,
        run_id=run_id,
        chunk_id=ref.chunk_id,
        line_number_start=ref.line_number_start,
    )
    alerts = [alert.to_dict() for alert in detection.alerts]
    metrics = dict(sorted(detection.metrics.items()))
    attempt_id = attempt_id_for(ref.job_id, source_sha256, detector.config_hash)
    output_digest = sha256_hex(
        canonical_json(
            {
                "schema_version": RESULT_SCHEMA_VERSION,
                "run_id": run_id,
                "job_id": ref.job_id,
                "chunk_id": ref.chunk_id,
                "attempt_id": attempt_id,
                "algorithm_version": detection.algorithm_version,
                "metrics": metrics,
                "alerts": alerts,
            }
        )
    )
    result: dict[str, Any] = {
        "job_id": ref.job_id,
        "run_id": run_id,
        "chunk_id": ref.chunk_id,
        "actual_backend": SPARK_BACKEND,
        "algorithm_version": detection.algorithm_version,
        "metrics": metrics,
        "alerts": alerts,
        "output_sha256": output_digest,
        "attempt_id": attempt_id,
    }
    return dict(sorted(result.items()))


# -- immutable attempt output ------------------------------------------------


def write_attempt_output(
    s3_client: Any, bucket: str, result: Mapping[str, Any]
) -> AttemptOutput:
    """Write ``runs/{run_id}/attempts/{job_id}/{attempt_id}.json`` and verify it.

    The write is create-if-absent. A replay of the same deterministic attempt
    reuses the object only when its bytes still match; any divergence is a hard
    verification failure, never a silent overwrite.
    """
    client = _s3_client(s3_client)
    for key_name in ("run_id", "job_id", "attempt_id", "output_sha256"):
        value = result.get(key_name)
        if not isinstance(value, str) or not value:
            raise SparkStageError(f"result has no valid {key_name!r}")

    key = attempt_output_key(
        str(result["run_id"]), str(result["job_id"]), str(result["attempt_id"])
    )
    body = canonical_json(dict(result)) + b"\n"
    digest = sha256_hex(body)
    metadata = {
        "sha256": digest,
        "output-sha256": str(result["output_sha256"]),
        "run-id": str(result["run_id"]),
        "job-id": str(result["job_id"]),
        "attempt-id": str(result["attempt_id"]),
        "backend": str(result.get("actual_backend") or SPARK_BACKEND),
    }
    existed = False
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            Metadata=metadata,
            ContentType="application/json",
            IfNoneMatch="*",
        )
    except ClientError as exc:
        if _code(exc) not in _EXISTS_CODES:
            raise
        existed = True

    if existed:
        response = client.get_object(Bucket=bucket, Key=key)
        stored = response["Body"].read()
        if sha256_hex(stored) != digest:
            raise AttemptOutputError(bucket, key, stage="existing-body")

    head = client.head_object(Bucket=bucket, Key=key)
    if int(head.get("ContentLength", -1)) != len(body):
        raise AttemptOutputError(bucket, key, stage="head-size")
    if (head.get("Metadata") or {}).get("sha256") != digest:
        raise AttemptOutputError(bucket, key, stage="head-metadata")

    return AttemptOutput(
        bucket=bucket,
        key=key,
        sha256=digest,
        size_bytes=len(body),
        existed=existed,
    )
