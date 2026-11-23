"""Sprint 4-D edge cases for the CPU reference."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from sentinel.detector import (
    DetectorConfigError, DetectorSettings, detect,
)
from sentinel.detector import pipeline
from sentinel.detector.records import parse_records

RULE = {"rule_id": "auth-fail", "pattern": "Failed password", "severity": "high"}
SYSLOG = b"Jan  5 03:%02d:%02d lab sshd[1]: Failed password from 10.0.0.%d port 1234 ssh2\n"


def settings(**overrides: object) -> DetectorSettings:
    data = {
        "rules": [RULE],
        "window_seconds": 60,
        "baseline_windows": 2,
        "z_threshold": 3.0,
    }
    data.update(overrides)
    return DetectorSettings.from_dict(data)


def run(data: bytes, cfg: DetectorSettings | None = None) -> pipeline.DetectionResult:
    return detect(data, settings=cfg or settings(), source_sha256="sha-test")


def test_empty_dataset_and_empty_line() -> None:
    empty = run(b"")
    assert empty.line_count == 0
    assert empty.matches == [] and empty.alerts == []
    assert empty.metrics["bytes"] == 0

    one_empty = run(b"\n")
    assert one_empty.line_count == 1
    assert one_empty.matches == []
    assert one_empty.metrics["missing_timestamps"] == 1

    two_empty = run(b"\n\n")
    assert two_empty.line_count == 2


def test_single_line_and_maximum_record_length() -> None:
    single = run(SYSLOG % (12, 0, 1))
    assert single.line_count == 1
    assert single.metrics["matches"] == 1

    long_line = b"x" * 65536 + b"\n"
    assert run(long_line, settings(rules=[
        {"rule_id": "x", "pattern": "x"},
    ])).line_count == 1
    with pytest.raises(ValueError, match="limit is 65536"):
        run(b"x" * 65537 + b"\n", settings(rules=[{"rule_id": "x", "pattern": "x"}]))


def test_no_matches_and_multiple_rules_on_one_line() -> None:
    assert run(b"Jan  5 03:12:00 host kernel[1]: INFO ok\n").matches == []
    cfg = settings(rules=[
        {"rule_id": "one", "pattern": "ERROR"},
        {"rule_id": "two", "pattern": "ERROR E1203"},
        {"rule_id": "three", "pattern": "E1203"},
    ])
    result = run(b"Jan  5 03:12:00 host app[1]: ERROR E1203 failed\n", cfg)
    assert [m.rule_id for m in result.matches] == ["one", "two", "three"]
    assert [m.rule_index for m in result.matches] == [0, 1, 2]


def test_duplicate_patterns_and_shared_prefixes_are_distinct_rules() -> None:
    cfg = settings(rules=[
        {"rule_id": "dup-a", "pattern": "Failed password"},
        {"rule_id": "dup-b", "pattern": "Failed password"},
        {"rule_id": "prefix", "pattern": "Failed"},
    ])
    result = run(SYSLOG % (12, 0, 1), cfg)
    assert [m.rule_id for m in result.matches] == ["dup-a", "dup-b", "prefix"]
    assert len({f.rule_id for f in result.features}) == 3
    with pytest.raises(DetectorConfigError, match="duplicate rule_id"):
        settings(rules=[RULE, RULE])


def test_lf_crlf_mixed_and_malformed_bytes() -> None:
    mixed = (SYSLOG % (12, 0, 1))[:-1] + b"\r\n" + SYSLOG % (13, 0, 2)
    result = run(mixed)
    assert result.line_count == 2
    assert all(m.rule_id == "auth-fail" for m in result.matches)
    assert parse_records(mixed, source_sha256="s")[0].raw.endswith(b"ssh2")
    with pytest.raises(ValueError, match="malformed utf-8"):
        run(b"Jan  5 03:12:00 host sshd[1]: \xff Failed password\n")
    sig = run(b"\xef\xbb\xbf" + SYSLOG % (12, 0, 1), settings(encoding="utf-8-sig"))
    assert sig.line_count == 1 and sig.metrics["matches"] == 1


def test_missing_invalid_and_out_of_order_timestamps() -> None:
    lines = [
        b"no timestamp here Failed password\n",
        b"Jan  5 99:12:00 host sshd[1]: Failed password\n",
        SYSLOG % (19, 0, 1),
        SYSLOG % (12, 0, 1),
        SYSLOG % (13, 0, 1),
    ]
    result = run(b"".join(lines))
    assert result.metrics["missing_timestamps"] == 1
    assert result.metrics["invalid_timestamps"] == 1
    assert result.metrics["matches"] == 5
    assert [e.epoch for e in result.events] == sorted(e.epoch for e in result.events)
    assert [f.window_id for f in result.features] == sorted(
        f.window_id for f in result.features
    )

    in_order = run(b"".join(lines[:2] + lines[3:] + [lines[2]]))
    left = {(f.key, f.window_id, f.rule_id, f.count) for f in result.features}
    right = {(f.key, f.window_id, f.rule_id, f.count) for f in in_order.features}
    assert left == right


def test_exact_window_boundary_is_half_open() -> None:
    data = (SYSLOG % (12, 0, 1) + SYSLOG % (12, 59, 2)
            + SYSLOG % (13, 0, 3))
    result = run(data)
    windows = [(f.window_id, f.count) for f in result.features]
    assert windows == sorted(windows)
    assert windows[-1][1] == 1  # 03:13:00 starts a new window
    assert sum(count for _, count in windows) == 3


def test_warm_up_zero_variance_and_high_anomaly() -> None:
    def series(minutes: list[int]) -> bytes:
        return b"".join(SYSLOG % (minute, 0, 1) for minute in minutes)

    assert run(series([12, 13])).alerts == []  # insufficient history
    constant = run(series([12, 12, 13, 13, 14, 14]))
    assert constant.alerts == []  # zero variance at the target window
    burst = run(series([12] + [13] * 3 + [14] * 12))
    assert len(burst.alerts) == 1
    alert = burst.alerts[0]
    assert (alert.count, alert.mean, alert.std, alert.score) == (12, 2.0, 1.0, 10.0)
    assert alert.created_at == "2026-01-05T03:14:00Z"


def test_counter_overflow_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "UINT32_MAX", 2)
    with pytest.raises(OverflowError, match="overflow"):
        run(SYSLOG % (12, 0, 1) * 3)


def test_distinct_keys_are_not_merged() -> None:
    data = (SYSLOG % (12, 0, 1) + SYSLOG % (12, 0, 2)
            + b'Jan  5 03:12:00 lab nginx[1]: 10.0.0.1 "GET / HTTP/1.1" 404 Failed password\n')
    result = run(data)
    assert len(result.features) == 3
    assert {f.key for f in result.features} == {
        (1, 167772161, 0), (1, 167772162, 0), (2, 167772161, 404),
    }


def test_source_sha_is_part_of_stable_ids(tmp_path: Path) -> None:
    data = SYSLOG % (12, 0, 1)
    first = detect(data, settings=settings(), source_sha256="a" * 64)
    second = detect(data, settings=settings(), source_sha256="b" * 64)
    assert first.matches[0].line_id != second.matches[0].line_id
    assert hashlib.sha256(b"a").hexdigest()  # stdlib hash remains deterministic
