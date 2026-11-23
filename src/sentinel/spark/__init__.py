"""Optional Apache Spark batch execution stage for Sentinel.

The package is importable without pyspark: only :mod:`sentinel.spark.job`
imports pyspark, and it does so lazily inside functions. Install the optional
dependency with ``pip install -r requirements-spark.txt`` before using the
``run`` path; ``plan`` and all core helpers work with the base environment.
"""

from __future__ import annotations

from .core import (
    ELIGIBLE_STATUSES,
    SPARK_BACKEND,
    AttemptOutput,
    AttemptOutputError,
    ChunkCorruptError,
    ChunkMissingError,
    DetectorConfigMismatchError,
    JobRef,
    JobRefError,
    S3Connection,
    SparkStageError,
    attempt_id_for,
    attempt_output_key,
    connection_from_env,
    job_refs_from_records,
    load_chunk,
    make_s3_client,
    process_job_ref,
    write_attempt_output,
)

__all__ = [
    "ELIGIBLE_STATUSES",
    "SPARK_BACKEND",
    "AttemptOutput",
    "AttemptOutputError",
    "ChunkCorruptError",
    "ChunkMissingError",
    "DetectorConfigMismatchError",
    "JobRef",
    "JobRefError",
    "S3Connection",
    "SparkStageError",
    "attempt_id_for",
    "attempt_output_key",
    "connection_from_env",
    "job_refs_from_records",
    "load_chunk",
    "make_s3_client",
    "process_job_ref",
    "write_attempt_output",
]
