"""Pure-CPU reference pipeline (docs/algorithm-semantics.md §§2–6).

No FastAPI, SQS or DynamoDB imports are allowed here. The function
:func:`detect` is the deterministic correctness oracle for the CUDA port.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..ids import alert_id_for
from .features import Event, Feature, Match, extract_ip, extract_service, extract_status
from .records import parse_records
from .semantics import (
    ALGORITHM_VERSION, MIN_HISTORY, UINT32_MAX, DetectorSettings,
)
from .timestamps import (
    classify_timestamp, window_id_for, window_start_iso,
)


@dataclass(frozen=True)
class Alert:
    alert_id: str
    algorithm_version: str
    run_id: str
    chunk_id: str
    line_id_first: str
    line_ids: tuple[str, ...]
    window_id: int
    window_seconds: int
    service_id: int
    ip: int
    status: int
    rule_id: str
    severity: str
    count: int
    mean: float
    std: float
    score: float
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "alert_id": self.alert_id,
            "algorithm_version": self.algorithm_version,
            "run_id": self.run_id,
            "chunk_id": self.chunk_id,
            "line_id_first": self.line_id_first,
            "line_ids": list(self.line_ids),
            "window_id": self.window_id,
            "window_seconds": self.window_seconds,
            "service_id": self.service_id,
            "ip": self.ip,
            "status": self.status,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "count": self.count,
            "mean": round(self.mean, 6),
            "std": round(self.std, 6),
            "score": round(self.score, 6),
            "created_at": self.created_at,
        }
        return dict(sorted(data.items()))


@dataclass
class DetectionResult:
    algorithm_version: str = ALGORITHM_VERSION
    matches: list[Match] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    features: list[Feature] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    metrics: dict[str, int] = field(default_factory=dict)

    @property
    def line_count(self) -> int:
        return self.metrics["line_count"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm_version": self.algorithm_version,
            "matches": [m.to_dict() for m in self.matches],
            "events": [e.to_dict() for e in self.events],
            "features": [f.to_dict() for f in self.features],
            "alerts": [a.to_dict() for a in self.alerts],
            "metrics": dict(sorted(self.metrics.items())),
        }


def detect(
    data: bytes,
    *,
    settings: DetectorSettings,
    source_sha256: str,
    run_id: str = "run",
    chunk_id: str = "chunk",
    line_number_start: int = 1,
) -> DetectionResult:
    """Run the CPU reference over one chunk of raw bytes."""
    result = DetectionResult()
    records = parse_records(
        data, source_sha256=source_sha256, encoding=settings.encoding,
        max_line_bytes=settings.max_line_bytes,
        line_number_start=line_number_start,
    )
    result.metrics["line_count"] = len(records)
    result.metrics["bytes"] = len(data)
    result.metrics["matches"] = 0
    result.metrics["missing_timestamps"] = 0
    result.metrics["invalid_timestamps"] = 0

    epochs: dict[int, int] = {}
    events: list[Event] = []
    for record in records:
        status, epoch = classify_timestamp(record.text, settings.default_year)
        if status == "missing":
            result.metrics["missing_timestamps"] += 1
        elif status == "invalid":
            result.metrics["invalid_timestamps"] += 1
        else:
            assert epoch is not None
            epochs[record.line_no] = epoch

        for rule in settings.rules:
            if rule.pattern_bytes not in record.raw:
                continue
            match = Match(
                line_no=record.line_no, line_id=record.line_id,
                rule_id=rule.rule_id, rule_index=rule.index,
                severity=rule.severity, pattern=rule.pattern,
            )
            result.matches.append(match)
            result.metrics["matches"] += 1
            if record.line_no in epochs:
                epoch = epochs[record.line_no]
                events.append(Event(
                    line_no=record.line_no, line_id=record.line_id,
                    rule_id=rule.rule_id, rule_index=rule.index,
                    severity=rule.severity, epoch=epoch,
                    window_id=window_id_for(epoch, settings.window_seconds),
                    service_id=extract_service(record.text),
                    ip=extract_ip(record.text), status=extract_status(record.text),
                ))

    # Sorting here is what makes out-of-order timestamps harmless.
    events.sort(key=lambda e: (e.epoch, e.line_no, e.rule_index))
    result.events = events

    feature_counts: dict[tuple[tuple[int, int, int], int, str], int] = defaultdict(int)
    feature_lines: dict[tuple[tuple[int, int, int], int, str], set[str]] = defaultdict(set)
    for event in events:
        identity = (event.key, event.window_id, event.rule_id)
        if feature_counts[identity] == UINT32_MAX:
            raise OverflowError("per-key window counter would overflow uint32")
        feature_counts[identity] += 1
        feature_lines[identity].add(event.line_id)
    result.features = [
        Feature(key=key, window_id=window, rule_id=rule, count=count,
                line_ids=tuple(sorted(feature_lines[(key, window, rule)])))
        for (key, window, rule), count in sorted(
            feature_counts.items(),
            key=lambda item: (item[0][1], item[0][0], item[0][2]),
        )
    ]

    series: dict[tuple[tuple[int, int, int], str], dict[int, int]] = defaultdict(dict)
    for feature in result.features:
        series[(feature.key, feature.rule_id)][feature.window_id] = feature.count
    severities = {rule.rule_id: rule.severity for rule in settings.rules}

    alerts: list[Alert] = []
    for (key, rule_id), windows in series.items():
        start_window = min(windows)
        for target_window in sorted(windows):
            history = [
                windows.get(target_window - back, 0)
                for back in range(1, settings.baseline_windows + 1)
                if target_window - back >= start_window
            ]
            if len(history) < MIN_HISTORY:
                continue
            count = windows[target_window]
            mean = sum(history) / len(history)
            variance = max(0.0, sum(x * x for x in history) / len(history) - mean * mean)
            std = variance ** 0.5
            score = 0.0 if std == 0.0 else (count - mean) / std
            if not std > 0.0 or score < settings.z_threshold:
                continue
            ip, service_id, status = key[1], key[0], key[2]
            base_id = alert_id_for(
                run_id, chunk_id, ip, target_window, service_id, rule_id
            )
            # The legacy ID space has no status component; keep statuses distinct.
            alert_id = base_id if status == 0 else f"{base_id}-s{status:03d}"
            feature_identity = (key, target_window, rule_id)
            alerts.append(Alert(
                alert_id=alert_id,
                algorithm_version=ALGORITHM_VERSION,
                run_id=run_id, chunk_id=chunk_id,
                line_id_first=min(feature_lines[feature_identity]),
                line_ids=tuple(sorted(feature_lines[feature_identity])),
                window_id=target_window, window_seconds=settings.window_seconds,
                service_id=service_id, ip=ip, status=status,
                rule_id=rule_id, severity=severities[rule_id],
                count=count, mean=mean, std=std, score=score,
                created_at=window_start_iso(target_window, settings.window_seconds),
            ))
    alerts.sort(key=lambda a: (
        a.window_id, a.service_id, a.ip, a.status, a.rule_id, a.alert_id
    ))
    result.alerts = alerts
    result.metrics["features"] = len(result.features)
    result.metrics["alerts"] = len(alerts)
    return result
