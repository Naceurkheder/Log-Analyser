"""Publish and verify immutable native artifacts.

Local layout (``--out-dir artifacts`` by default)::

    artifacts/<sha256[:12]>/libsentinel.so
    artifacts/<sha256[:12]>/libsentinel.so.json

The same relative keys are used in the ``artifacts`` S3 bucket when
``--bucket`` is supplied.  Publication is idempotent and never overwrites an
existing local file or object key: a repeated publish is a no-op after
verifying that the existing bytes have the expected digest.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from .manifest import (
    ARTIFACT_PREFIX,
    DEFAULT_ABI_VERSION,
    ArtifactManifest,
    digest_file,
    utc_timestamp,
)
from .verify import ArtifactVerificationError, verify_artifact

log = logging.getLogger("sentinel.artifacts.publish")

_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")
_CUDA_RELEASE_RE = re.compile(r"release\s+(\d+\.\d+)", re.IGNORECASE)
_KNOWN_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


def _run(*command: str, timeout: float = 5.0) -> str | None:
    """Best-effort read-only tool probe; never raises for missing tools."""
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return (completed.stdout or "").strip() or None


def infer_git_commit() -> str:
    return os.environ.get("SENTINEL_GIT_COMMIT") or _run(
        "git", "rev-parse", "HEAD"
    ) or "unknown"


def infer_build_id() -> str:
    def _short() -> str | None:
        value = _run("git", "rev-parse", "--short", "HEAD")
        return value.splitlines()[0].strip() if value else None

    return os.environ.get("SENTINEL_BUILD_ID") or _short() or "dev"


def infer_compiler_version() -> str:
    override = os.environ.get("SENTINEL_COMPILER_VERSION")
    if override:
        return override
    compiler = next(
        (
            candidate
            for candidate in ("g++-13", "g++-14", "g++", "c++", "cc")
            if shutil.which(candidate)
        ),
        None,
    )
    if compiler is None:
        return "unknown"
    banner = _run(compiler, "--version") or ""
    first_line = banner.splitlines()[0] if banner else ""
    match = _VERSION_RE.search(first_line)
    return f"{compiler} {match.group(0)}" if match else first_line or "unknown"


def infer_cuda_version() -> str:
    override = os.environ.get("SENTINEL_CUDA_VERSION")
    if override:
        return override
    nvcc = shutil.which("nvcc")
    if nvcc is None:
        return "none"
    banner = _run(nvcc, "--version") or ""
    match = _CUDA_RELEASE_RE.search(banner)
    return match.group(1) if match else "unknown"


def infer_target_arch() -> str:
    return os.environ.get("SENTINEL_TARGET_ARCH") or platform.machine() or "unknown"


def _s3_client(endpoint_url: str | None, region: str) -> Any:
    import boto3  # imported lazily so local publishing stays dependency-light

    return boto3.client("s3", endpoint_url=endpoint_url or None, region_name=region)


def _upload_once(
    client: Any, bucket: str, key: str, path: Path, content_type: str
) -> bool:
    """Upload a key only when it does not exist. Return True when uploaded."""
    from botocore.exceptions import ClientError

    try:
        client.head_object(Bucket=bucket, Key=key)
        return False
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in _KNOWN_NOT_FOUND_CODES:
            raise

    with path.open("rb") as handle:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=handle,
            ContentType=content_type,
        )
    return True


def publish_artifact(
    library: str | Path,
    out_dir: str | Path = ARTIFACT_PREFIX,
    *,
    artifact_name: str | None = None,
    build_id: str | None = None,
    git_commit: str | None = None,
    abi_version: int = DEFAULT_ABI_VERSION,
    compiler_version: str | None = None,
    cuda_version: str | None = None,
    target_arch: str | None = None,
    build_flags: Sequence[str] = (),
    test_results: Sequence[str] = (),
    created_at: str | None = None,
    infer: bool = True,
    bucket: str | None = None,
    endpoint_url: str | None = None,
    region: str = "us-east-1",
    prefix: str = ARTIFACT_PREFIX,
    s3_client: Any | None = None,
) -> dict[str, Any]:
    """Hash, copy, describe and optionally upload a native library once."""
    source = Path(library)
    if not source.is_file():
        raise FileNotFoundError(f"library does not exist: {source}")

    sha256, size_bytes = digest_file(source)
    name = artifact_name or source.name
    manifest = ArtifactManifest(
        build_id=build_id if build_id is not None else (infer_build_id() if infer else "manual"),
        git_commit=git_commit if git_commit is not None else (infer_git_commit() if infer else "unknown"),
        abi_version=abi_version,
        compiler_version=compiler_version
        if compiler_version is not None
        else (infer_compiler_version() if infer else "unknown"),
        cuda_version=cuda_version
        if cuda_version is not None
        else (infer_cuda_version() if infer else "none"),
        target_arch=target_arch
        if target_arch is not None
        else (infer_target_arch() if infer else "unknown"),
        build_flags=tuple(build_flags),
        test_results=tuple(test_results),
        sha256=sha256,
        size_bytes=size_bytes,
        created_at=created_at or utc_timestamp(),
        artifact_name=name,
    )

    artifact_path, manifest_path = manifest.paths(out_dir)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    existing: list[str] = []

    if artifact_path.exists():
        on_disk_sha, _ = digest_file(artifact_path)
        if on_disk_sha != sha256:
            raise ArtifactVerificationError(
                f"content-addressed destination already holds different bytes: "
                f"{artifact_path}"
            )
        existing.append(str(artifact_path))
    else:
        try:
            with source.open("rb") as src, artifact_path.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
        except FileExistsError:  # concurrent publisher won the race
            on_disk_sha, _ = digest_file(artifact_path)
            if on_disk_sha != sha256:
                raise ArtifactVerificationError(
                    f"content-addressed destination was replaced: {artifact_path}"
                ) from None
            existing.append(str(artifact_path))
        else:
            written.append(str(artifact_path))

    manifest_bytes = manifest.to_bytes()
    if manifest_path.exists():
        old = manifest_path.read_bytes()
        if old != manifest_bytes:
            try:
                previous = ArtifactManifest.from_bytes(old)
            except (TypeError, ValueError) as exc:
                raise ArtifactVerificationError(
                    f"existing manifest is not readable: {manifest_path}"
                ) from exc
            if (
                previous.sha256 != manifest.sha256
                or previous.abi_version != manifest.abi_version
                or previous.artifact_name != manifest.artifact_name
            ):
                raise ArtifactVerificationError(
                    f"existing manifest conflicts with artifact identity: {manifest_path}"
                )
            manifest = previous  # created_at may differ; never overwrite the key
        existing.append(str(manifest_path))
    else:
        try:
            with manifest_path.open("xb") as handle:
                handle.write(manifest_bytes)
        except FileExistsError:
            existing.append(str(manifest_path))
        else:
            written.append(str(manifest_path))

    verification = verify_artifact(
        artifact_path,
        manifest.sha256,
        expected_abi=manifest.abi_version,
        manifest_path=manifest_path,
        check_native=False,
    )

    s3_result: dict[str, Any] | None = None
    if bucket:
        client = s3_client or _s3_client(endpoint_url, region)
        artifact_key, manifest_key = manifest.object_keys(prefix)
        uploaded: list[str] = []
        skipped: list[str] = []
        for key, path, content_type in (
            (artifact_key, artifact_path, "application/octet-stream"),
            (manifest_key, manifest_path, "application/json"),
        ):
            if _upload_once(client, bucket, key, path, content_type):
                uploaded.append(key)
            else:
                skipped.append(key)
        s3_result = {
            "bucket": bucket,
            "artifact_key": artifact_key,
            "manifest_key": manifest_key,
            "uploaded": uploaded,
            "skipped": skipped,
        }

    result: dict[str, Any] = {
        "ok": True,
        "artifact_name": manifest.artifact_name,
        "artifact_id": manifest.artifact_id,
        "sha256": manifest.sha256,
        "abi_version": manifest.abi_version,
        "manifest": manifest.to_dict(),
        "artifact_path": str(artifact_path),
        "manifest_path": str(manifest_path),
        "written": written,
        "existing": existing,
        "verification": {
            "ok": True,
            "sha256": verification.sha256,
            "abi_version": verification.abi_version,
            "abi_source": verification.abi_source,
        },
        "s3": s3_result,
    }
    log.info(
        "artifact_published",
        extra={
            "event_fields": {
                "artifact_name": manifest.artifact_name,
                "sha256": manifest.sha256,
                "abi_version": manifest.abi_version,
                "written_count": len(written),
            }
        },
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sentinel.artifacts.publish",
        description="Publish an immutable, content-addressed native artifact.",
    )
    parser.add_argument("--library", type=Path, help="native .so file to publish")
    parser.add_argument(
        "--out-dir", type=Path, default=Path(ARTIFACT_PREFIX), help="local output root"
    )
    parser.add_argument("--artifact-name", help="object name (defaults to file name)")
    parser.add_argument("--build-id")
    parser.add_argument("--git-commit")
    parser.add_argument("--abi-version", type=int, default=DEFAULT_ABI_VERSION)
    parser.add_argument("--compiler-version")
    parser.add_argument("--cuda-version")
    parser.add_argument("--target-arch")
    parser.add_argument("--build-flags", nargs="+", default=[], metavar="FLAG")
    parser.add_argument("--build-flag", action="append", default=[], metavar="FLAG")
    parser.add_argument("--test-results", nargs="+", default=[], metavar="RESULT")
    parser.add_argument("--test-result", action="append", default=[], metavar="RESULT")
    parser.add_argument("--created-at", help="ISO-8601 timestamp (default: now)")
    parser.add_argument(
        "--no-infer",
        dest="infer",
        action="store_false",
        help="do not probe git/compiler/CUDA/arch when values are omitted",
    )
    parser.add_argument("--bucket", help="S3 bucket for the optional upload")
    parser.add_argument("--endpoint-url", help="S3 endpoint (e.g. moto)")
    parser.add_argument(
        "--region", default=os.environ.get("AWS_REGION", "us-east-1")
    )
    parser.add_argument(
        "--prefix", default=ARTIFACT_PREFIX, help="S3 key prefix (default: artifacts)"
    )
    parser.add_argument(
        "--verify",
        type=Path,
        metavar="PATH",
        help="verify a published artifact/directory/manifest instead of publishing",
    )
    parser.add_argument("--expected-sha256", help="digest required by --verify")
    parser.add_argument(
        "--expected-abi", type=int, default=DEFAULT_ABI_VERSION, help="for --verify"
    )
    parser.add_argument(
        "--require-native",
        action="store_true",
        help="--verify must call sentinel_abi_version() via ctypes",
    )
    parser.add_argument(
        "--no-native-check",
        action="store_true",
        help="--verify trusts the manifest sidecar for ABI metadata",
    )
    parser.set_defaults(infer=True)
    return parser


def _expected_sha_from_manifest(path: Path) -> str | None:
    """Locate a manifest for ``--verify`` and return its expected digest."""
    if path.is_dir():
        candidates = sorted(path.glob("*.json"))
        if len(candidates) != 1:
            return None
        return ArtifactManifest.from_file(candidates[0]).sha256
    if path.suffix.lower() == ".json":
        return ArtifactManifest.from_file(path).sha256
    sidecar = path.with_name(f"{path.name}.json")
    return ArtifactManifest.from_file(sidecar).sha256 if sidecar.is_file() else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    def _emit(payload: dict[str, Any]) -> None:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))

    if args.verify is not None:
        expected_sha = args.expected_sha256
        if not expected_sha:
            # A manifest sidecar is itself the expected-digest source; callers
            # may omit --expected-sha256 when verifying a published layout.
            try:
                expected_sha = _expected_sha_from_manifest(args.verify)
            except (OSError, TypeError, ValueError):
                expected_sha = None
            if not expected_sha:
                parser.error(
                    "--verify requires --expected-sha256 when no manifest "
                    "sidecar is available"
                )
        try:
            result = verify_artifact(
                args.verify,
                expected_sha,
                expected_abi=args.expected_abi,
                check_native=not args.no_native_check,
                require_native=args.require_native,
            )
        except (ArtifactVerificationError, OSError, TypeError, ValueError) as exc:
            _emit({"ok": False, "error": str(exc)})
            return 1
        _emit(
            {
                "ok": True,
                "artifact_path": str(result.artifact_path),
                "manifest_path": str(result.manifest_path)
                if result.manifest_path
                else None,
                "sha256": result.sha256,
                "size_bytes": result.size_bytes,
                "abi_version": result.abi_version,
                "abi_source": result.abi_source,
                "native_abi_version": result.native_abi_version,
            }
        )
        return 0

    if args.library is None:
        parser.error("--library is required unless --verify is used")

    try:
        result = publish_artifact(
            args.library,
            args.out_dir,
            artifact_name=args.artifact_name,
            build_id=args.build_id,
            git_commit=args.git_commit,
            abi_version=args.abi_version,
            compiler_version=args.compiler_version,
            cuda_version=args.cuda_version,
            target_arch=args.target_arch,
            build_flags=[*args.build_flags, *args.build_flag],
            test_results=[*args.test_results, *args.test_result],
            created_at=args.created_at,
            infer=args.infer,
            bucket=args.bucket,
            endpoint_url=args.endpoint_url,
            region=args.region,
            prefix=args.prefix,
        )
    except (ArtifactVerificationError, FileNotFoundError, OSError, TypeError, ValueError) as exc:
        _emit({"ok": False, "error": str(exc)})
        return 1
    _emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
