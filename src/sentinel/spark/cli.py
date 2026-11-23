"""CLI for the optional Spark batch stage.

``plan`` is a read-only driver operation and works without pyspark. ``run``
needs the optional dependency and fails with an actionable message if it is
missing.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from ..api.store import SentinelStore
from .config import SparkSettings
from .core import (
    SPARK_BACKEND,
    AttemptOutputError,
    ChunkCorruptError,
    ChunkMissingError,
    DetectorConfigMismatchError,
    JobRefError,
    SparkStageError,
)
from .job import (
    PySparkUnavailableError,
    ResultCommitter,
    plan_jobs,
    run_spark_job,
)

_ERROR_EXIT = 1
_VERIFICATION_EXIT = 2
_PYSPARK_EXIT = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sentinel.spark.cli",
        description="Optional Spark batch executor for Sentinel runs",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("run", "execute a bounded Spark batch and write attempt outputs"),
        ("plan", "print the job refs that would be processed"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--run-id", required=True, help="run id to process")
        command.add_argument("--limit", type=int, default=None, help="max jobs")
        command.add_argument(
            "--master", default=None, help="Spark master (default: local[*])"
        )
        command.add_argument(
            "--endpoint-url", default=None, help="S3/DynamoDB endpoint override"
        )
        command.add_argument(
            "--results-bucket", default=None, help="results bucket override"
        )
        command.add_argument("--app-name", default=None, help="Spark app name")
        command.add_argument(
            "--committer",
            default=None,
            help="result committer: logging|worker-fenced (default: logging)",
        )
        command.add_argument("--shuffle-partitions", type=int, default=None)
        command.add_argument("--max-parallelism", type=int, default=None)
        command.add_argument("--page-size", type=int, default=None)
        command.add_argument("--max-pages", type=int, default=None)
    return parser


def _settings_from_args(args: argparse.Namespace) -> SparkSettings:
    return SparkSettings.from_env().overrides(
        master=args.master,
        app_name=args.app_name,
        shuffle_partitions=args.shuffle_partitions,
        max_parallelism=args.max_parallelism,
        page_size=args.page_size,
        max_pages=args.max_pages,
        limit=args.limit,
        results_bucket=args.results_bucket,
        endpoint_url=args.endpoint_url,
        committer=args.committer,
    )


def _emit(payload: dict[str, Any], *, stream: Any = None) -> None:
    print(json.dumps(payload, sort_keys=True), file=stream or sys.stdout)


def main(
    argv: Sequence[str] | None = None,
    *,
    store: Any | None = None,
    committer: ResultCommitter | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = _settings_from_args(args)
        active_store = store if store is not None else SentinelStore(settings.api)
        if args.command == "plan":
            plan = plan_jobs(args.run_id, settings=settings, store=active_store)
            _emit(
                {
                    "run_id": plan.run_id,
                    "backend": SPARK_BACKEND,
                    "jobs_selected": len(plan.refs),
                    "job_refs": [ref.as_dict() for ref in plan.refs],
                }
            )
            return 0

        summary = run_spark_job(
            args.run_id, settings=settings, store=active_store, committer=committer
        )
        _emit(summary)
        return 0
    except PySparkUnavailableError as exc:
        _emit(
            {"ok": False, "error_class": "pyspark_unavailable", "error": str(exc)},
            stream=sys.stderr,
        )
        return _PYSPARK_EXIT
    except (
        ChunkMissingError,
        ChunkCorruptError,
        AttemptOutputError,
        DetectorConfigMismatchError,
        JobRefError,
    ) as exc:
        _emit(
            {"ok": False, "error_class": type(exc).__name__, "error": str(exc)},
            stream=sys.stderr,
        )
        return _VERIFICATION_EXIT
    except (SparkStageError, ValueError) as exc:
        _emit(
            {"ok": False, "error_class": type(exc).__name__, "error": str(exc)},
            stream=sys.stderr,
        )
        return _VERIFICATION_EXIT
    except Exception as exc:  # noqa: BLE001 - CLI boundary, never a traceback
        _emit(
            {"ok": False, "error_class": type(exc).__name__, "error": str(exc)},
            stream=sys.stderr,
        )
        return _ERROR_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
