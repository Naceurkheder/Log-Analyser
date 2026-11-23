"""Deterministic identifiers, hashes, cursors, and timestamps."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from ..ids import config_hash, fingerprint, run_id_for
from .errors import ApiError
from .schemas import IDEMPOTENCY_KEY_PATTERN, DetectorConfig, RunRequest


def utcnow() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_fingerprint(payload: RunRequest) -> str:
    """Hash the normalised request so defaulted and explicit fields agree."""
    return fingerprint(payload.model_dump(mode="json"))


def detector_config_hash(detector: DetectorConfig) -> str:
    return config_hash(detector.model_dump(mode="json"))


def derive_run_id(idempotency_key: str) -> str:
    """Stable run id: repeating a key can never create a second logical run."""
    return run_id_for(idempotency_key)


def validate_idempotency_key(raw: str | None) -> str:
    if raw is None or raw == "":
        raise ApiError(
            400,
            "idempotency_key_required",
            "the Idempotency-Key header is required",
        )
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(raw):
        raise ApiError(
            400,
            "invalid_idempotency_key",
            "Idempotency-Key must be 8-128 characters of [A-Za-z0-9._:-] "
            "and start with an alphanumeric character",
        )
    return raw


def encode_cursor(last_key: dict[str, Any]) -> str:
    raw = canonical_json(last_key).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str, required: Iterable[str]) -> dict[str, str]:
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except Exception as exc:  # noqa: BLE001 - any malformed cursor is a 400
        raise ApiError(400, "invalid_cursor", "cursor is not valid") from exc
    expected = set(required)
    if not isinstance(decoded, dict) or set(decoded) != expected:
        raise ApiError(400, "invalid_cursor", "cursor does not match this collection")
    if not all(isinstance(value, str) and value for value in decoded.values()):
        raise ApiError(400, "invalid_cursor", "cursor is not valid")
    return decoded
