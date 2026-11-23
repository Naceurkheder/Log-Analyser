"""Confusion counting and detection-quality metrics (Sprint 8 / S8-B).

Everything in this module is pure stdlib and has no detector, API or queue
dependencies.  The evaluation unit is documented in
``docs/decisions/0012-evaluation.md`` as::

    (window_id, service_id, ip, status, rule_id)

A label set and a prediction set are both sets of those units.  Undefined
rates are ``None``; callers must render them as ``N/A`` (never as ``0``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

EvaluationUnit = tuple[int, int, int, int, str]

_POSITIVE_LABELS = frozenset({"anomalous", "anomaly", "positive", "pos", "true", "yes", "1"})
_NEGATIVE_LABELS = frozenset({"normal", "negative", "neg", "false", "no", "0"})


@dataclass(frozen=True)
class ConfusionMatrix:
    """Counts at the documented evaluation unit."""

    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"tp": self.tp, "fp": self.fp, "tn": self.tn, "fn": self.fn}

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.tn + self.fn


@dataclass(frozen=True)
class MetricSet:
    """Rates derived from a confusion matrix.

    ``None`` means the denominator is zero or the metric is not defined by the
    label coverage policy.  JSON serialisation keeps ``None`` as ``null``; the
    markdown renderer turns it into ``N/A``.
    """

    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    false_positive_rate: float | None = None
    fp_per_1000_units: float | None = None
    fp_per_10000_units: float | None = None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "precision": _round(self.precision),
            "recall": _round(self.recall),
            "f1": _round(self.f1),
            "false_positive_rate": _round(self.false_positive_rate),
            "fp_per_1000_units": _round(self.fp_per_1000_units),
            "fp_per_10000_units": _round(self.fp_per_10000_units),
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def format_metric(value: float | None, digits: int = 3) -> str:
    """Render a rate for humans.  Undefined values are always ``N/A``."""
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}"


def _divide(numerator: int, denominator: int | None) -> float | None:
    if denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def normalise_label(value: Any) -> str:
    """Map a label value to ``anomalous`` or ``normal``.

    Booleans and 0/1 are accepted so hand-written evaluation manifests remain
    readable, but numeric values other than 0/1 are rejected.
    """
    if isinstance(value, bool):
        return "anomalous" if value else "normal"
    if isinstance(value, int) and value in (0, 1):
        return "anomalous" if value else "normal"
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _POSITIVE_LABELS:
            return "anomalous"
        if token in _NEGATIVE_LABELS:
            return "normal"
    raise ValueError(f"unsupported evaluation label: {value!r}")


def is_positive(value: Any) -> bool:
    """True when ``value`` is the positive/anomalous class."""
    return normalise_label(value) == "anomalous"


def count_confusion(
    labels: Mapping[EvaluationUnit, Any],
    predictions: Iterable[EvaluationUnit],
) -> ConfusionMatrix:
    """Count TP/FP/TN/FN for sets of labelled and predicted units.

    Predicted units that have no label are counted as FP (closed-world
    scoring).  The caller decides whether that is meaningful for a stratum by
    supplying ``fpr_denominator`` to :func:`compute_metrics` and by recording
    the label coverage policy in the report.
    """
    predicted = set(predictions)
    tp = fp = tn = fn = 0
    for unit, raw_label in labels.items():
        label = normalise_label(raw_label)
        if label == "anomalous":
            if unit in predicted:
                tp += 1
            else:
                fn += 1
        elif unit in predicted:
            fp += 1
        else:
            tn += 1
    fp += len(predicted.difference(labels))
    return ConfusionMatrix(tp=tp, fp=fp, tn=tn, fn=fn)


def compute_metrics(
    matrix: ConfusionMatrix,
    *,
    evaluation_units: int,
    fpr_denominator: int | None = None,
) -> MetricSet:
    """Derive rates.  Every zero denominator becomes ``None``.

    ``fpr_denominator`` is the number of units known to be negative.  Pass
    ``None`` for positive-only label sets: in that case FP is still reported
    (unmatched alerts are false positives relative to known injections), but
    a false-positive *rate* would be undefined and must render as ``N/A``.
    """
    precision = _divide(matrix.tp, matrix.tp + matrix.fp)
    recall = _divide(matrix.tp, matrix.tp + matrix.fn)
    if precision is None or recall is None:
        f1 = None
    elif precision + recall == 0.0:
        # Both rates are defined and zero: the conventional F1 is 0, not N/A.
        f1 = 0.0
    else:
        f1 = 2.0 * precision * recall / (precision + recall)
    return MetricSet(
        precision=precision,
        recall=recall,
        f1=f1,
        false_positive_rate=_divide(matrix.fp, fpr_denominator),
        fp_per_1000_units=(
            None if evaluation_units <= 0 else 1000.0 * matrix.fp / evaluation_units
        ),
        fp_per_10000_units=(
            None if evaluation_units <= 0 else 10000.0 * matrix.fp / evaluation_units
        ),
    )
