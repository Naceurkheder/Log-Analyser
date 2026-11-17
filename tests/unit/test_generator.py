"""Tests for scripts/gen_dataset.py — determinism and injection invariants."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GEN = REPO / "scripts" / "gen_dataset.py"
SAMPLE = REPO / "tests" / "fixtures" / "sample.log"


def _run(tmp_path: Path, *args: str, name: str = "out") -> Path:
    out = tmp_path / f"{name}.log"
    subprocess.run(
        [sys.executable, str(GEN), *args, "--out", str(out)],
        check=True, capture_output=True,
    )
    return out


def test_deterministic_same_seed(tmp_path: Path) -> None:
    a = _run(tmp_path, "--lines", "2000", "--seed", "7")
    b = tmp_path / "b.log"
    subprocess.run(
        [sys.executable, str(GEN), "--lines", "2000", "--seed", "7", "--out", str(b)],
        check=True, capture_output=True,
    )
    assert hashlib.sha256(a.read_bytes()).digest() == \
        hashlib.sha256(b.read_bytes()).digest()


def test_different_seed_changes_output(tmp_path: Path) -> None:
    a = _run(tmp_path, "--lines", "2000", "--seed", "7", name="a")
    b = _run(tmp_path, "--lines", "2000", "--seed", "8", name="b")
    assert a.read_bytes() != b.read_bytes()


def test_exact_line_count(tmp_path: Path) -> None:
    out = _run(tmp_path, "--lines", "1234", "--seed", "3")
    assert out.read_text().count("\n") == 1234


def test_size_flag_produces_requested_order(tmp_path: Path) -> None:
    out = _run(tmp_path, "--size-mb", "1", "--seed", "3")
    size = out.stat().st_size
    # budget is line-granular; overshoot is bounded by one injected burst
    assert 950_000 <= size < 1_050_000


def test_fixture_covers_all_pattern_classes() -> None:
    """The committed fixture must exercise every K1 pattern."""
    text = SAMPLE.read_text()
    for pattern in (
        "Failed password", "Invalid user", "ERROR", " 404 ", "SEGFAULT",
    ):
        assert pattern in text, f"fixture missing pattern: {pattern!r}"
