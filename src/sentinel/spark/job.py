"""Thin Spark wrapper: bounded driver planning and one boto3 client per task.

pyspark is imported lazily inside functions so ``plan``, the core helpers and
this module itself all work in a base installation that has no pyspark.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from ..api.store import SentinelStore
from ..detector.semantics import DetectorSettings
from .commit import committer_for
from .config import SparkSettings
from .core import (
    ELIGIBLE_STATUSES,
    SPARK_BACKEND,
    AttemptOutput,
    ChunkCorruptError,
    ChunkMissingError,
    DetectorConfigMismatchError,
    JobRef,
    JobRefError,
    S3Connection,
    SparkStageError,
    attempt_id_for,
    job_refs_from_records,
    make_s3_client,
    process_job_ref,
    write_attempt_output,
)

log = logging.getLogger("sentinel.spark.job")

_LOG_EVENTS = frozenset(
    {
        "spark_plan_loaded",
        "spark_batch_started",
        "spark_task_complete",
        "spark_task_failed",
        "spark_result_written",
        "spark_result_not_committed",
        "spark_batch_complete",
        "spark_batch_failed",
    }
)


class PySparkUnavailableError(SparkStageError):
    """Raised when the optional pyspark dependency is missing."""


def _log(level: int, event: str, **fields: Any) -> None:
    if event not in _LOG_EVENTS:
        raise ValueError(f"unbounded log event: {event}")
    payload = {
        "service": "spark",
        "backend": SPARK_BACKEND,
        **{name: value for name, value in fields.items() if value is not None},
    }
    log.log(level, event, extra={"event_fields": payload})


def _import_pyspark() -> tuple[Any, Any]:
    try:
        from pyspark import SparkConf
        from pyspark.sql import SparkSession
    except ImportError as exc:
        raise PySparkUnavailableError(
            "pyspark is not installed; install the optional Spark dependencies "
            "with `pip install -r requirements-spark.txt` before running this stage"
        ) from exc
    return SparkConf, SparkSession


def build_spark_session(settings: SparkSettings) -> Any:
    """Create/get a bounded local-mode SparkSession (pyspark imported here)."""
    SparkConf, SparkSession = _import_pyspark()
    conf = (
        SparkConf()
        .setAppName(settings.app_name)
        .setMaster(settings.master)
        .set("spark.sql.shuffle.partitions", str(settings.shuffle_partitions))
        .set("spark.default.parallelism", str(settings.max_parallelism))
        .set("spark.ui.enabled", "false")
    )
    local_ip = os.environ.get("SPARK_LOCAL_IP")
    if local_ip:
        conf = conf.set("spark.driver.host", local_ip).set(
            "spark.driver.bindAddress", local_ip
        )
    return SparkSession.builder.config(conf=conf).getOrCreate()


@dataclass(frozen=True)
class SparkPlan:
    """Driver-side bounded selection for one run."""

    run_id: str
    detector: dict[str, Any]
    refs: tuple[JobRef, ...]


def _store_for(settings: SparkSettings, store: Any | None) -> Any:
    return store if store is not None else SentinelStore(settings.api)


def _build_plan(run_id: str, *, settings: SparkSettings, store: Any) -> SparkPlan:
    run = store.get_run(run_id)
    if run is None:
        raise SparkStageError(f"run not found: {run_id}")
    detector = run.get("detector")
    if not isinstance(detector, Mapping):
        raise SparkStageError(f"run {run_id} has no detector configuration")
    detector_dict = dict(detector)
    detector_settings = DetectorSettings.from_dict(detector_dict)
    expected_hash = detector_settings.config_hash
    recorded_hash = run.get("detector_config_hash")
    if recorded_hash is not None and recorded_hash != expected_hash:
        raise DetectorConfigMismatchError(str(recorded_hash), expected_hash)

    refs: list[JobRef] = []
    cursor: dict[str, Any] | None = None
    for page_number in range(settings.max_pages):
        remaining = settings.limit - len(refs)
        if remaining <= 0:
            break
        page_limit = min(settings.page_size, remaining)
        items, cursor = store.query_jobs(run_id, page_limit, start_key=cursor)
        page_refs = job_refs_from_records(items, statuses=ELIGIBLE_STATUSES)
        for ref in page_refs:
            # Spark runs the CPU detector only; GPU-routed jobs stay with S6.
            if ref.backend != "cpu":
                continue
            if ref.run_id != run_id:
                raise SparkStageError(
                    f"job {ref.job_id} from query belongs to run {ref.run_id}"
                )
            if ref.detector_config_hash != expected_hash:
                raise DetectorConfigMismatchError(
                    ref.detector_config_hash, expected_hash
                )
            refs.append(ref)
            if len(refs) >= settings.limit:
                break
        if not cursor or (page_number == settings.max_pages - 1 and len(refs) < settings.limit):
            break
    plan = SparkPlan(run_id=run_id, detector=detector_dict, refs=tuple(refs))
    _log(
        logging.INFO,
        "spark_plan_loaded",
        run_id=run_id,
        jobs_selected=len(plan.refs),
        limit=settings.limit,
    )
    return plan


def plan_jobs(
    run_id: str, *, settings: SparkSettings, store: Any | None = None
) -> SparkPlan:
    """Load the bounded job selection without importing pyspark."""
    return _build_plan(run_id, settings=settings, store=_store_for(settings, store))


def _error_attributes(exc: BaseException) -> dict[str, Any]:
    """Picklable subset of an exception's fields, for driver-side rebuild."""
    scalar = (str, int, float, bool, type(None))
    return {
        name: value
        for name, value in getattr(exc, "__dict__", {}).items()
        if isinstance(value, scalar)
    }


def _rebuild_task_error(info: Mapping[str, Any]) -> Exception:
    """Recreate a typed stage error from a task envelope.

    Spark can wrap Python worker exceptions in a JVM error, so tasks return a
    small envelope and the driver restores the original error class. This keeps
    CLI exit codes and messages stable for verification failures.
    """
    name = str(info.get("class") or "SparkStageError")
    message = str(info.get("message") or "spark task failed")
    attributes = dict(info.get("attributes") or {})
    if name == ChunkMissingError.__name__:
        return ChunkMissingError(
            str(attributes.get("bucket", "")), str(attributes.get("key", ""))
        )
    if name == ChunkCorruptError.__name__:
        return ChunkCorruptError(
            str(attributes.get("bucket", "")),
            str(attributes.get("key", "")),
            stage=str(attributes.get("stage", "body")),
            expected_sha256=attributes.get("expected_sha256"),
            actual_sha256=attributes.get("actual_sha256"),
            expected_size=attributes.get("expected_size"),
            actual_size=attributes.get("actual_size"),
        )
    if name == DetectorConfigMismatchError.__name__:
        return DetectorConfigMismatchError(
            str(attributes.get("expected", "")), str(attributes.get("actual", ""))
        )
    if name == JobRefError.__name__:
        return JobRefError(message)
    return SparkStageError(message)


def _raise_task_errors(items: Iterable[Any]) -> None:
    for item in items:
        if isinstance(item, Mapping) and "_spark_error" in item:
            raise _rebuild_task_error(item["_spark_error"])


def _task_execute(
    ref: JobRef,
    detector: dict[str, Any],
    connection: S3Connection,
    expected_run_id: str,
) -> dict[str, Any]:
    """Execute one ref in a Spark task with a task-local boto3 client.

    Success returns the result dict; failure returns a typed error envelope
    that the driver rebuilds (see :func:`_raise_task_errors`).
    """
    attempt_id = attempt_id_for(ref.job_id, ref.source_sha256, ref.detector_config_hash)
    try:
        client = make_s3_client(connection)
        result = process_job_ref(
            ref,
            DetectorSettings.from_dict(detector),
            source_sha256=ref.source_sha256,
            run_id=expected_run_id,
            client=client,
        )
    except Exception as exc:
        _log(
            logging.ERROR,
            "spark_task_failed",
            run_id=expected_run_id,
            job_id=ref.job_id,
            attempt_id=attempt_id,
            error_class=type(exc).__name__,
        )
        return {
            "_spark_error": {
                "class": type(exc).__name__,
                "message": str(exc),
                "attributes": _error_attributes(exc),
            }
        }
    _log(
        logging.INFO,
        "spark_task_complete",
        run_id=expected_run_id,
        job_id=ref.job_id,
        attempt_id=result["attempt_id"],
        alerts=len(result["alerts"]),
    )
    return result


@runtime_checkable
class ResultCommitter(Protocol):
    """Boundary for adapting the Spark stage to the S6 durable worker store.

    The driver has already written the immutable attempt object when this is
    called. A committer decides whether and how that output is acknowledged;
    the default :class:`LoggingCommitter` performs no durable write at all.
    """

    def commit(
        self,
        *,
        result: dict[str, Any],
        output: AttemptOutput,
        settings: SparkSettings,
        store: Any,
    ) -> dict[str, Any]:
        ...


class LoggingCommitter:
    """Default committer: logs each result, fakes no durable commit."""

    name = "logging"

    def commit(
        self,
        *,
        result: dict[str, Any],
        output: AttemptOutput,
        settings: SparkSettings,
        store: Any,
    ) -> dict[str, Any]:
        _log(
            logging.INFO,
            "spark_result_not_committed",
            run_id=result["run_id"],
            job_id=result["job_id"],
            attempt_id=result["attempt_id"],
            result_key=output.key,
        )
        return {"committed": False, "durable": False, "committer": self.name}


def _abort_claims(committer: Any, claims: Any, reason: str) -> None:
    """Best-effort release of any claims prepared before a Spark failure."""
    if claims is None:
        return
    abort = getattr(committer, "abort", None)
    if not callable(abort):
        return
    try:
        abort(claims=claims, reason=reason[:500])
    except Exception as exc:  # pragma: no cover - defensive cleanup
        log.warning(
            "spark_claim_abort_failed",
            extra={"event_fields": {"error": type(exc).__name__}},
        )


def run_spark_job(
    run_id: str,
    *,
    settings: SparkSettings,
    store: Any | None = None,
    committer: ResultCommitter | None = None,
    session: Any | None = None,
) -> dict[str, Any]:
    """Run a bounded Spark batch, write attempt outputs and return a summary."""
    active_store = _store_for(settings, store)
    active_committer = committer or committer_for(settings.committer)
    claims: Any = None
    created_session = session is None
    spark = session if session is not None else build_spark_session(settings)
    try:
        plan = _build_plan(run_id, settings=settings, store=active_store)
        prepare = getattr(active_committer, "prepare", None)
        if callable(prepare):
            claims = prepare(plan=plan, settings=settings, store=active_store)
        num_slices = min(settings.max_parallelism, max(1, len(plan.refs)))
        _log(
            logging.INFO,
            "spark_batch_started",
            run_id=run_id,
            jobs_selected=len(plan.refs),
            num_slices=num_slices,
            master=settings.master,
        )
        results: list[dict[str, Any]] = []
        if plan.refs:
            rdd = spark.sparkContext.parallelize(plan.refs, numSlices=num_slices)
            results = list(
                rdd.map(
                    lambda ref: _task_execute(
                        ref, dict(plan.detector), settings.connection, plan.run_id
                    )
                ).collect()
            )
        _raise_task_errors(results)

        output_client = getattr(active_store, "s3", None) or make_s3_client(
            settings.connection
        )
        attempts: list[dict[str, Any]] = []
        commits: list[dict[str, Any]] = []
        for result in results:
            output = write_attempt_output(
                output_client, settings.results_bucket, result
            )
            _log(
                logging.INFO,
                "spark_result_written",
                run_id=result["run_id"],
                job_id=result["job_id"],
                attempt_id=result["attempt_id"],
                result_key=output.key,
            )
            decision = active_committer.commit(
                result=result,
                output=output,
                settings=settings,
                store=active_store,
            )
            if not isinstance(decision, Mapping):
                raise SparkStageError("result committer must return a mapping")
            commits.append(dict(decision))
            attempts.append(
                {
                    "job_id": result["job_id"],
                    "attempt_id": result["attempt_id"],
                    "result_key": output.key,
                    "result_sha256": output.sha256,
                    "alerts": len(result["alerts"]),
                }
            )

        summary = {
            "run_id": run_id,
            "backend": SPARK_BACKEND,
            "jobs_selected": len(plan.refs),
            "jobs_processed": len(results),
            "attempts_written": len(attempts),
            "commits_acknowledged": sum(
                1 for item in commits if bool(item.get("committed"))
            ),
            "committer": settings.committer,
            "results_bucket": settings.results_bucket,
            "attempts": attempts,
        }
        _log(
            logging.INFO,
            "spark_batch_complete",
            run_id=run_id,
            jobs_processed=len(results),
            attempts_written=len(attempts),
        )
        return summary
    except (
        ChunkMissingError,
        ChunkCorruptError,
        DetectorConfigMismatchError,
    ) as exc:
        _abort_claims(active_committer, claims, str(exc) or type(exc).__name__)
        _log(
            logging.ERROR,
            "spark_batch_failed",
            run_id=run_id,
            error_class=type(exc).__name__,
        )
        raise
    except Exception as exc:
        _abort_claims(active_committer, claims, str(exc) or type(exc).__name__)
        _log(
            logging.ERROR,
            "spark_batch_failed",
            run_id=run_id,
            error_class=type(exc).__name__,
        )
        raise
    finally:
        if created_session and spark is not None:
            spark.stop()
