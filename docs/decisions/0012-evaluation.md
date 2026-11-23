# ADR-0012 — Detection-quality evaluation and benchmark scope rules

* Status: accepted
* Date: 2026-11-23
* Deciders: project owner, agent

## Context

Sprint 8 needs reproducible detection-quality numbers and a benchmark report
that does not turn diagnostics into speedup claims. The detector emits alerts
keyed by `(window_id, service_id, ip, status, rule_id)`; the dataset generator
emits a separate `sentinel.ground-truth` manifest whose scenarios are recorded
at `(window, key, rule pattern)` granularity (ADR-0008). Saved benchmark JSON
contains a full CPU-pipeline baseline and a GPU staging/VRAM diagnostic, but no
GPU kernel-scope timing and no end-to-end run.

Labels and predictions must be joined without leaking labels into detection,
and every report must make its timing scope and label coverage visible.

## Decision

### Evaluation unit

One evaluation unit is:

```text
(window_id, service_id, ip, status, rule_id)
```

* `window_id` comes from the alert and from the label's window projection.
* `service_id` uses the detector's `SERVICE_TAGS` integers (`sshd=1`, `nginx=2`,
  `app-server=3`, `kernel=4`, `auth-daemon=5`, `0=unknown`).
* `ip` is the detector's host-order `uint32`. Dotted IPv4 strings in labels are
  converted to the same integer. Missing IP is `0`.
* `status` is the HTTP-like status integer; missing status is `0`.
* `rule_id` is the detector rule identifier, not the human-readable pattern.

A generator scenario is adapted to this unit as follows: the scenario's `key`
supplies service/IP/status, `window.id` plus `timestamp.*_offset_seconds`
determines every touched `window_id`, and `rule.pattern` is resolved to exactly
one `rule_id` using the `matches` array of the detector output. A pattern that
maps to zero or several rule IDs is recorded in `unresolved` and is **not**
guessed. The sample's `event_rate_burst` scenarios use a pattern that is not
configured in `tests/fixtures/cpu/rules.json`; those scenarios are correctly
reported as unresolved.

`src/sentinel/labels.py` remains the authoritative helper for resolving
scenario line ranges to stable line IDs. Evaluation does not re-read dataset
bytes and does not feed label data to workers.

### Metric definitions and zero denominators

For a set of labelled units `L` and predicted units `P` at the unit above:

* `TP = |L_anomalous ∩ P|`
* `FN = L_anomalous − P`
* `TN = |L_normal − P|`
* `FP = |P − L_anomalous|`, including predictions with no label.

Definitions:

* `precision = TP / (TP + FP)`
* `recall = TP / (TP + FN)`
* `F1 = 2·precision·recall / (precision + recall)`, with `F1 = 0` when both
  rates are defined and zero
* `false_positive_rate = FP / (FP + TN)` only when negative units are known
* `FP/1k = 1000·FP / |L ∪ P|`, `FP/10k = 10000·FP / |L ∪ P|`

Every undefined rate is `None` in JSON and `N/A` in markdown. No denominator
check ever returns `0.0` merely because it divided by zero. When `precision`
or `recall` is undefined, `F1` is also undefined.

### Label coverage and strata

A label document declares:

* `coverage = positive-only` — only anomalous units are labelled. Unmatched
  alerts are reported as FP relative to known injections, but **TN and FPR are
  undefined** because the negative population is not enumerated. This is the
  coverage of the generator manifest.
* `coverage = closed` — all units in the declared scope are labelled; alerts
  outside the label set are FP and contribute to FPR.

`scope = all_detections` scores all alerts in that stratum (used for the
synthetic manifest that covers the whole sample dataset). `scope =
labelled_units_only` scores only alerts whose unit appears in that stratum, so
a sparse natural-label sample never absorbs unrelated alerts. Natural and
synthetic labels are always emitted in separate tables and totals; an alert is
never silently assigned to a stratum that has no matching label.

### Benchmark scope rules

* **Kernel-only**: native pattern match + window aggregation/scoring over a
  staged input. The committed `kernel-parity.json` pair (CPU 2769.1 ms,
  GPU 81.5 ms, reported 34.0x, best of three) has this scope.
  `cpu-baseline.json` supplies input identity and detector config but contains
  no GPU kernel timing; the report reads the pair from the saved JSON rather
  than recomputing it.
* **Full CPU pipeline**: `cpu-baseline.json` rows include parsing, features and
  alerts; they must never be divided by a kernel-only GPU time.
* **Staging/VRAM**: `cuda-memory-pipeline.json` phase totals, wall time and
  sampled VRAM are memory-pipeline diagnostics and are explicitly **NOT** a
  CPU/GPU speedup result.
* **Not run**: 5-repetition workload matrix, PR-AUC, full end-to-end benchmark,
  and pageable-memory baseline. The report lists these as `Not run` rather
  than implying they were measured.

## Consequences

* Evaluation is deterministic and JSON-serialisable; undefined values are
  never shown as misleading zeroes.
* Reports expose unresolved scenarios, missing predictions and missing labels,
  so imperfect label projection is visible instead of silently improving or
  degrading scores.
* Synthetic results are clearly separated from natural results and are not
  presented as real-world detection quality.
* The benchmark report links raw JSON evidence and preserves the distinction
  between compute, memory diagnostics and unmeasured scenarios.
