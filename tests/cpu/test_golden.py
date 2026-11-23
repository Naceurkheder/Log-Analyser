"""Golden fixture: full logical output is byte-for-byte stable."""

from __future__ import annotations

import json
from pathlib import Path

from sentinel.detector import DetectorSettings, detect


def test_golden_output_matches_committed_fixture(
    golden_bytes: bytes, golden_sha: str,
    golden_expected_path: Path, settings: DetectorSettings,
) -> None:
    actual = detect(
        golden_bytes, settings=settings, source_sha256=golden_sha,
        run_id="run-golden", chunk_id="chk-golden",
    ).to_dict()
    expected = json.loads(golden_expected_path.read_text(encoding="utf-8"))
    assert actual == expected["reference"]


def test_golden_covers_empty_multi_match_and_unterminated_records(
    golden_bytes: bytes, golden_sha: str, settings: DetectorSettings,
) -> None:
    result = detect(
        golden_bytes, settings=settings, source_sha256=golden_sha,
        run_id="run-golden", chunk_id="chk-golden",
    )
    assert result.line_count == 24
    assert result.metrics["missing_timestamps"] == 1  # the empty record
    assert result.metrics["invalid_timestamps"] == 0

    by_line: dict[int, set[str]] = {}
    for match in result.matches:
        by_line.setdefault(match.line_no, set()).add(match.rule_id)
    assert by_line[5] == {"http-not-found", "auth-failed-password"}
    assert by_line[24] == {"app-error", "app-segfault"}  # final record, no LF
    assert 6 not in by_line  # empty record

    assert [a.window_id for a in result.alerts] == sorted(
        a.window_id for a in result.alerts
    )
    assert {(a.rule_id, a.count, round(a.score, 3)) for a in result.alerts} == {
        ("auth-failed-password", 3, 5.0),
        ("auth-failed-password", 12, 10.0),
    }


def test_repeated_runs_are_identical(
    golden_bytes: bytes, golden_sha: str, settings: DetectorSettings,
) -> None:
    kwargs = dict(settings=settings, source_sha256=golden_sha,
                  run_id="run-golden", chunk_id="chk-golden")
    first = detect(golden_bytes, **kwargs).to_dict()
    second = detect(golden_bytes, **kwargs).to_dict()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
