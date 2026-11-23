"""Canonical publish-side digest and ABI verification.

Workers keep their own minimal check, but the contract is the same: hash the
exact bytes that will be loaded, require SHA-256 equality, and require native
ABI v2.  A sidecar manifest is accepted as ABI evidence when the shared object
cannot be dlopen'ed on the verifying host (for example, a CUDA build on a
CPU-only machine).
"""

from __future__ import annotations

import ctypes
import re
from dataclasses import dataclass
from pathlib import Path

from .manifest import DEFAULT_ABI_VERSION, ArtifactManifest, digest_file

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ELF_MAGIC = b"\x7fELF"


class ArtifactVerificationError(RuntimeError):
    """Raised when an artifact's digest or ABI identity does not match."""


@dataclass(frozen=True, slots=True)
class VerificationResult:
    artifact_path: Path
    manifest_path: Path | None
    sha256: str
    size_bytes: int
    abi_version: int
    abi_source: str
    native_abi_version: int | None = None


def _normalize_digest(value: str, field: str = "sha256") -> str:
    digest = value.strip().lower() if isinstance(value, str) else ""
    if not _DIGEST_RE.match(digest):
        raise ArtifactVerificationError(
            f"{field} must be 64 hexadecimal characters"
        )
    return digest


def _native_abi(path: Path) -> int | None:
    """Call ``sentinel_abi_version`` when the file is a loadable ELF library."""
    try:
        with path.open("rb") as handle:
            if handle.read(4) != _ELF_MAGIC:
                return None
        library = ctypes.CDLL(str(path.resolve()))
        entry = library.sentinel_abi_version
        entry.restype = ctypes.c_uint32
        return int(entry())
    except (OSError, AttributeError, TypeError, ValueError):
        # Missing CUDA runtime, stripped symbol, or a non-library fixture:
        # the manifest remains authoritative for ABI metadata.
        return None


def _resolve_paths(
    path: Path,
    expected_sha256: str,
    manifest_path: Path | None,
) -> tuple[Path, Path | None, ArtifactManifest | None]:
    if path.is_dir():
        candidates = sorted(path.glob("*.json"))
        chosen: tuple[Path, ArtifactManifest] | None = None
        for candidate in candidates:
            try:
                manifest = ArtifactManifest.from_file(candidate)
            except (OSError, TypeError, ValueError):
                continue
            if manifest.sha256 == expected_sha256:
                chosen = (candidate, manifest)
                break
        if chosen is None:
            raise ArtifactVerificationError(
                f"no manifest in {path} matches sha256 {expected_sha256}"
            )
        sidecar, manifest = chosen
        return path / manifest.artifact_name, sidecar, manifest

    if path.suffix.lower() == ".json":
        sidecar = path
        manifest = ArtifactManifest.from_file(sidecar)
        return sidecar.parent / manifest.artifact_name, sidecar, manifest

    sidecar = manifest_path or path.with_name(f"{path.name}.json")
    manifest = ArtifactManifest.from_file(sidecar) if sidecar.is_file() else None
    return path, sidecar if sidecar.is_file() else None, manifest


def verify_artifact(
    path: str | Path,
    expected_sha256: str,
    expected_abi: int = DEFAULT_ABI_VERSION,
    *,
    manifest_path: str | Path | None = None,
    check_native: bool = True,
    require_native: bool = False,
) -> VerificationResult:
    """Verify an artifact's bytes and ABI before a worker loads it.

    ``expected_abi`` defaults to the ABI version pinned by ADR-0009.  Raises
    :class:`ArtifactVerificationError` on any mismatch or missing ABI evidence;
    returns a structured result otherwise.
    """
    expected = _normalize_digest(expected_sha256)
    if isinstance(expected_abi, bool) or not isinstance(expected_abi, int):
        raise ArtifactVerificationError("expected_abi must be an integer")

    artifact, sidecar, manifest = _resolve_paths(
        Path(path), expected, Path(manifest_path) if manifest_path else None
    )
    if not artifact.is_file():
        raise ArtifactVerificationError(f"artifact does not exist: {artifact}")

    actual_sha, actual_size = digest_file(artifact)
    if actual_sha != expected:
        raise ArtifactVerificationError(
            f"sha256 mismatch for {artifact}: expected {expected}, got {actual_sha}"
        )

    if manifest is not None:
        if manifest.sha256 != expected:
            raise ArtifactVerificationError(
                f"manifest sha256 mismatch: {manifest.sha256} != {expected}"
            )
        if manifest.size_bytes != actual_size:
            raise ArtifactVerificationError(
                f"size mismatch for {artifact}: manifest says "
                f"{manifest.size_bytes}, file has {actual_size}"
            )
        if manifest.abi_version != expected_abi:
            raise ArtifactVerificationError(
                f"ABI mismatch for {artifact}: manifest declares "
                f"v{manifest.abi_version}, expected v{expected_abi}"
            )

    native_version = _native_abi(artifact) if check_native else None
    if native_version is not None:
        if native_version != expected_abi:
            raise ArtifactVerificationError(
                f"ABI mismatch for {artifact}: native library reports "
                f"v{native_version}, expected v{expected_abi}"
            )
        if manifest is not None and manifest.abi_version != native_version:
            raise ArtifactVerificationError(
                f"ABI metadata disagreement for {artifact}: manifest says "
                f"v{manifest.abi_version}, native library says v{native_version}"
            )

    if manifest is None and native_version is None:
        raise ArtifactVerificationError(
            f"ABI metadata unavailable for {artifact}: no manifest sidecar and "
            "native sentinel_abi_version() could not be checked"
        )
    if require_native and native_version is None:
        raise ArtifactVerificationError(
            f"native ABI check required but unavailable for {artifact}"
        )

    if manifest is not None and native_version is not None:
        source = "manifest+native"
    elif manifest is not None:
        source = "manifest"
    else:
        source = "native"

    return VerificationResult(
        artifact_path=artifact,
        manifest_path=sidecar,
        sha256=actual_sha,
        size_bytes=actual_size,
        abi_version=manifest.abi_version if manifest else int(native_version or 0),
        abi_source=source,
        native_abi_version=native_version,
    )
