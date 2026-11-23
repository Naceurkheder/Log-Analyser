"""Baseline record format and reproducibility."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from sentinel.detector.baseline import BASELINE_SCHEMA_VERSION, CSV_FIELDS, run_baseline
from sentinel.detector.semantics import ALGORITHM_VERSION, DetectorSettings

REPO = Path(__file__).resolve().parents[2]


def _stable(row: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in row.items()
            if key not in {"best_ms", "mean_ms", "throughput_mb_s"}}


def test_baseline_json_csv_are_stable_and_separate_modes(
    golden_path: Path, golden_bytes: bytes, settings: DetectorSettings,
    tmp_path: Path,
) -> None:
    first_dir, second_dir = tmp_path / "a", tmp_path / "b"
    first = run_baseline(
        golden_path, settings, repeats=2, threads=2, output_dir=first_dir
    )
    second = run_baseline(
        golden_path, settings, repeats=2, threads=2, output_dir=second_dir
    )

    assert first["schema_version"] == BASELINE_SCHEMA_VERSION
    assert first["algorithm_version"] == ALGORITHM_VERSION == second["algorithm_version"]
    assert first["input"]["sha256"] == hashlib.sha256(golden_bytes).hexdigest()
    assert first["input"]["bytes"] == len(golden_bytes)
    assert first["detector"]["config_hash"] == settings.config_hash
    assert [row["mode"] for row in first["rows"]] == [
        "cpu-reference-single-thread", "cpu-worker-threaded"
    ]
    assert first["rows"][0]["threads"] == 1
    assert first["rows"][1]["threads"] == 2
    assert all(row["line_count"] > 0 for row in first["rows"])
    assert all(row["config_hash"] == settings.config_hash for row in first["rows"])
    assert [_stable(row) for row in first["rows"]] == [
        _stable(row) for row in second["rows"]
    ]

    document_text = (first_dir / "cpu-baseline.json").read_text(encoding="utf-8")
    assert "ground-truth" not in document_text
    assert "scenario_id" not in document_text
    assert json.loads(document_text) == first

    with (first_dir / "cpu-baseline.csv").open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == CSV_FIELDS
        rows = list(reader)
    assert len(rows) == 2
    assert rows[0]["mode"] == "cpu-reference-single-thread"
    assert int(rows[0]["input_bytes"]) == len(golden_bytes)


def test_cli_baseline_writes_same_format(
    golden_path: Path, rules_path: Path, tmp_path: Path,
) -> None:
    subprocess.run(
        [
            sys.executable, "-m", "sentinel.detector.cli", "baseline",
            "--input", str(golden_path), "--rules", str(rules_path),
            "--repeats", "1", "--out-dir", str(tmp_path),
        ],
        check=True, capture_output=True, cwd=REPO,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO / "src")},
    )
    document = json.loads((tmp_path / "cpu-baseline.json").read_text())
    assert document["schema_version"] == BASELINE_SCHEMA_VERSION
    assert len(document["rows"]) == 1
