"""Shared fixtures for CPU reference correctness tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from sentinel.detector import DetectorSettings
from sentinel.detector.config import settings_from_file

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cpu"


@pytest.fixture(scope="module")
def rules_path() -> Path:
    return FIXTURES / "rules.json"


@pytest.fixture(scope="module")
def golden_path() -> Path:
    return FIXTURES / "golden.log"


@pytest.fixture(scope="module")
def golden_expected_path() -> Path:
    return FIXTURES / "golden.expected.json"


@pytest.fixture(scope="module")
def settings(rules_path: Path) -> DetectorSettings:
    return settings_from_file(rules_path)


@pytest.fixture(scope="module")
def golden_bytes(golden_path: Path) -> bytes:
    return golden_path.read_bytes()


@pytest.fixture(scope="module")
def golden_sha(golden_bytes: bytes) -> str:
    return hashlib.sha256(golden_bytes).hexdigest()
