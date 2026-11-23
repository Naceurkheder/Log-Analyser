"""Sprint 3-C: deterministic ground truth and label/chunk alignment."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

from sentinel.ids import line_id_for
from sentinel.labels import labels_for_chunk, load_manifest, scenario_line_ids
from sentinel.producer.chunker import SourceSpec, chunk_stream

REPO = Path(__file__).resolve().parents[2]
GEN = REPO / "scripts" / "gen_dataset.py"


def _generate(tmp: Path, *, seed: int = 11, lines: int = 6000,
              burst_every: int = 60, name: str = "derived") -> tuple[Path, Path]:
    out = tmp / f"{name}.log"
    labels = tmp / f"{name}.labels.json"
    subprocess.run(
        [
            sys.executable, str(GEN), "--lines", str(lines),
            "--seed", str(seed), "--burst-every", str(burst_every),
            "--out", str(out), "--labels-out", str(labels),
        ],
        check=True,
        capture_output=True,
    )
    return out, labels


def test_same_seed_produces_identical_data_and_labels(tmp_path: Path) -> None:
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir(); b_dir.mkdir()
    data_a, labels_a = _generate(a_dir)
    data_b, labels_b = _generate(b_dir)

    assert data_a.read_bytes() == data_b.read_bytes()
    assert labels_a.read_bytes() == labels_b.read_bytes()
    manifest = load_manifest(labels_a)
    assert manifest["synthetic"] is True
    assert manifest["source"]["sha256"] == hashlib.sha256(data_a.read_bytes()).hexdigest()
    assert manifest["source"]["line_count"] == data_a.read_text().count("\n")


def test_scenarios_cover_required_patterns_with_valid_ranges(tmp_path: Path) -> None:
    data, labels = _generate(tmp_path)
    manifest = load_manifest(labels)
    kinds = {scenario["kind"] for scenario in manifest["scenarios"]}
    assert {
        "failed_login_burst",
        "http_404_burst",
        "event_rate_burst",
    } <= kinds

    line_count = manifest["source"]["line_count"]
    for scenario in manifest["scenarios"]:
        span = scenario["line_range"]
        assert 1 <= span["start"] <= span["end"] <= line_count
        assert span["end"] - span["start"] + 1 == scenario["line_count"]
        assert scenario["seed"] == manifest["seed"]
        assert scenario["window"]["id"] >= 0
        assert scenario["window"]["seconds"] == manifest["window_seconds"]
    assert len(data.read_bytes()) > 0


def test_labels_are_never_embedded_in_detector_input(tmp_path: Path) -> None:
    data, labels = _generate(tmp_path)
    payload = data.read_bytes()
    for forbidden in (b"scenario_id", b"ground-truth", b"synthetic",
                      b"expected_label"):
        assert forbidden not in payload
    assert labels.is_file() and labels.stat().st_size > 0


def test_labels_align_after_chunking(tmp_path: Path) -> None:
    data, labels = _generate(tmp_path)
    manifest = load_manifest(labels)
    source_sha = manifest["source"]["sha256"]
    raw = data.read_bytes()

    collected: list[bytes] = []

    def on_chunk(record, payload: bytes) -> None:
        collected.append(payload)

    result = chunk_stream(
        io.BytesIO(raw),
        run_id="run_groundtruth",
        source=SourceSpec(
            bucket="incoming", key=data.name,
            sha256=source_sha, size_bytes=len(raw),
        ),
        target_bytes=2048,
        max_line_bytes=65536,
        on_chunk=on_chunk,
    )
    assert result.source_sha256 == source_sha
    assert b"".join(collected) == raw  # boundaries never split a record

    for scenario in manifest["scenarios"]:
        span = scenario["line_range"]
        covering = [
            record for record in result.chunks
            if record.line_start <= span["end"] and record.line_end >= span["start"]
        ]
        assert covering, scenario["scenario_id"]
        assert covering[0].line_start <= span["start"]
        assert covering[-1].line_end >= span["end"]
        for first, second in zip(covering, covering[1:]):
            assert second.line_start == first.line_end + 1

        stitched: list[int] = []
        for record in covering:
            matches = labels_for_chunk(manifest, record)
            match = next(
                (m for m in matches if m["scenario_id"] == scenario["scenario_id"]),
                None,
            )
            assert match is not None
            stitched.extend(range(
                match["line_range"]["start"], match["line_range"]["end"] + 1
            ))
        assert stitched == list(range(span["start"], span["end"] + 1))

        first_id, last_id = scenario_line_ids(manifest, scenario)
        assert first_id == line_id_for(source_sha, span["start"])
        assert last_id == line_id_for(source_sha, span["end"])
