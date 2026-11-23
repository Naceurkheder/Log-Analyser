"""Sprint 8 report: joins, strata, missing items and CLI round-trips."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from sentinel.evaluation.report import evaluate_documents, evaluate_files, render_markdown

REPO = Path(__file__).resolve().parents[2]


def _ip_uint32(text: str) -> int:
    parts = [int(part) for part in text.split(".")]
    return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]


def _label(window: int, service: int, ip: str, status: int, rule: str,
           value: str, stratum: str = "natural") -> dict:
    return {
        "window_id": window, "service_id": service, "ip": ip,
        "status": status, "rule_id": rule, "label": value,
        "stratum": stratum,
    }


def _alert(window: int, service: int, ip: str, status: int, rule: str,
           alert_id: str) -> dict:
    return {
        "alert_id": alert_id, "window_id": window, "service_id": service,
        "ip": _ip_uint32(ip), "status": status, "rule_id": rule,
        "window_seconds": 60,
    }


def _documents(labels: list[dict], alerts: list[dict], **extra) -> tuple[dict, dict]:
    label_doc = {
        "schema_version": 1, "kind": "sentinel.evaluation-labels",
        "coverage": "closed", "scope": "all_detections", "labels": labels,
    }
    detection_doc = {
        "algorithm_version": "cpu-reference-v1", "alerts": alerts,
        "matches": [{"rule_id": "r1", "pattern": "Failed password"}],
    }
    label_doc.update(extra.get("label_extra", {}))
    detection_doc.update(extra.get("detection_extra", {}))
    return label_doc, detection_doc


def test_join_by_documented_unit() -> None:
    a = _label(10, 1, "203.0.113.1", 0, "r1", "anomalous", "synthetic")
    b = _label(11, 1, "203.0.113.2", 0, "r1", "anomalous", "synthetic")
    negative = _label(12, 3, "0.0.0.0", 0, "r2", "normal", "synthetic")
    labels, detections = _documents(
        [a, b, negative],
        [
            _alert(10, 1, "203.0.113.1", 0, "r1", "alert-a"),
            _alert(13, 1, "203.0.113.9", 0, "r1", "alert-extra"),
        ],
    )
    report = evaluate_documents(labels, detections)
    block = report["strata"]["synthetic"]

    assert block["counts"] == {
        "tp": 1, "fp": 1, "tn": 1, "fn": 1,
        "positives": 2, "negatives": 1,
        "predicted_units": 2, "evaluation_units": 4,
    }
    assert block["metrics"]["precision"] == 0.5
    assert block["metrics"]["recall"] == 0.5
    assert block["metrics"]["false_positive_rate"] == 0.5
    assert block["metrics"]["fp_per_1000_units"] == 250.0
    assert report["evaluation_unit"]["fields"] == [
        "window_id", "service_id", "ip", "status", "rule_id",
    ]


def test_natural_and_synthetic_strata_are_separate() -> None:
    natural = _label(20, 1, "203.0.113.10", 0, "r1", "anomalous", "natural")
    synthetic = _label(21, 2, "203.0.113.11", 404, "r3", "anomalous", "synthetic")
    labels, detections = _documents(
        [natural, synthetic],
        [
            _alert(20, 1, "203.0.113.10", 0, "r1", "alert-natural"),
            _alert(21, 2, "203.0.113.11", 404, "r3", "alert-synthetic"),
        ],
        label_extra={"scope": "labelled_units_only"},
    )
    report = evaluate_documents(labels, detections)

    for stratum in ("natural", "synthetic"):
        block = report["strata"][stratum]
        assert block["counts"]["tp"] == 1
        assert block["counts"]["fp"] == 0
        assert block["counts"]["evaluation_units"] == 1
    assert report["totals"]["counts"]["tp"] == 2
    markdown = render_markdown(report)
    assert "## Natural labels" in markdown
    assert "## Synthetic labels" in markdown


def test_missing_predictions_and_labels_are_reported() -> None:
    labels, detections = _documents(
        [_label(30, 1, "203.0.113.30", 0, "r1", "anomalous", "synthetic")],
        [_alert(31, 1, "203.0.113.31", 0, "r1", "alert-unlabelled")],
    )
    report = evaluate_documents(labels, detections)
    block = report["strata"]["synthetic"]

    assert block["counts"]["fn"] == 1
    assert block["counts"]["fp"] == 1
    # Closed coverage permits FPR: the unlabelled prediction is a negative unit.
    assert block["metrics"]["false_positive_rate"] == 1.0
    assert report["strata"]["synthetic"]["coverage"] == "closed"
    assert [item["window_id"] for item in block["missing"]["predictions_for_labels"]] == [30]
    assert [item["window_id"] for item in block["missing"]["labels_for_predictions"]] == [31]


def test_generator_manifest_adapter_resolves_patterns_and_windows() -> None:
    manifest = {
        "schema_version": 1, "kind": "sentinel.ground-truth", "synthetic": True,
        "window_seconds": 60, "source": {"sha256": "abc", "name": "sample.log"},
        "scenarios": [{
            "scenario_id": "scn-0001", "kind": "failed_login_burst",
            "line_range": {"start": 1, "end": 62}, "line_count": 62,
            "timestamp": {"start_offset_seconds": 59, "end_offset_seconds": 120},
            "window": {"id": 100, "seconds": 60},
            "rule": {"pattern": "Failed password", "hint": "failed_login_burst"},
            "key": {"service": "sshd", "ip": "203.0.113.50"},
            "expected": {"label": "anomalous", "evaluation_unit": "window"},
        }],
    }
    detections = {
        "algorithm_version": "cpu-reference-v1",
        "matches": [{"rule_id": "auth-failed-password", "pattern": "Failed password"}],
        "alerts": [
            _alert(100, 1, "203.0.113.50", 0, "auth-failed-password", "alert-100"),
            _alert(101, 1, "203.0.113.50", 0, "auth-failed-password", "alert-101"),
        ],
    }
    report = evaluate_documents(manifest, detections)
    block = report["strata"]["synthetic"]

    assert block["counts"]["tp"] == 2  # scenario spans both windows
    assert block["counts"]["fp"] == 0
    assert block["counts"]["fn"] == 0
    assert block["counts"]["tn"] is None  # no negative labels were enumerated
    assert report["inputs"]["labels"]["kind"] == "sentinel.ground-truth"
    assert report["inputs"]["labels"]["adaptation"].startswith("scenario key")


def test_empty_report_renders_na_and_is_deterministic() -> None:
    labels, detections = _documents([], [])
    first = evaluate_documents(labels, detections)
    second = evaluate_documents(labels, detections)

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert all(value is None for value in first["strata"]["natural"]["metrics"].values())
    assert all(value is None for value in first["strata"]["synthetic"]["metrics"].values())
    assert "N/A" in render_markdown(first)
    assert "no labels supplied" in first["strata"]["natural"]["note"]


def test_cli_round_trip_via_tmp_path(tmp_path: Path) -> None:
    labels, detections = _documents(
        [_label(40, 1, "203.0.113.40", 0, "r1", "anomalous", "synthetic")],
        [_alert(40, 1, "203.0.113.40", 0, "r1", "alert-40")],
    )
    labels_path = tmp_path / "labels.json"
    detections_path = tmp_path / "detections.json"
    json_report = tmp_path / "report.json"
    md_report = tmp_path / "report.md"
    labels_path.write_text(json.dumps(labels), encoding="utf-8")
    detections_path.write_text(json.dumps(detections), encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    subprocess.run(
        [sys.executable, "-m", "sentinel.evaluation.cli", "evaluate",
         "--labels", str(labels_path), "--detections", str(detections_path),
         "--out", str(json_report)],
        check=True, env=env, capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "sentinel.evaluation.cli", "evaluate",
         "--labels", str(labels_path), "--detections", str(detections_path),
         "--out", str(md_report), "--format", "markdown"],
        check=True, env=env, capture_output=True,
    )

    from_cli = json.loads(json_report.read_text(encoding="utf-8"))
    expected = evaluate_files(labels_path, detections_path)
    assert from_cli == expected
    assert "| natural | 0 |" in md_report.read_text(encoding="utf-8")
    assert "| synthetic | 1 | 0 | 0 | 0 |" in md_report.read_text(encoding="utf-8")


def test_benchmark_report_includes_scope_limits_and_not_run(tmp_path: Path) -> None:
    out = tmp_path / "benchmark-report.md"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    subprocess.run(
        [sys.executable, str(REPO / "scripts" / "benchmark_report.py"),
         "--evaluation-report", str(REPO / "docs" / "evaluation" / "sample-evaluation.json"),
         "--out", str(out)],
        check=True, env=env, capture_output=True, cwd=REPO,
    )
    text = out.read_text(encoding="utf-8")

    assert "2769.1" in text and "81.5" in text and "34.0x" in text
    assert "NOT a speedup result" in text
    assert "## Natural labels" in text and "## Synthetic labels" in text
    for phrase in ("5-repetition workload matrix", "PR-AUC",
                   "Full end-to-end benchmark", "Pageable-memory baseline"):
        assert phrase in text
