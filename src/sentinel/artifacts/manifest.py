"""Immutable identity records for native build artifacts.

An artifact is identified by the SHA-256 of its *bytes*, not by a build id or
a git tag.  The manifest pins the toolchain and test evidence that produced
those bytes so a worker can decide whether a library is safe to load.  Both
the manifest and the library are written once under ``<prefix>/<sha256[:12]>/``;
a different byte string with the same name never reuses that path.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..ids import canonical_json

ARTIFACT_PREFIX = "artifacts"
DEFAULT_ABI_VERSION = 2
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_CHUNK_SIZE = 1024 * 1024

_FIELD_NAMES = (
    "build_id",
    "git_commit",
    "abi_version",
    "compiler_version",
    "cuda_version",
    "target_arch",
    "build_flags",
    "test_results",
    "sha256",
    "size_bytes",
    "created_at",
    "artifact_name",
)


def utc_timestamp() -> str:
    """Stable ISO-8601 UTC timestamp for newly published manifests."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def digest_file(path: str | Path, *, chunk_size: int = _CHUNK_SIZE) -> tuple[str, int]:
    """Return ``(sha256_hex, size_bytes)`` for a file, streaming large binaries."""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def sha256_file(path: str | Path) -> str:
    """Return only the digest of a file (convenience wrapper)."""
    return digest_file(path)[0]


def _freeze_strings(
    value: Any,
    field_name: str,
    *,
    mapping_join: str | None = None,
) -> tuple[str, ...]:
    """Normalise list/mapping/str inputs into an immutable tuple of strings."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        items: list[str] = []
        for key in sorted(value, key=str):
            item = value[key]
            if isinstance(item, bool):
                item = "passed" if item else "failed"
            rendered = f"{key}{mapping_join}{item}" if mapping_join else f"{key}={item}"
            items.append(rendered)
        return tuple(items)
    if isinstance(value, Iterable):
        rendered_items = tuple(str(item) for item in value)
        if any(not item for item in rendered_items):
            raise ValueError(f"{field_name} must not contain empty values")
        return rendered_items
    raise TypeError(f"{field_name} must be a string, mapping, or iterable of strings")


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    """Frozen manifest; nested collections are tuples, never mutable lists."""

    build_id: str
    git_commit: str
    abi_version: int
    compiler_version: str
    cuda_version: str
    target_arch: str
    build_flags: tuple[str, ...]
    test_results: tuple[str, ...]
    sha256: str
    size_bytes: int
    created_at: str
    artifact_name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "build_flags", _freeze_strings(self.build_flags, "build_flags")
        )
        object.__setattr__(
            self,
            "test_results",
            _freeze_strings(self.test_results, "test_results", mapping_join="="),
        )

        for name in (
            "build_id",
            "git_commit",
            "compiler_version",
            "cuda_version",
            "target_arch",
            "created_at",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")

        if not isinstance(self.artifact_name, str) or not _NAME_RE.match(
            self.artifact_name
        ):
            raise ValueError("artifact_name must be a plain file name (no path parts)")

        if isinstance(self.abi_version, bool) or not isinstance(self.abi_version, int):
            raise ValueError("abi_version must be an integer")
        if self.abi_version < 1:
            raise ValueError("abi_version must be >= 1")

        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise ValueError("size_bytes must be an integer")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be >= 0")

        digest = self.sha256.strip().lower() if isinstance(self.sha256, str) else ""
        if not _DIGEST_RE.match(digest):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        object.__setattr__(self, "sha256", digest)

    @property
    def artifact_id(self) -> str:
        """Short content id; safe to embed in job ids and log fields."""
        return self.sha256[:12]

    @property
    def digest_prefix(self) -> str:
        return self.sha256[:12]

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in _FIELD_NAMES}

    def to_bytes(self) -> bytes:
        """Canonical JSON encoding (sorted keys, no insignificant whitespace)."""
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactManifest":
        provided = set(data)
        unknown = provided - set(_FIELD_NAMES)
        if unknown:
            raise ValueError(f"unknown manifest fields: {sorted(unknown)}")
        missing = set(_FIELD_NAMES) - provided
        if missing:
            raise ValueError(f"missing manifest fields: {sorted(missing)}")
        return cls(**dict(data))

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ArtifactManifest":
        value: Any
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("manifest is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("manifest must be a JSON object")
        return cls.from_dict(value)

    @classmethod
    def from_file(cls, path: str | Path) -> "ArtifactManifest":
        return cls.from_bytes(Path(path).read_bytes())

    def directory(self, out_dir: str | Path = ARTIFACT_PREFIX) -> Path:
        return Path(out_dir) / self.digest_prefix

    def paths(
        self, out_dir: str | Path = ARTIFACT_PREFIX
    ) -> tuple[Path, Path]:
        """Return ``(artifact_path, manifest_path)`` for a local publication."""
        directory = self.directory(out_dir)
        return (
            directory / self.artifact_name,
            directory / f"{self.artifact_name}.json",
        )

    def object_keys(self, prefix: str = ARTIFACT_PREFIX) -> tuple[str, str]:
        """Return ``(artifact_key, manifest_key)`` for object storage."""
        base = prefix.strip("/")
        root = f"{base}/{self.digest_prefix}" if base else self.digest_prefix
        return f"{root}/{self.artifact_name}", f"{root}/{self.artifact_name}.json"


def artifact_object_keys(
    sha256: str, artifact_name: str, prefix: str = ARTIFACT_PREFIX
) -> tuple[str, str]:
    """Compute content-addressed keys without building a full manifest."""
    base = prefix.strip("/")
    root = f"{base}/{sha256[:12]}" if base else sha256[:12]
    return f"{root}/{artifact_name}", f"{root}/{artifact_name}.json"
