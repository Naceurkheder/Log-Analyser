"""Sprint 8 metrics: confusion counting, rates and zero denominators."""

from __future__ import annotations

from sentinel.evaluation.metrics import (
    ConfusionMatrix,
    compute_metrics,
    count_confusion,
    format_metric,
)

U1 = (100, 1, 3405803777, 0, "auth-failed-password")
U2 = (101, 2, 3405803778, 404, "http-not-found")
N1 = (102, 3, 0, 0, "app-error")
N2 = (103, 4, 0, 0, "kernel-info")


def test_perfect_prediction() -> None:
    labels = {U1: "anomalous", U2: "anomalous", N1: "normal", N2: "normal"}
    matrix = count_confusion(labels, [U1, U2])
    metrics = compute_metrics(matrix, evaluation_units=4, fpr_denominator=2)

    assert matrix == ConfusionMatrix(tp=2, fp=0, tn=2, fn=0)
    assert metrics.precision == 1.0
    assert metrics.recall == 1.0
    assert metrics.f1 == 1.0
    assert metrics.false_positive_rate == 0.0
    assert metrics.fp_per_1000_units == 0.0
    assert metrics.fp_per_10000_units == 0.0


def test_all_wrong_prediction() -> None:
    labels = {U1: "anomalous", U2: "anomalous", N1: "normal", N2: "normal"}
    matrix = count_confusion(labels, [N1, N2])
    metrics = compute_metrics(matrix, evaluation_units=4, fpr_denominator=2)

    assert matrix == ConfusionMatrix(tp=0, fp=2, tn=0, fn=2)
    assert metrics.precision == 0.0
    assert metrics.recall == 0.0
    assert metrics.f1 == 0.0
    assert metrics.false_positive_rate == 1.0


def test_empty_inputs_have_no_rates() -> None:
    matrix = count_confusion({}, [])
    metrics = compute_metrics(matrix, evaluation_units=0, fpr_denominator=None)

    assert matrix == ConfusionMatrix(0, 0, 0, 0)
    assert all(value is None for value in metrics.as_dict().values())
    assert format_metric(metrics.precision) == "N/A"
    assert format_metric(metrics.fp_per_1000_units) == "N/A"


def test_zero_denominators_are_none_not_zero() -> None:
    # No positives: recall is undefined even though the single prediction is FP.
    matrix = count_confusion({N1: "normal"}, [N1])
    metrics = compute_metrics(matrix, evaluation_units=1, fpr_denominator=1)
    assert matrix == ConfusionMatrix(tp=0, fp=1, tn=0, fn=0)
    assert metrics.precision == 0.0
    assert metrics.recall is None
    assert metrics.f1 is None
    assert metrics.false_positive_rate == 1.0

    # No negatives: FPR is undefined for positive-only label coverage.
    positive_only = compute_metrics(matrix, evaluation_units=1, fpr_denominator=None)
    assert positive_only.false_positive_rate is None
    assert format_metric(positive_only.false_positive_rate) == "N/A"

    # No predictions: precision and FP rates are undefined, FN is still counted.
    missed = count_confusion({U1: "anomalous"}, [])
    missed_metrics = compute_metrics(
        missed, evaluation_units=1, fpr_denominator=None,
    )
    assert missed == ConfusionMatrix(tp=0, fp=0, tn=0, fn=1)
    assert missed_metrics.precision is None
    assert missed_metrics.recall == 0.0
    assert missed_metrics.fp_per_1000_units == 0.0


def test_labels_accept_aliases_and_reject_unknown_values() -> None:
    assert count_confusion({U1: True}, [U1]).tp == 1
    assert count_confusion({U1: "positive"}, [U1]).tp == 1
    assert count_confusion({N1: "False"}, []).tn == 1
    try:
        count_confusion({U1: "maybe"}, [U1])
    except ValueError:
        pass
    else:
        raise AssertionError("unknown label values must be rejected")
