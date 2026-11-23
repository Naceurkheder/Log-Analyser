"""CLI surface is import-safe and exposes the documented subcommands."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sentinel.worker.cli import build_parser, main


def test_parser_subcommands() -> None:
    parser = build_parser()
    for argv in (
        ["serve", "--backend", "cpu", "--once"],
        ["serve", "--backend", "gpu", "--loop"],
        ["reconcile", "--once"],
        ["dlq-inspect", "--backend", "cpu"],
        ["dlq-replay", "--backend", "gpu", "--reason", "root cause fixed", "--limit", "2"],
    ):
        args = parser.parse_args(argv)
        assert callable(args.func)


def test_dlq_replay_requires_reason() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["dlq-replay", "--backend", "cpu"])


def test_reconcile_cli_runs_once(
    aws: Any, artifact_file: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["reconcile", "--once"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "reconcile"
    assert payload["jobs_republished"] == 0
    assert payload["runs_republished"] == 0


def test_dlq_inspect_cli_reports_empty(
    aws: Any, artifact_file: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["dlq-inspect", "--backend", "cpu"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "dlq-inspect"
    assert payload["count"] == 0


def test_dlq_replay_cli_requires_reason_at_runtime(
    aws: Any, artifact_file: Any
) -> None:
    from sentinel.worker.errors import MessageError

    with pytest.raises(MessageError):
        main(["dlq-replay", "--backend", "cpu", "--reason", "x"])
