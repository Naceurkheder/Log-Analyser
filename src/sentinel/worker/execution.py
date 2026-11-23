"""Detector execution for a verified job input.

CPU uses the pure-Python oracle. GPU calls the v2 native ABI for K1 flags and
the native windowed K2 scorer for alerts; a missing or failed GPU library is
reported as ``gpu_unavailable`` and is never silently replaced by CPU work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..detector import window_native
from ..detector.native import NativeLibraryUnavailable, detect_native
from ..detector.pipeline import Alert, DetectionResult, detect
from ..detector.records import parse_records
from ..detector.semantics import ALGORITHM_VERSION, DetectorSettings
from ..detector.timestamps import (
    classify_timestamp,
    window_id_for,
    window_start_iso,
)
from ..ids import alert_id_for, canonical_json
from .artifact import ArtifactIdentity
from .errors import DetectorConfigError, GpuUnavailableError, PermanentError
from .message import JobMessage

log = logging.getLogger("sentinel.worker.execution")


@dataclass
class ExecutionResult:
    """Backend-independent output of one detection attempt."""

    alerts: list[dict[str, Any]]
    metrics: dict[str, Any] = field(default_factory=dict)
    backend: str = "cpu"
    artifact: dict[str, Any] = field(default_factory=dict)

    def document(
        self,
        *,
        message: JobMessage,
        job: dict[str, Any],
        attempt_id: str,
        input_info: dict[str, Any],
    ) -> bytes:
        """Canonical, attempt-specific JSON for the results bucket."""
        payload = {
            "schema_version": 1,
            "result_type": "job-attempt",
            "run_id": message.run_id,
            "job_id": message.job_id,
            "chunk_id": message.chunk_id,
            "attempt_id": attempt_id,
            "backend": self.backend,
            "artifact": self.artifact,
            "detector": {
                "config_hash": message.detector["config_hash"],
                "algorithm_version": ALGORITHM_VERSION,
            },
            "source_sha256": job.get("source_sha256"),
            "input": input_info,
            "alerts": self.alerts,
            "metrics": dict(sorted(self.metrics.items())),
        }
        return canonical_json(payload) + b"\n"


def detector_settings(
    message: JobMessage, run: dict[str, Any] | None
) -> DetectorSettings:
    if run is None or not isinstance(run.get("detector"), dict):
        raise DetectorConfigError(
            "run record has no detector configuration",
            details={"run_id": message.run_id},
        )
    settings = DetectorSettings.from_dict(run["detector"])
    if settings.config_hash != message.detector["config_hash"]:
        raise DetectorConfigError(
            "detector configuration hash does not match the run record",
            details={
                "expected": message.detector["config_hash"],
                "actual": settings.config_hash,
            },
        )
    return settings


def execute_job(
    message: JobMessage,
    job: dict[str, Any],
    run: dict[str, Any] | None,
    data: bytes,
    artifact: ArtifactIdentity,
    *,
    input_info: dict[str, Any] | None = None,
) -> ExecutionResult:
    """Execute one verified chunk with the selected backend capability."""
    settings = detector_settings(message, run)
    if message.backend == "cpu":
        result = detect(
            data,
            settings=settings,
            source_sha256=str(job.get("source_sha256") or ""),
            run_id=message.run_id,
            chunk_id=message.chunk_id,
            line_number_start=int(message.chunk["line_number_start"]),
        )
        return ExecutionResult(
            alerts=[alert.to_dict() for alert in result.alerts],
            metrics=result.metrics,
            backend="cpu",
            artifact=artifact.as_dict(),
        )
    if message.backend != "gpu":
        raise PermanentError(
            f"unsupported execution backend: {message.backend}",
            code="unsupported_backend",
        )
    gpu = _execute_gpu(message, job, data, settings, artifact)
    return ExecutionResult(
        alerts=[alert.to_dict() for alert in gpu],
        metrics={"gpu_native": True, "alerts": len(gpu)},
        backend="gpu",
        artifact=artifact.as_dict(),
    )


def _execute_gpu(
    message: JobMessage,
    job: dict[str, Any],
    data: bytes,
    settings: DetectorSettings,
    artifact: ArtifactIdentity,
) -> list[Alert]:
    if artifact.lib is None:
        raise GpuUnavailableError(
            "no verified native GPU library is loaded",
            details={"artifact_id": message.execution["artifact_id"]},
        )
    patterns = [rule.pattern for rule in settings.rules]
    try:
        native = detect_native(
            data,
            patterns,
            settings.z_threshold,
            lib=artifact.lib,
            backend="gpu",
        )
    except (NativeLibraryUnavailable, RuntimeError, ValueError, OSError) as exc:
        raise GpuUnavailableError(
            f"native GPU detection failed: {exc}",
            details={"artifact_id": message.execution["artifact_id"]},
        ) from exc

    line_start = int(message.chunk["line_number_start"])
    records = parse_records(
        data,
        source_sha256=str(job.get("source_sha256") or ""),
        encoding=settings.encoding,
        max_line_bytes=settings.max_line_bytes,
        line_number_start=line_start,
    )
    if len(native.line_flags) != len(records):
        raise GpuUnavailableError(
            "native K1 result has an unexpected line count",
            code="native_result_mismatch",
            details={"native": len(native.line_flags), "expected": len(records)},
        )

    # Native K1 flags are authoritative. Python is only used to derive the
    # documented timestamp/service/IP keys from already-tokenized records;
    # K1 and K2 scoring both come from the native GPU library.
    rows: list[window_native.FeatureRow] = []
    line_ids: dict[tuple[int, int, int, int, int], str] = {}
    for record in records:
        status, epoch = classify_timestamp(record.text, settings.default_year)
        if status != "ok" or epoch is None:
            continue
        flags = native.line_flags[record.line_no - line_start]
        for rule in settings.rules:
            if not flags & (1 << rule.index):
                continue
            from ..detector.features import (
                extract_ip,
                extract_service,
                extract_status,
            )

            key = (
                extract_service(record.text),
                extract_ip(record.text),
                extract_status(record.text),
                rule.index,
            )
            window = window_id_for(epoch, settings.window_seconds)
            rows.append((*key, window, 1))
            identity = (*key[:3], window, rule.index)
            previous = line_ids.get(identity)
            if previous is None or record.line_id < previous:
                line_ids[identity] = record.line_id

    try:
        scored = window_native.detect_window(
            rows,
            baseline_windows=settings.baseline_windows,
            z_threshold=settings.z_threshold,
            max_rule_index=max(len(settings.rules) - 1, 0),
            lib=artifact.lib,
        )
    except (NativeLibraryUnavailable, RuntimeError, ValueError, OSError) as exc:
        raise GpuUnavailableError(
            f"native GPU window scorer failed: {exc}",
            details={"artifact_id": message.execution["artifact_id"]},
        ) from exc

    severities = {rule.rule_id: rule.severity for rule in settings.rules}
    alerts: list[Alert] = []
    for item in scored.alerts:
        rule = settings.rules[item.rule]
        identity = (item.service, item.ip, item.status, item.window, item.rule)
        base_id = alert_id_for(
            message.run_id, message.chunk_id, item.ip, item.window,
            item.service, rule.rule_id,
        )
        alert_id = base_id if item.status == 0 else f"{base_id}-s{item.status:03d}"
        alerts.append(
            Alert(
                alert_id=alert_id,
                algorithm_version=ALGORITHM_VERSION,
                run_id=message.run_id,
                chunk_id=message.chunk_id,
                line_id_first=line_ids.get(identity, ""),
                line_ids=(line_ids.get(identity, ""),),
                window_id=int(item.window),
                window_seconds=settings.window_seconds,
                service_id=item.service,
                ip=item.ip,
                status=item.status,
                rule_id=rule.rule_id,
                severity=severities[rule.rule_id],
                count=int(item.count),
                mean=float(item.mean),
                std=float(item.std),
                score=float(item.score),
                created_at=window_start_iso(int(item.window), settings.window_seconds),
            )
        )
    alerts.sort(
        key=lambda a: (
            a.window_id, a.service_id, a.ip, a.status, a.rule_id, a.alert_id
        )
    )
    log.info(
        "gpu_native_executed",
        extra={
            "event_fields": {
                "run_id": message.run_id,
                "job_id": message.job_id,
                "lines": len(records),
                "features": len(rows),
                "alerts": len(alerts),
                "build_id": artifact.build_id,
            }
        },
    )
    return alerts
