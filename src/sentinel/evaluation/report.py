"""Join ground-truth labels to detection alerts and build an evaluation report.

Two label inputs are accepted:

* ``sentinel.evaluation-labels`` — an explicit unit-level label document; and
* ``sentinel.ground-truth`` — the manifest written by ``scripts/gen_dataset.py``.

The generator manifest records scenarios at ``(window, key)`` granularity with
a rule *pattern*.  This module adapts those scenarios to the documented unit
``(window_id, service_id, ip, status, rule_id)`` by resolving the pattern
against the ``matches`` array of the detector output and expanding a scenario
across every window it touches.  Anything that cannot be resolved is reported
under ``unresolved`` rather than silently guessed.

See ``docs/decisions/0012-evaluation.md`` for metric and coverage definitions.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..detector.semantics import SERVICE_TAGS
from .metrics import (
    ConfusionMatrix,
    EvaluationUnit,
    compute_metrics,
    count_confusion,
    format_metric,
    normalise_label,
)

SCHEMA_VERSION = 1
EVALUATION_UNIT_FIELDS = ("window_id", "service_id", "ip", "status", "rule_id")
STRATA = ("natural", "synthetic")
COVERAGES = frozenset({"closed", "positive-only"})
SCOPES = frozenset({"all_detections", "labelled_units_only"})

_SERVICE_IDS = {name: value for name, value in SERVICE_TAGS}
_POSITIVE_SOURCE_LABEL = "anomalous"


@dataclass(frozen=True)
class EvaluationLabel:
    unit: EvaluationUnit
    label: str
    stratum: str
    source: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalisedLabels:
    labels: list[EvaluationLabel]
    coverage: str
    scope: str
    kind: str
    window_seconds: int | None
    source_info: dict[str, Any]
    unresolved: list[dict[str, Any]]


@dataclass
class NormalisedDetections:
    predictions: set[EvaluationUnit]
    alert_ids: dict[EvaluationUnit, list[str]]
    window_seconds: int | None
    unresolved: list[dict[str, Any]]
    source_info: dict[str, Any]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _issue(scope: str, reason: str, **details: Any) -> dict[str, Any]:
    return {"scope": scope, "reason": reason, **details}


def _unit_dict(unit: EvaluationUnit) -> dict[str, Any]:
    return dict(zip(EVALUATION_UNIT_FIELDS, unit))


def _unit_key(unit: EvaluationUnit) -> str:
    return "|".join(str(part) for part in unit)


def _sorted_units(units: Iterable[EvaluationUnit]) -> list[dict[str, Any]]:
    return [_unit_dict(unit) for unit in sorted(set(units), key=_unit_key)]


def _stable_issues(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def _service_id(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError("service_id is missing or invalid")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        result = int(value.strip())
    elif isinstance(value, str):
        result = _SERVICE_IDS.get(value.strip().lower(), -1)
    else:
        raise ValueError(f"unsupported service value: {value!r}")
    if not 0 <= result <= 255:
        raise ValueError(f"unsupported service value: {value!r}")
    return result


def _ip_uint32(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError(f"unsupported ip value: {value!r}")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        if value.strip().isdigit():
            result = int(value.strip())
        else:
            result = int(ipaddress.IPv4Address(value.strip()))
    else:
        raise ValueError(f"unsupported ip value: {value!r}")
    if not 0 <= result <= 0xFFFFFFFF:
        raise ValueError(f"ip outside uint32 range: {value!r}")
    return result


def _build_unit(
    *,
    window_id: Any,
    service_id: Any,
    ip: Any,
    status: Any,
    rule_id: Any,
) -> EvaluationUnit:
    window = _as_int(window_id, "window_id")
    if window < 0:
        raise ValueError("window_id must be non-negative")
    service = _service_id(service_id)
    address = _ip_uint32(ip)
    code = _as_int(status if status is not None else 0, "status")
    if not 0 <= code <= 65535:
        raise ValueError("status outside uint16 range")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ValueError("rule_id must be a non-empty string")
    return (window, service, address, code, rule_id.strip())


def _read_json(path: str | Path) -> tuple[Any, str]:
    raw = Path(path).read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def load_json_document(path: str | Path) -> tuple[Any, str]:
    """Return ``(document, sha256_of_raw_file)`` for a JSON evidence file."""
    return _read_json(path)


# ---------------------------------------------------------------------------
# Detection input
# ---------------------------------------------------------------------------

def rule_pattern_map(detections: Any) -> dict[str, list[str]]:
    """Map detector pattern strings to sorted rule IDs using ``matches``."""
    document = detections if isinstance(detections, dict) else {}
    matches = document.get("matches", [])
    result: dict[str, set[str]] = defaultdict(set)
    if isinstance(matches, list):
        for match in matches:
            if not isinstance(match, Mapping):
                continue
            pattern = match.get("pattern")
            rule_id = match.get("rule_id")
            if isinstance(pattern, str) and isinstance(rule_id, str) and rule_id:
                result[pattern].add(rule_id)
    return {pattern: sorted(rule_ids) for pattern, rule_ids in result.items()}


def _detection_alerts(detections: Any) -> list[Any]:
    if isinstance(detections, list):
        return detections
    if not isinstance(detections, Mapping):
        raise ValueError("detections must be a JSON object or a list of alerts")
    for key in ("alerts", "detections"):
        value = detections.get(key)
        if isinstance(value, list):
            return value
    raise ValueError("detections document has no 'alerts' list")


def normalise_detections(detections: Any) -> NormalisedDetections:
    """Project a detector output document to evaluation units."""
    issues: list[dict[str, Any]] = []
    alerts = _detection_alerts(detections)
    document = detections if isinstance(detections, Mapping) else {}
    windows: Counter[int] = Counter()
    alert_ids: dict[EvaluationUnit, list[str]] = defaultdict(list)

    for index, alert in enumerate(alerts):
        if not isinstance(alert, Mapping):
            issues.append(_issue("detection", "alert is not an object", index=index))
            continue
        try:
            unit = _build_unit(
                window_id=alert.get("window_id"),
                service_id=alert.get("service_id", 0),
                ip=alert.get("ip", 0),
                status=alert.get("status", 0),
                rule_id=alert.get("rule_id"),
            )
        except ValueError as exc:
            issues.append(_issue(
                "detection", str(exc), index=index,
                alert_id=alert.get("alert_id"),
            ))
            continue
        raw_window = alert.get("window_seconds")
        if raw_window is not None:
            windows[_as_int(raw_window, "window_seconds")] += 1
        alert_ids[unit].append(str(alert.get("alert_id", f"index-{index}")))

    window_seconds = windows.most_common(1)[0][0] if windows else None
    for unit, ids in alert_ids.items():
        if len(ids) > 1:
            issues.append(_issue(
                "detection", "multiple alerts for one evaluation unit",
                unit=_unit_dict(unit), alert_ids=sorted(ids),
            ))

    source_info = {
        "algorithm_version": document.get("algorithm_version"),
        "alert_count": len(alerts),
        "match_count": len(document.get("matches", [])) if isinstance(
            document.get("matches", []), list
        ) else 0,
        "window_seconds": window_seconds,
    }
    return NormalisedDetections(
        predictions=set(alert_ids),
        alert_ids=dict(alert_ids),
        window_seconds=window_seconds,
        unresolved=issues,
        source_info=source_info,
    )


# ---------------------------------------------------------------------------
# Label inputs
# ---------------------------------------------------------------------------

def _resolve_rule_id(
    record: Mapping[str, Any],
    pattern_map: Mapping[str, list[str]],
    *,
    scope: str,
) -> str:
    explicit = record.get("rule_id")
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit.strip():
            raise ValueError("rule_id must be a non-empty string")
        return explicit.strip()
    pattern = record.get("pattern")
    if pattern is None and isinstance(record.get("rule"), Mapping):
        pattern = record["rule"].get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("label has neither rule_id nor rule pattern")
    candidates = pattern_map.get(pattern, [])
    if len(candidates) != 1:
        raise ValueError(
            f"rule pattern {pattern!r} maps to {len(candidates)} rule IDs; "
            "provide an explicit rule_id in the label"
        )
    return candidates[0]


def _label_stratum(record: Mapping[str, Any], default: str) -> str:
    if record.get("stratum") is not None:
        stratum = str(record["stratum"]).strip().lower()
    elif record.get("synthetic") is True:
        stratum = "synthetic"
    elif record.get("synthetic") is False:
        stratum = "natural"
    else:
        stratum = default
    if stratum not in STRATA:
        raise ValueError(f"unsupported stratum: {stratum!r}")
    return stratum


def _label_record(
    record: Mapping[str, Any],
    *,
    pattern_map: Mapping[str, list[str]],
    default_stratum: str,
    default_coverage: str,
    scope: str,
) -> EvaluationLabel:
    rule_id = _resolve_rule_id(record, pattern_map, scope=scope)
    unit = _build_unit(
        window_id=record.get("window_id"),
        service_id=record.get("service_id", record.get("service", 0)),
        ip=record.get("ip", 0),
        status=record.get("status", 0),
        rule_id=rule_id,
    )
    raw_label = record.get("label", record.get("expected_label"))
    label = normalise_label(raw_label)
    source = {
        key: record[key]
        for key in ("scenario_id", "label_id", "kind", "source_window")
        if key in record
    }
    return EvaluationLabel(
        unit=unit,
        label=label,
        stratum=_label_stratum(record, default_stratum),
        source=source,
    )


def _adapt_generator_manifest(
    manifest: Mapping[str, Any],
    pattern_map: Mapping[str, list[str]],
) -> NormalisedLabels:
    issues: list[dict[str, Any]] = []
    labels: list[EvaluationLabel] = []
    scenarios = manifest.get("scenarios", [])
    window_seconds = _as_int(manifest.get("window_seconds", 60), "window_seconds")
    if window_seconds < 1:
        raise ValueError("window_seconds must be positive")

    for scenario in sorted(scenarios, key=lambda s: str(s.get("scenario_id", ""))):
        if not isinstance(scenario, Mapping):
            issues.append(_issue("label", "scenario is not an object"))
            continue
        scenario_id = str(scenario.get("scenario_id", "unknown"))
        try:
            rule_id = _resolve_rule_id(scenario.get("rule", {}), pattern_map, scope="label")
            key = scenario.get("key", {})
            window = scenario.get("window", {})
            timestamp = scenario.get("timestamp", {})
            offset_start = _as_int(timestamp.get("start_offset_seconds"), "start_offset_seconds")
            offset_end = _as_int(
                timestamp.get(
                    "end_offset_seconds",
                    offset_start + _as_int(scenario.get("line_count", 1), "line_count") - 1,
                ),
                "end_offset_seconds",
            )
            scenario_ws = _as_int(
                window.get("seconds", window_seconds), "scenario window seconds"
            )
            base_epoch = _as_int(window["id"], "window id") * scenario_ws - offset_start
            first_window = (base_epoch + offset_start) // scenario_ws
            last_window = (base_epoch + offset_end) // scenario_ws
            expected = scenario.get("expected", {})
            raw_label = expected.get("label", _POSITIVE_SOURCE_LABEL)
            label = normalise_label(raw_label)
            line_range = scenario.get("line_range", {})
            for window_id in range(first_window, last_window + 1):
                labels.append(EvaluationLabel(
                    unit=_build_unit(
                        window_id=window_id,
                        service_id=key.get("service", 0),
                        ip=key.get("ip", 0),
                        status=key.get("status", 0),
                        rule_id=rule_id,
                    ),
                    label=label,
                    stratum="synthetic",
                    source={
                        "scenario_id": scenario_id,
                        "kind": scenario.get("kind"),
                        "line_range": line_range,
                        "source_window": window_id,
                    },
                ))
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(_issue(
                "label", f"scenario could not be adapted: {exc}",
                scenario_id=scenario_id,
            ))

    source_info = {
        "kind": manifest.get("kind"),
        "schema_version": manifest.get("schema_version"),
        "generator": manifest.get("generator"),
        "source": manifest.get("source"),
        "label_unit": manifest.get("label_unit"),
        "scenario_count": len(scenarios),
        "adaptation": (
            "scenario key + rule pattern projected to "
            "(window_id, service_id, ip, status, rule_id); "
            "multi-window scenarios expanded across every touched window"
        ),
    }
    return NormalisedLabels(
        labels=labels,
        coverage="positive-only",
        scope="all_detections",
        kind="sentinel.ground-truth",
        window_seconds=window_seconds,
        source_info=source_info,
        unresolved=issues,
    )


def normalise_labels(
    labels: Any,
    pattern_map: Mapping[str, list[str]],
) -> NormalisedLabels:
    """Normalise either supported label document to unit-level labels."""
    if isinstance(labels, list):
        labels = {"kind": "sentinel.evaluation-labels", "labels": labels}
    if not isinstance(labels, Mapping):
        raise ValueError("labels must be a JSON object or a list of label records")

    version = labels.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported label schema_version: {version!r}")

    if labels.get("kind") == "sentinel.ground-truth" or (
        "scenarios" in labels and labels.get("synthetic") is True
    ):
        return _adapt_generator_manifest(labels, pattern_map)

    if labels.get("kind") not in (None, "sentinel.evaluation-labels"):
        raise ValueError(f"unsupported label document kind: {labels.get('kind')!r}")
    records = labels.get("labels")
    if not isinstance(records, list):
        raise ValueError("evaluation labels document needs a 'labels' list")

    coverage = str(labels.get("coverage", "positive-only")).strip().lower()
    if coverage not in COVERAGES:
        raise ValueError(f"unsupported coverage: {coverage!r}")
    scope = str(labels.get("scope", "labelled_units_only")).strip().lower()
    if scope not in SCOPES:
        raise ValueError(f"unsupported scope: {scope!r}")
    default_stratum = str(labels.get("stratum", "natural")).strip().lower()
    if default_stratum not in STRATA:
        raise ValueError(f"unsupported default stratum: {default_stratum!r}")

    issues: list[dict[str, Any]] = []
    result: list[EvaluationLabel] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            issues.append(_issue("label", "label is not an object", index=index))
            continue
        try:
            item = _label_record(
                record,
                pattern_map=pattern_map,
                default_stratum=default_stratum,
                default_coverage=coverage,
                scope=scope,
            )
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(_issue(
                "label", str(exc), index=index,
                label_id=record.get("label_id", record.get("scenario_id")),
            ))
            continue
        result.append(item)

    raw_window = labels.get("window_seconds")
    window_seconds = None if raw_window is None else _as_int(raw_window, "window_seconds")
    source_info = {
        "kind": labels.get("kind", "sentinel.evaluation-labels"),
        "schema_version": version,
        "label_count": len(records),
        "source": labels.get("source"),
    }
    return NormalisedLabels(
        labels=result,
        coverage=coverage,
        scope=scope,
        kind="sentinel.evaluation-labels",
        window_seconds=window_seconds,
        source_info=source_info,
        unresolved=issues,
    )


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def _count_dict(matrix: ConfusionMatrix, coverage: str) -> dict[str, int | None]:
    """Render confusion counts, hiding TN when negatives were never enumerated."""
    counts: dict[str, int | None] = matrix.as_dict()  # type: ignore[assignment]
    if coverage == "positive-only" and counts["tn"] == 0:
        counts["tn"] = None
    return counts


def _count_cell(value: int | None) -> str:
    return "N/A" if value is None else str(value)


def _stratum_result(
    stratum: str,
    labels: list[EvaluationLabel],
    predictions: set[EvaluationUnit],
    detection_issues: list[dict[str, Any]],
    *,
    coverage: str,
    scope: str,
) -> dict[str, Any]:
    if not labels:
        return {
            "stratum": stratum,
            "coverage": coverage,
            "scope": scope,
            "counts": {
                "tp": 0, "fp": 0, "tn": None if coverage == "positive-only" else 0,
                "fn": 0,
                "positives": 0, "negatives": 0,
                "predicted_units": 0, "evaluation_units": 0,
            },
            "metrics": compute_metrics(
                count_confusion({}, []), evaluation_units=0, fpr_denominator=None
            ).as_dict(),
            "missing": {
                "predictions_for_labels": [],
                "labels_for_predictions": [],
                "duplicate_predictions": [],
                "conflicting_labels": [],
            },
            "unresolved": [],
            "note": "no labels supplied for this stratum; metrics are undefined",
        }

    label_values: dict[EvaluationUnit, list[str]] = defaultdict(list)
    for item in labels:
        label_values[item.unit].append(item.label)
    label_map = {unit: sorted(values)[0] for unit, values in label_values.items()}
    conflicts = [
        {"unit": _unit_dict(unit), "values": sorted(values)}
        for unit, values in sorted(label_values.items(), key=lambda pair: _unit_key(pair[0]))
        if len(set(values)) > 1
    ]

    matrix = count_confusion(label_map, predictions)
    evaluation_units = len(set(label_map) | predictions)
    fpr_denominator = matrix.fp + matrix.tn if coverage == "closed" else None
    metrics = compute_metrics(
        matrix, evaluation_units=evaluation_units, fpr_denominator=fpr_denominator
    )

    positives = {unit for unit, value in label_map.items() if value == "anomalous"}
    missing_predictions = sorted(positives - predictions, key=_unit_key)
    missing_labels = sorted(predictions - set(label_map), key=_unit_key)
    duplicate_predictions = [
        {"unit": _unit_dict(unit), "alert_ids": sorted(ids)}
        for unit, ids in sorted(
            detection_issues_alert_ids(detection_issues).items(),
            key=lambda pair: _unit_key(pair[0]),
        )
        if unit in predictions
    ]

    return {
        "stratum": stratum,
        "coverage": coverage,
        "scope": scope,
        "counts": {
            **_count_dict(matrix, coverage),
            "positives": len(positives),
            "negatives": sum(1 for value in label_map.values() if value == "normal"),
            "predicted_units": len(predictions),
            "evaluation_units": evaluation_units,
        },
        "metrics": metrics.as_dict(),
        "missing": {
            "predictions_for_labels": _sorted_units(missing_predictions),
            "labels_for_predictions": _sorted_units(missing_labels),
            "duplicate_predictions": duplicate_predictions,
            "conflicting_labels": conflicts,
        },
        "unresolved": _stable_issues([
            issue for issue in detection_issues
            if issue.get("reason") == "multiple alerts for one evaluation unit"
        ]),
    }


def detection_issues_alert_ids(
    issues: Iterable[dict[str, Any]],
) -> dict[EvaluationUnit, list[str]]:
    """Extract duplicate-alert details from normalisation issues."""
    result: dict[EvaluationUnit, list[str]] = {}
    for issue in issues:
        unit = issue.get("unit")
        if issue.get("reason") != "multiple alerts for one evaluation unit" or not isinstance(unit, dict):
            continue
        try:
            parsed = _build_unit(**unit)
        except (TypeError, ValueError):
            continue
        result[parsed] = [str(value) for value in issue.get("alert_ids", [])]
    return result


def _merge_labels(labels: list[EvaluationLabel]) -> tuple[dict[EvaluationUnit, str], list[dict[str, Any]]]:
    values: dict[EvaluationUnit, list[str]] = defaultdict(list)
    for item in labels:
        values[item.unit].append(item.label)
    merged: dict[EvaluationUnit, str] = {}
    issues: list[dict[str, Any]] = []
    for unit, items in sorted(values.items(), key=lambda pair: _unit_key(pair[0])):
        distinct = sorted(set(items))
        if len(distinct) > 1:
            issues.append(_issue(
                "report", "conflicting labels across strata",
                unit=_unit_dict(unit), values=distinct,
            ))
        else:
            merged[unit] = distinct[0]
    return merged, issues


def evaluate_documents(
    labels: Any,
    detections: Any,
    *,
    labels_sha256: str | None = None,
    detections_sha256: str | None = None,
) -> dict[str, Any]:
    """Evaluate in-memory documents and return a JSON-serialisable report."""
    pattern_map = rule_pattern_map(detections)
    normalised_labels = normalise_labels(labels, pattern_map)
    normalised_detections = normalise_detections(detections)

    window_seconds = normalised_labels.window_seconds
    if window_seconds is None:
        window_seconds = normalised_detections.window_seconds
    elif (
        normalised_detections.window_seconds is not None
        and window_seconds != normalised_detections.window_seconds
    ):
        normalised_labels.unresolved.append(_issue(
            "report", "label and detection window_seconds differ",
            labels=window_seconds, detections=normalised_detections.window_seconds,
        ))

    all_unresolved = _stable_issues(
        normalised_labels.unresolved + normalised_detections.unresolved
    )
    strata: dict[str, Any] = {}
    for stratum in STRATA:
        stratum_labels = [item for item in normalised_labels.labels if item.stratum == stratum]
        scope = normalised_labels.scope if stratum_labels else "labelled_units_only"
        predictions = (
            set(normalised_detections.predictions)
            if scope == "all_detections" and stratum_labels
            else {
                unit for unit in normalised_detections.predictions
                if unit in {item.unit for item in stratum_labels}
            }
        )
        strata[stratum] = _stratum_result(
            stratum, stratum_labels, predictions,
            normalised_detections.unresolved,
            coverage=normalised_labels.coverage,
            scope=scope,
        )

    merged_labels, merge_issues = _merge_labels(normalised_labels.labels)
    all_unresolved = _stable_issues(all_unresolved + merge_issues)
    all_scopes = {
        item["scope"] for item in strata.values()
        if item.get("counts", {}).get("evaluation_units")
    }
    total_predictions = (
        set(normalised_detections.predictions)
        if "all_detections" in all_scopes
        else set(merged_labels) & normalised_detections.predictions
    )
    total = _stratum_result(
        "all", list(normalised_labels.labels), total_predictions,
        normalised_detections.unresolved,
        coverage=normalised_labels.coverage,
        scope="all_detections" if "all_detections" in all_scopes else "labelled_units_only",
    )
    # Totals must not double-count a unit that appears in two strata.
    total_matrix = count_confusion(merged_labels, total_predictions)
    total_units = len(set(merged_labels) | total_predictions)
    fpr_denominator = (
        total_matrix.fp + total_matrix.tn
        if normalised_labels.coverage == "closed" else None
    )
    total_counts = {
        **_count_dict(total_matrix, normalised_labels.coverage),
        "positives": sum(1 for value in merged_labels.values() if value == "anomalous"),
        "negatives": sum(1 for value in merged_labels.values() if value == "normal"),
        "predicted_units": len(total_predictions),
        "evaluation_units": total_units,
    }
    missing_all = {
        key: [
            {**entry, "stratum": "all"} if isinstance(entry, dict) else entry
            for entry in total["missing"][key]
        ]
        for key in total["missing"]
    }
    combined_missing = []
    for stratum in STRATA:
        for key, entries in strata[stratum]["missing"].items():
            for entry in entries:
                combined_missing.append({**entry, "stratum": stratum} if isinstance(entry, dict) else entry)
    combined_missing = _stable_issues(combined_missing)

    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "sentinel.evaluation-report",
        "evaluation_unit": {
            "fields": list(EVALUATION_UNIT_FIELDS),
            "description": (
                "one alert/label per (window_id, service_id, ip, status, rule_id); "
                "ip is the detector's host-order uint32, service_id uses SERVICE_TAGS"
            ),
        },
        "coverage_policy": {
            "positive-only": (
                "unmatched alerts count as FP relative to known injections; "
                "TN/FPR are undefined and render as N/A"
            ),
            "closed": (
                "all units in the label scope are labelled; alerts outside the "
                "label set are FP and contribute to FPR"
            ),
        },
        "inputs": {
            "labels": {
                **normalised_labels.source_info,
                "normalised_label_count": len(normalised_labels.labels),
                "coverage": normalised_labels.coverage,
                "scope": normalised_labels.scope,
                "sha256": labels_sha256,
            },
            "detections": {
                **normalised_detections.source_info,
                "normalised_prediction_count": len(normalised_detections.predictions),
                "sha256": detections_sha256,
            },
        },
        "window_seconds": window_seconds,
        "strata": strata,
        "totals": {
            "stratum": "all",
            "coverage": normalised_labels.coverage,
            "counts": total_counts,
            "metrics": compute_metrics(
                total_matrix, evaluation_units=total_units,
                fpr_denominator=fpr_denominator,
            ).as_dict(),
            "missing": missing_all,
        },
        "unresolved": all_unresolved,
        "missing": combined_missing,
    }
    return report


def evaluate_files(labels_path: str | Path, detections_path: str | Path) -> dict[str, Any]:
    """Load two JSON evidence files and evaluate them."""
    labels, labels_sha = load_json_document(labels_path)
    detections, detections_sha = load_json_document(detections_path)
    return evaluate_documents(
        labels, detections,
        labels_sha256=labels_sha,
        detections_sha256=detections_sha,
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

_METRIC_COLUMNS = (
    ("precision", "Precision"),
    ("recall", "Recall"),
    ("f1", "F1"),
    ("false_positive_rate", "FPR"),
    ("fp_per_1000_units", "FP/1k"),
    ("fp_per_10000_units", "FP/10k"),
)


def _metrics_row(block: Mapping[str, Any], label: str) -> str:
    counts = block["counts"]
    metrics = block["metrics"]
    cells = [
        label, _count_cell(counts["tp"]), _count_cell(counts["fp"]),
        _count_cell(counts["tn"]), _count_cell(counts["fn"]),
    ]
    cells.extend(format_metric(metrics[key], 3) for key, _ in _METRIC_COLUMNS)
    cells.append(str(counts["evaluation_units"]))
    return "| " + " | ".join(cells) + " |"


def _metrics_table(blocks: Iterable[tuple[str, Mapping[str, Any]]]) -> str:
    header = (
        "| Stratum | TP | FP | TN | FN | Precision | Recall | F1 | FPR "
        "| FP/1k | FP/10k | Units |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    return "\n".join([header, *(_metrics_row(block, label) for label, block in blocks)])


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render a detection-quality report; undefined values become ``N/A``."""
    sections = [
        "# Detection-quality evaluation",
        "",
        f"Evaluation unit: `{', '.join(report['evaluation_unit']['fields'])}`. "
        f"Window seconds: `{report.get('window_seconds')}`. "
        "Natural and synthetic labels are reported separately; an alert with no "
        "label in a stratum is not silently assigned to another stratum.",
        "",
        "## Natural labels",
        "",
        _metrics_table([("natural", report["strata"]["natural"])]),
        "",
        "## Synthetic labels",
        "",
        _metrics_table([("synthetic", report["strata"]["synthetic"])]),
        "",
        "## Combined (context only)",
        "",
        _metrics_table([("all", report["totals"])]),
        "",
        "N/A means the denominator is zero or the metric is undefined for the "
        "declared label coverage. For positive-only label sets, TN and FPR are "
        "undefined; unmatched alerts are still reported as FP.",
        "",
        "## Missing and unresolved items",
        "",
        f"- unresolved items: {len(report['unresolved'])}",
        f"- missing-item records: {len(report['missing'])}",
    ]
    for item in report["unresolved"][:20]:
        sections.append(f"  - unresolved: `{json.dumps(item, sort_keys=True, default=str)}`")
    for item in report["missing"][:20]:
        sections.append(f"  - missing: `{json.dumps(item, sort_keys=True, default=str)}`")
    if len(report["unresolved"]) > 20 or len(report["missing"]) > 20:
        sections.append("  - additional items are listed in the JSON report")
    return "\n".join(sections) + "\n"
