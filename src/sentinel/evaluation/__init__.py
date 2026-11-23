"""Sprint 8 detection-quality evaluation.

The evaluation unit is ``(window_id, service_id, ip, status, rule_id)``.  See
``docs/decisions/0012-evaluation.md`` for the metric definitions and the
zero-denominator policy.
"""

from .metrics import (
    ConfusionMatrix,
    MetricSet,
    compute_metrics,
    count_confusion,
    format_metric,
)
from .report import evaluate_documents, evaluate_files, render_markdown

__all__ = [
    "ConfusionMatrix",
    "MetricSet",
    "compute_metrics",
    "count_confusion",
    "format_metric",
    "evaluate_documents",
    "evaluate_files",
    "render_markdown",
]
