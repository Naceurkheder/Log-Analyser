"""Artifact digests, immutable key naming, verification and CLI output."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from sentinel.artifacts.manifest import ArtifactManifest, digest_file
from sentinel.artifacts.publish import main, publish_artifact
from sentinel.artifacts.verify import ArtifactVerificationError, verify_artifact

CREATED_AT = "2026-11-23T12:00:00Z"


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    path = tmp_path / "libsentinel.so"
    path.write_bytes(b"sentinel native artifact fixture\n" + b"x" * 1024)
    return path


def _manifest(sha256: str, size_bytes: int, abi_version: int = 2) -> ArtifactManifest:
    return ArtifactManifest(
        build_id="s7-test",
        git_commit="0123456789abcdef",
        abi_version=abi_version,
        compiler_version="g++ 13.5",
        cuda_version="12.4",
        target_arch="x86_64",
        build_flags=("-O3", "-fPIC"),
        test_results=("pytest=passed", "cuda-parity=passed"),
        sha256=sha256,
        size_bytes=size_bytes,
        created_at=CREATED_AT,
        artifact_name="libsentinel.so",
    )


def _publish_args(library: Path, out_dir: Path) -> list[str]:
    return [
        "--library",
        str(library),
        "--out-dir",
        str(out_dir),
        "--no-infer",
        "--build-id",
        "s7-test",
        "--git-commit",
        "0123456789abcdef",
        "--compiler-version",
        "g++ 13.5",
        "--cuda-version",
        "12.4",
        "--target-arch",
        "x86_64",
        "--created-at",
        CREATED_AT,
        "--build-flag=-O3",
        "--test-result",
        "pytest=passed",
    ]


def test_sha256_and_immutable_content_addressed_keys(
    library: Path, tmp_path: Path
) -> None:
    digest, size = digest_file(library)
    assert digest == hashlib.sha256(library.read_bytes()).hexdigest()

    manifest = _manifest(digest, size)
    assert manifest.artifact_id == digest[:12]
    assert manifest.sha256 == digest
    assert manifest.build_flags == ("-O3", "-fPIC")
    assert ArtifactManifest.from_bytes(manifest.to_bytes()) == manifest

    artifact_path, manifest_path = manifest.paths(tmp_path / "artifacts")
    assert artifact_path == tmp_path / "artifacts" / digest[:12] / "libsentinel.so"
    assert manifest_path == artifact_path.with_name("libsentinel.so.json")
    assert manifest.object_keys() == (
        f"artifacts/{digest[:12]}/libsentinel.so",
        f"artifacts/{digest[:12]}/libsentinel.so.json",
    )

    with pytest.raises(FrozenInstanceError):
        manifest.sha256 = "0" * 64  # type: ignore[misc]
    assert isinstance(manifest.to_dict()["build_flags"], tuple)
    assert isinstance(json.loads(manifest.to_bytes())["build_flags"], list)


def test_verify_success_digest_mismatch_and_abi_mismatch(
    library: Path, tmp_path: Path
) -> None:
    digest, size = digest_file(library)
    manifest = _manifest(digest, size)
    artifact_path, manifest_path = manifest.paths(tmp_path / "artifacts")
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(library.read_bytes())
    manifest_path.write_bytes(manifest.to_bytes())

    result = verify_artifact(
        artifact_path,
        digest,
        expected_abi=2,
        manifest_path=manifest_path,
        check_native=False,
    )
    assert result.abi_source == "manifest"
    assert result.size_bytes == size

    with pytest.raises(ArtifactVerificationError, match="sha256 mismatch"):
        verify_artifact(
            artifact_path,
            "0" * 64,
            manifest_path=manifest_path,
            check_native=False,
        )

    corrupt = tmp_path / "corrupt.so"
    corrupt.write_bytes(b"different bytes")
    with pytest.raises(ArtifactVerificationError, match="sha256 mismatch"):
        verify_artifact(corrupt, digest, check_native=False)

    wrong_abi = _manifest(digest, size, abi_version=1)
    manifest_path.write_bytes(wrong_abi.to_bytes())
    with pytest.raises(ArtifactVerificationError, match="ABI mismatch"):
        verify_artifact(
            artifact_path, digest, expected_abi=2, check_native=False
        )


def test_cli_publish_output_structure_and_idempotency(
    library: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    digest, _ = digest_file(library)
    out_dir = tmp_path / "cli-artifacts"
    args = _publish_args(library, out_dir)

    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["ok"] is True
    assert first["sha256"] == digest
    assert first["abi_version"] == 2
    assert first["manifest"]["artifact_name"] == "libsentinel.so"
    assert set(first) >= {
        "ok",
        "artifact_id",
        "manifest",
        "artifact_path",
        "manifest_path",
        "written",
        "existing",
        "verification",
        "s3",
    }
    assert first["s3"] is None
    assert first["verification"]["abi_source"] == "manifest"
    artifact_path = Path(first["artifact_path"])
    manifest_path = Path(first["manifest_path"])
    assert artifact_path.is_file() and manifest_path.is_file()
    assert set(first["written"]) == {str(artifact_path), str(manifest_path)}

    assert main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["written"] == []
    assert set(second["existing"]) == {str(artifact_path), str(manifest_path)}


def test_cli_verify_success_and_failure(
    library: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    digest, _ = digest_file(library)
    result = publish_artifact(
        library,
        tmp_path / "verified-artifacts",
        infer=False,
        created_at=CREATED_AT,
    )
    manifest_path = Path(result["manifest_path"])

    # The sidecar supplies the expected digest when the flag is omitted.
    assert main(["--verify", str(manifest_path), "--no-native-check"]) == 0
    json.loads(capsys.readouterr().out)

    assert (
        main(
            [
                "--verify",
                str(manifest_path),
                "--expected-sha256",
                digest,
                "--no-native-check",
            ]
        )
        == 0
    )
    verified: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert verified["ok"] is True
    assert verified["sha256"] == digest
    assert verified["abi_version"] == 2

    assert (
        main(
            [
                "--verify",
                str(manifest_path),
                "--expected-sha256",
                "f" * 64,
                "--no-native-check",
            ]
        )
        == 1
    )
    failed: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert failed["ok"] is False
    assert "mismatch" in failed["error"]


def test_publish_uploads_to_s3_only_when_key_is_absent(
    library: Path, tmp_path: Path
) -> None:
    import boto3
    from moto import mock_aws

    digest, _ = digest_file(library)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="artifacts")

        kwargs: dict[str, Any] = {
            "bucket": "artifacts",
            "endpoint_url": None,
            "region": "us-east-1",
            "infer": False,
            "created_at": CREATED_AT,
        }
        result = publish_artifact(library, tmp_path / "s3-artifacts", **kwargs)
        keys = {
            item["Key"]
            for item in s3.list_objects_v2(Bucket="artifacts")["Contents"]
        }
        assert keys == {
            result["s3"]["artifact_key"],
            result["s3"]["manifest_key"],
        }
        manifest_key = result["s3"]["manifest_key"]
        uploaded = json.loads(
            s3.get_object(Bucket="artifacts", Key=manifest_key)["Body"].read()
        )
        assert uploaded["sha256"] == digest
        assert set(result["s3"]["uploaded"]) == keys

        repeated = publish_artifact(library, tmp_path / "s3-artifacts", **kwargs)
        assert repeated["s3"]["uploaded"] == []
        assert set(repeated["s3"]["skipped"]) == keys
