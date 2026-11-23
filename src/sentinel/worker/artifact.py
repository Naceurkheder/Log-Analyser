"""Artifact identity checks performed before any native library is loaded.

The digest is supplied out of band (``SENTINEL_WORKER_ARTIFACT_SHA256``) so a
corrupt or swapped file cannot reach ``ctypes.CDLL``. The CPU worker uses the
pure-Python oracle but still verifies a pinned digest when one is configured.
The GPU worker must load the v2 native ABI; there is no CPU fallback.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..detector.native import (
    NativeLibraryUnavailable,
    assert_abi,
    load_gpu_library,
)
from .config import WorkerSettings
from .errors import ArtifactError, GpuUnavailableError

log = logging.getLogger("sentinel.worker.artifact")

# include/sentinel/sentinel.h: SENTINEL_ABI_VERSION 2u.
EXPECTED_ABI_VERSION = 2


@dataclass(frozen=True)
class ArtifactIdentity:
    """Verified artifact metadata; ``lib`` is only populated for GPU work."""

    artifact_id: str
    path: str | None
    sha256: str | None
    build_id: str | None = None
    abi_version: int | None = None
    lib: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "path": self.path,
            "sha256": self.sha256,
            "build_id": self.build_id,
            "abi_version": self.abi_version,
        }


class ArtifactVerifier:
    """Verify a local artifact digest and, for GPU, its native ABI."""

    def __init__(self, settings: WorkerSettings) -> None:
        self.settings = settings
        self._lib: Any = None

    def verify(self, artifact_id: str) -> ArtifactIdentity:
        configured_digest = self.settings.artifact_sha256
        path = self.settings.artifact_path
        if configured_digest is not None and not path:
            if self.settings.capability == "gpu":
                raise GpuUnavailableError(
                    "GPU artifact digest is configured but no artifact path is set",
                    details={"artifact_id": artifact_id},
                )
            raise ArtifactError(
                "artifact digest is configured but no artifact path is set",
                details={"artifact_id": artifact_id},
            )

        actual_digest: str | None = None
        if path:
            target = Path(path)
            if not target.is_file():
                if self.settings.capability == "gpu":
                    raise GpuUnavailableError(
                        "GPU artifact is missing",
                        details={"artifact_id": artifact_id, "path": path},
                    )
                if configured_digest is not None:
                    raise ArtifactError(
                        "pinned artifact is missing",
                        details={"artifact_id": artifact_id, "path": path},
                    )
            else:
                actual_digest = hashlib.sha256(target.read_bytes()).hexdigest()
                if configured_digest is not None and actual_digest != configured_digest:
                    raise ArtifactError(
                        "artifact digest mismatch",
                        code="artifact_digest_mismatch",
                        details={
                            "artifact_id": artifact_id,
                            "expected": configured_digest,
                            "actual": actual_digest,
                        },
                    )

        if self.settings.capability == "cpu":
            # The pure-Python oracle needs no native library. Identity has
            # still been verified above when a digest is configured.
            log.info(
                "artifact_verified",
                extra={
                    "event_fields": {
                        "artifact_id": artifact_id,
                        "backend": "cpu",
                        "digest": configured_digest,
                        "native_loaded": False,
                    }
                },
            )
            return ArtifactIdentity(
                artifact_id=artifact_id,
                path=path,
                sha256=actual_digest,
            )

        if not path:
            raise GpuUnavailableError(
                "GPU execution requires a native artifact path",
                details={"artifact_id": artifact_id},
            )
        if self._lib is None:
            # CDLL is called only after the digest check above.
            try:
                self._lib = load_gpu_library(path)
                build_id = assert_abi(self._lib)
            except (NativeLibraryUnavailable, RuntimeError, OSError, AttributeError) as exc:
                raise GpuUnavailableError(
                    f"native GPU artifact could not be loaded: {exc}",
                    details={"artifact_id": artifact_id, "path": path},
                ) from exc
            version = int(self._lib.sentinel_abi_version())
            if version != self.settings.expected_abi_version:
                raise GpuUnavailableError(
                    "native ABI mismatch",
                    code="abi_mismatch",
                    details={
                        "expected": self.settings.expected_abi_version,
                        "actual": version,
                    },
                )
            self._abi_version = version
            self._build_id = build_id
        identity = ArtifactIdentity(
            artifact_id=artifact_id,
            path=path,
            sha256=actual_digest,
            build_id=self._build_id,
            abi_version=self._abi_version,
            lib=self._lib,
        )
        log.info(
            "artifact_verified",
            extra={
                "event_fields": {
                    "artifact_id": artifact_id,
                    "backend": "gpu",
                    "digest": actual_digest,
                    "abi_version": identity.abi_version,
                    "build_id": identity.build_id,
                    "native_loaded": True,
                }
            },
        )
        return identity

    # Initialised lazily alongside ``_lib``.
    _abi_version: int | None = None
    _build_id: str | None = None

    @property
    def expected_abi_version(self) -> int:
        return self.settings.expected_abi_version or EXPECTED_ABI_VERSION
