"""Immutable, content-addressed native build artifacts (S7).

The publish side owns manifest creation and the canonical digest/ABI verifier.
Workers may implement a smaller check, but they must enforce the same contract:
verify the SHA-256 of the bytes that will be loaded and the native ABI version
before calling any other native symbol.
"""

from __future__ import annotations

from .manifest import (
    ARTIFACT_PREFIX,
    DEFAULT_ABI_VERSION,
    ArtifactManifest,
    artifact_object_keys,
    digest_file,
    sha256_file,
)
from .verify import ArtifactVerificationError, VerificationResult, verify_artifact

__all__ = [
    "ARTIFACT_PREFIX",
    "DEFAULT_ABI_VERSION",
    "ArtifactManifest",
    "ArtifactVerificationError",
    "VerificationResult",
    "artifact_object_keys",
    "digest_file",
    "sha256_file",
    "verify_artifact",
]
