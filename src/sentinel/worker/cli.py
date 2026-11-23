"""Command line entrypoint: ``python -m sentinel.worker.cli``.

Importing this module only defines parsers and functions; clients, threads and
HTTP servers are created when a command is actually invoked.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Sequence

from .config import WorkerSettings
from .dlq import inspect_dlq, replay_dlq
from .reconcile import reconcile_outbox
from .store import WorkerStore
from .worker import Worker


def _print(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, default=str))


def _settings(args: argparse.Namespace, *, backend: str | None = None) -> WorkerSettings:
    if getattr(args, "metrics_port", None) is not None:
        os.environ["SENTINEL_WORKER_METRICS_PORT"] = str(args.metrics_port)
    if getattr(args, "worker_id", None):
        os.environ["SENTINEL_WORKER_ID"] = str(args.worker_id)
    return WorkerSettings.from_env(backend or getattr(args, "backend", None))


def cmd_serve(args: argparse.Namespace) -> int:
    settings = _settings(args)
    worker = Worker(settings)
    worker.serve(once=args.once)
    _print({"command": "serve", "backend": settings.capability, "metrics": worker.metrics.snapshot()})
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = WorkerStore(settings.api)
    report = reconcile_outbox(store, settings)
    _print({"command": "reconcile", **report.as_dict()})
    return 0


def cmd_dlq_inspect(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = WorkerStore(settings.api)
    items = inspect_dlq(store, settings, args.backend)
    _print({"command": "dlq-inspect", "backend": args.backend, "count": len(items), "items": items})
    return 0


def cmd_dlq_replay(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = WorkerStore(settings.api)
    report = replay_dlq(
        store,
        settings,
        args.backend,
        reason=args.reason,
        limit=args.limit,
        operator=args.operator,
    )
    _print({"command": "dlq-replay", **report.as_dict()})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sentinel.worker.cli",
        description="Sentinel capability-specific workers and recovery tooling",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="consume jobs for one backend")
    serve.add_argument("--backend", choices=["cpu", "gpu"], required=True)
    mode = serve.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="process one bounded batch")
    mode.add_argument("--loop", action="store_true", help="run until SIGTERM (default)")
    serve.add_argument("--metrics-port", type=int, default=None)
    serve.add_argument("--worker-id", default=None)
    serve.set_defaults(func=cmd_serve)

    reconcile = sub.add_parser("reconcile", help="republish stale unpublished records")
    reconcile.add_argument(
        "--once", action="store_true", help="run one bounded scan (default)"
    )
    reconcile.add_argument("--metrics-port", type=int, default=None)
    reconcile.set_defaults(func=cmd_reconcile)

    inspect = sub.add_parser("dlq-inspect", help="show quarantined messages")
    inspect.add_argument("--backend", choices=["cpu", "gpu"], required=True)
    inspect.add_argument("--metrics-port", type=int, default=None)
    inspect.set_defaults(func=cmd_dlq_inspect)

    replay = sub.add_parser("dlq-replay", help="explicitly requeue quarantined work")
    replay.add_argument("--backend", choices=["cpu", "gpu"], required=True)
    replay.add_argument("--reason", required=True, help="mandatory operator reason")
    replay.add_argument("--limit", type=int, default=None)
    replay.add_argument("--operator", default=None)
    replay.add_argument("--metrics-port", type=int, default=None)
    replay.set_defaults(func=cmd_dlq_replay)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - exercised via the CLI module
    raise SystemExit(main())
