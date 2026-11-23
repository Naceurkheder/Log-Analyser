"""Artifact digest and v2 ABI checks happen before any native load."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sentinel.worker.artifact import ArtifactVerifier
from sentinel.worker.config import WorkerSettings
from sentinel.worker.errors import ArtifactError, GpuUnavailableError


def _lib(version: int = 2) -> Any:
    return SimpleNamespace(sentinel_abi_version=lambda: version)


def test_cpu_verifies_digest_without_loading_native(
    aws: Any, artifact_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("CPU worker must not call CDLL")

    monkeypatch.setattr("ctypes.CDLL", explode)
    settings = WorkerSettings.from_env("cpu")
    identity = ArtifactVerifier(settings).verify("cpu-ref-v1")
    assert identity.sha256 == settings.artifact_sha256
    assert identity.lib is None


def test_digest_mismatch_rejects_before_native_load(
    aws: Any, artifact_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_file.write_bytes(b"tampered-artifact")
    calls: list[Any] = []
    monkeypatch.setattr(
        "sentinel.worker.artifact.load_gpu_library",
        lambda path: calls.append(path),
    )
    settings = WorkerSettings.from_env("gpu")
    with pytest.raises(ArtifactError) as exc:
        ArtifactVerifier(settings).verify("gpu-v1")
    assert exc.value.code == "artifact_digest_mismatch"
    assert calls == []


def test_cpu_without_digest_can_use_oracle(aws: Any, artifact_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SENTINEL_WORKER_ARTIFACT_SHA256")
    settings = WorkerSettings.from_env("cpu")
    identity = ArtifactVerifier(settings).verify("cpu-ref-v1")
    assert identity.sha256
    assert identity.lib is None
    artifact_file.unlink()
    assert ArtifactVerifier(settings).verify("cpu-ref-v1").sha256 is None


def test_pinned_cpu_artifact_missing_is_rejected(
    aws: Any, artifact_file: Path
) -> None:
    artifact_file.unlink()
    settings = WorkerSettings.from_env("cpu")
    with pytest.raises(ArtifactError):
        ArtifactVerifier(settings).verify("cpu-ref-v1")


def test_gpu_loads_v2_abi_after_digest_check(
    aws: Any, artifact_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded: list[Any] = []
    fake = _lib(2)

    def loader(path: str) -> Any:
        loaded.append(path)
        return fake

    monkeypatch.setattr("sentinel.worker.artifact.load_gpu_library", loader)
    monkeypatch.setattr("sentinel.worker.artifact.assert_abi", lambda lib: "build-7")
    settings = WorkerSettings.from_env("gpu")
    identity = ArtifactVerifier(settings).verify("gpu-v1")
    assert identity.lib is fake
    assert identity.abi_version == 2
    assert identity.build_id == "build-7"
    assert loaded == [str(artifact_file)]


def test_gpu_abi_mismatch_is_explicit(
    aws: Any, artifact_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sentinel.worker.artifact.load_gpu_library", lambda path: _lib(3)
    )
    monkeypatch.setattr("sentinel.worker.artifact.assert_abi", lambda lib: "build-7")
    settings = WorkerSettings.from_env("gpu")
    with pytest.raises(GpuUnavailableError) as exc:
        ArtifactVerifier(settings).verify("gpu-v1")
    assert exc.value.code == "abi_mismatch"
    assert exc.value.details["expected"] == 2
    assert exc.value.details["actual"] == 3


def test_gpu_missing_or_broken_artifact_fails_explicitly(
    aws: Any, artifact_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SENTINEL_WORKER_ARTIFACT_PATH")
    settings = WorkerSettings.from_env("gpu")
    with pytest.raises(GpuUnavailableError):
        ArtifactVerifier(settings).verify("gpu-v1")

    monkeypatch.setenv("SENTINEL_WORKER_ARTIFACT_PATH", str(artifact_file))
    monkeypatch.setenv("SENTINEL_WORKER_ARTIFACT_SHA256", "0" * 64)
    settings = WorkerSettings.from_env("gpu")
    with pytest.raises(ArtifactError):
        ArtifactVerifier(settings).verify("gpu-v1")

    monkeypatch.setenv("SENTINEL_WORKER_ARTIFACT_SHA256", __import__(
        "sentinel.ids", fromlist=["sha256_hex"]
    ).sha256_hex(artifact_file.read_bytes()))
    settings = WorkerSettings.from_env("gpu")
    monkeypatch.setattr(
        "sentinel.worker.artifact.load_gpu_library",
        lambda path: (_ for _ in ()).throw(OSError("bad ELF")),
    )
    with pytest.raises(GpuUnavailableError) as exc:
        ArtifactVerifier(settings).verify("gpu-v1")
    assert exc.value.code == "gpu_unavailable"
