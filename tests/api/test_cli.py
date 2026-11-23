"""CLI upload/submit paths."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from sentinel.api import cli
from sentinel.api.contract import canonical_json


def _payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "dataset": {"bucket": "incoming", "key": "datasets/sample.log"},
        "detector": {
            "rules": [{"rule_id": "auth-failed-password", "pattern": "Failed password"}]
        },
        "backend_policy": "gpu",
    }


def test_upload_puts_object_and_reports_digest(
    aws: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "input.log"
    source.write_bytes(b"line one\nline two\n")

    code = cli.main(["upload", str(source), "--key", "datasets/cli.log"])

    assert code == 0
    reported = json.loads(capsys.readouterr().out)
    assert reported["size_bytes"] == source.stat().st_size
    assert reported["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    stored = aws.s3.get_object(Bucket="incoming", Key="datasets/cli.log")
    assert stored["Body"].read() == source.read_bytes()


def test_upload_rejects_missing_file(tmp_path: Path) -> None:
    code = cli.main(
        ["upload", str(tmp_path / "nope.log"), "--key", "datasets/nope.log"]
    )

    assert code == 2


def test_upload_rejects_empty_file(
    aws: Any, tmp_path: Path
) -> None:
    source = tmp_path / "empty.log"
    source.write_bytes(b"")

    code = cli.main(["upload", str(source), "--key", "datasets/empty.log"])

    assert code == 2


def test_submit_derives_stable_idempotency_key(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = tmp_path / "run.json"
    request_path.write_text(json.dumps(_payload()), encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_post(
        url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> tuple[int, Any]:
        captured.update(url=url, payload=payload, headers=headers)
        return 201, {"run_id": "run_test", "created": True}

    monkeypatch.setattr(cli, "_post_json", fake_post)
    code = cli.main(
        ["submit", "--request", str(request_path), "--api-url", "http://api.local"]
    )

    assert code == 0
    expected = hashlib.sha256(canonical_json(_payload()).encode()).hexdigest()[:32]
    assert captured["url"] == "http://api.local/v1/runs"
    assert captured["headers"]["Idempotency-Key"] == expected
    assert json.loads(capsys.readouterr().out)["run_id"] == "run_test"


def test_submit_honours_explicit_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path = tmp_path / "run.json"
    request_path.write_text(json.dumps(_payload()), encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_post(url: str, payload: dict[str, Any], headers: dict[str, str]):
        captured.update(headers=headers)
        return 200, {"run_id": "run_test", "created": False}

    monkeypatch.setattr(cli, "_post_json", fake_post)
    cli.main(
        [
            "submit",
            "--request",
            str(request_path),
            "--idempotency-key",
            "explicit-key-1234",
        ]
    )

    assert captured["headers"]["Idempotency-Key"] == "explicit-key-1234"


def test_submit_rejects_invalid_request_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path = tmp_path / "bad.json"
    request_path.write_text(json.dumps({"backend_policy": "gpu"}), encoding="utf-8")

    def fail(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("must not reach the network")

    monkeypatch.setattr(cli, "_post_json", fail)

    assert cli.main(["submit", "--request", str(request_path)]) == 2
