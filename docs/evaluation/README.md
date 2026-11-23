# Evaluation evidence (Sprint 8)

* `sample-labels.json` — a small `sentinel.ground-truth` manifest produced by
  `scripts/gen_dataset.py --lines 2000 --seed 11 --burst-every 60` (the dataset
  itself is not committed).
* `sample-detections.json` — a compact projection of CPU-oracle output: every
  alert plus distinct `(rule_id, pattern)` pairs from `detect(...).to_dict()`.
* `sample-evaluation.json` — the report produced from those two inputs by the
  command below.
* `benchmark-report.md` — rendered from saved benchmark JSON and the evaluation
  report; it contains no newly executed benchmark.

Reproduce the committed reports from the repository root:

```bash
PYTHONPATH=src .venv/bin/python -m sentinel.evaluation.cli evaluate \
  --labels docs/evaluation/sample-labels.json \
  --detections docs/evaluation/sample-detections.json \
  --out docs/evaluation/sample-evaluation.json
PYTHONPATH=src .venv/bin/python scripts/benchmark_report.py \
  --evaluation-report docs/evaluation/sample-evaluation.json \
  --out docs/evaluation/benchmark-report.md
```

The full regeneration sequence for `sample-labels.json` and
`sample-detections.json` is in `benchmark-report.md` under **Reproduction**.
Metric and scope definitions are in `docs/decisions/0012-evaluation.md`.
