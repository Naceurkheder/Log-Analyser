# Sentinel-GPU benchmark and evaluation report

This report is rendered from **saved evidence only**. No benchmark, repetition or end-to-end run was executed to produce it. Timing scopes are never mixed: kernel-only, full-pipeline CPU and staging/VRAM diagnostics are separate sections.

## Environment and configuration metadata

| Item | Value | Evidence |
|---|---|---|
| Evidence date | 2026-11-23 | kernel-parity.json |
| Host profile | 16g (measured on the development laptop) | cuda-memory-pipeline.json |
| GPU | NVIDIA GeForce RTX 3050 Laptop GPU | cuda-memory-pipeline.json |
| GPU driver | 550.163.01 | cuda-memory-pipeline.json |
| GPU VRAM (MiB) | 4096 | cuda-memory-pipeline.json |
| Input | bench.log | cpu-baseline.json |
| Input bytes | 100007449 | cpu-baseline.json |
| Input SHA-256 | bff90c6ec7ac08c43b4e9b63ed65fbf51d0ec1e661b2491e701c64129469c801 | cpu-baseline.json |
| Algorithm version | cpu-reference-v1 | cpu-baseline.json |
| Detector config hash | 684584f351452326907d8386373ccf08dad30fd75c14b7cf70c5db92af477696 | cpu-baseline.json |
| Rules | auth-failed-password, auth-invalid-user, app-error, http-not-found, app-segfault | cpu-baseline.json |
| Window seconds | 60 | cpu-baseline.json |
| Baseline windows | 2 | cpu-baseline.json |
| Z threshold | 3.0 | cpu-baseline.json |

## Raw evidence

- [CPU baseline JSON](../benchmarks/cpu-baseline.json)
- [CPU baseline CSV](../benchmarks/cpu-baseline.csv)
- [CUDA memory pipeline JSON](../benchmarks/cuda-memory-pipeline.json)
- [Kernel parity JSON](../benchmarks/kernel-parity.json)
- [Benchmark methodology README](../benchmarks/README.md)
- [Detection-quality sample report](sample-evaluation.json)

## Kernel-only CPU vs GPU timing (scope: native detect over staged 100 MB input)

| Backend | Time (ms) | Throughput context | Scope |
|---|---|---|---|
| CPU kernel path | 2769.1 | same 100 MB input | native detect() kernel-path benchmark over one fixed 100 MB synthetic chunk |
| GPU kernel path | 81.5 | same 100 MB input | native detect() kernel-path benchmark over one fixed 100 MB synthetic chunk |
| Speedup | 34.0x | CPU ms / GPU ms | kernel scope only |

Provenance: Final verification output of `make bench-kernels` on the integrated mainline; summary transcribed from the harness stdout. Repetitions: best time of three repetitions, matching scripts/run_bench.sh. Kernel-scope timing only. It excludes ingestion, queueing, object storage, worker commit and dashboard overhead. Do not read it as an end-to-end platform speedup. `cpu-baseline.json` supplies the full-pipeline CPU reference and input identity; it contains no GPU kernel timing. This report does not run benchmarks; it only renders the committed kernel-parity JSON.

## CPU full-pipeline baseline (different scope — not comparable to kernel-only)

| Mode | Threads | Best ms | Mean ms | MB/s | Alerts | Features |
|---|---|---|---|---|---|---|
| cpu-reference-single-thread | 1 | 27085.293 | 27612.027 | 3.69 | 3129 | 84500 |
| cpu-worker-threaded | 2 | 48982.251 | 49189.8 | 2.04 | 1553 | 90674 |

Oracle row: `cpu-reference-single-thread`, 3 repetitions. This includes Python parsing, feature extraction and alert generation; do not divide its timing by a kernel-only GPU timing and call the result a speedup.

## Staging / VRAM diagnostic — NOT a speedup result

| Stage MiB | Chunks | Wall ms | Native total ms | Kernel ms | Peak VRAM MiB | MB/s |
|---|---|---|---|---|---|---|
| 2 | 48 | 2030.132 | 393.785 | 1.812 | 121 | 46.98 |
| 4 | 24 | 1749.627 | 185.083 | 0.927 | 143 | 54.51 |
| 8 | 12 | 1699.74 | 163.066 | 0.574 | 113 | 56.11 |
| 16 | 6 | 1624.165 | 142.498 | 0.276 | 157 | 58.72 |

Large stream (4.25 GiB, larger than the 4096 MiB device): 79623.777 ms wall, 6724.927 ms native total, 157 MiB peak VRAM. These numbers measure bounded staging, allocation and host/device transfer behaviour. They are **NOT** a CPU/GPU speedup result.

## Detection quality

Evaluation unit: `window_id, service_id, ip, status, rule_id`. Window seconds: `60`. Natural and synthetic label strata are reported separately.

### Natural labels

| Stratum | TP | FP | TN | FN | Precision | Recall | F1 | FPR | FP/1k | FP/10k | Units |
|---|---|---|---|---|---|---|---|---|---|---|---|
| natural | 0 | 0 | N/A | 0 | N/A | N/A | N/A | N/A | N/A | N/A | 0 |

### Synthetic labels

| Stratum | TP | FP | TN | FN | Precision | Recall | F1 | FPR | FP/1k | FP/10k | Units |
|---|---|---|---|---|---|---|---|---|---|---|---|
| synthetic | 4 | 4 | N/A | 13 | 0.500 | 0.235 | 0.320 | N/A | 190.476 | 1904.762 | 21 |

Unresolved items: 4. Missing-item records: 17. Undefined rates are shown as `N/A`. For the positive-only generator manifest, TN/FPR are undefined and unmatched alerts are counted as FP relative to known injections.

## Limitations

- All measurements come from **one physical host** with **one GPU**; container isolation does not create independent hardware.
- Infrastructure uses the **moto emulator** for S3/SQS/DynamoDB, so no cloud-service latency or availability claim is made.
- Synthetic labels are derived from injected bursts and carry **synthetic label bias**; unmatched alerts may be genuine traffic anomalies rather than detector errors.
- The 2,000-line sample and the 100 MB bench are not representative of all log sources, services or incident types; **dataset representativeness** is limited.
- **Thermal throttling** and clock variation were not recorded for the saved kernel figures; only the best of repeated trials is reported.
- Kernel-scope timing excludes ingestion, tokenisation, queueing and storage. Staging/VRAM numbers are diagnostics, not speedups.

## Not run (explicit)

- **5-repetition workload matrix** — not run. Saved CPU evidence has 3 repetitions; no 100 MiB/500 MiB/1 GiB/2 GiB × chunk-size × backend matrix was executed in S8.
- **PR-AUC** — not run. The sample provides binary unit labels, not a threshold sweep with calibrated scores.
- **Full end-to-end benchmark** — not run. API → queue → worker → storage timing was not measured; only saved detector and staging evidence is available.
- **Pageable-memory baseline** — not run. No unified/pageable-memory comparison against explicit staging was measured.

## Reproduction

```bash
# Regenerate the committed detection-quality evidence (inputs are small and committed).
mkdir -p /tmp/sentinel-eval
PYTHONPATH=src .venv/bin/python scripts/gen_dataset.py \
  --lines 2000 --seed 11 --burst-every 60 \
  --out /tmp/sentinel-eval/eval2000.log \
  --labels-out docs/evaluation/sample-labels.json
PYTHONPATH=src .venv/bin/python - <<'PY'
import hashlib, json
from pathlib import Path
from sentinel.detector import detect
from sentinel.detector.config import settings_from_file
raw = Path('/tmp/sentinel-eval/eval2000.log').read_bytes()
sha = hashlib.sha256(raw).hexdigest()
settings = settings_from_file('tests/fixtures/cpu/rules.json')
result = detect(raw, settings=settings, source_sha256=sha,
                run_id='run-eval-sample', chunk_id='chunk-eval-sample')
pairs = sorted({(m.rule_id, m.pattern) for m in result.matches})
payload = {
    'projection': ('alerts + distinct match (rule_id, pattern) pairs from '
                   'sentinel.detector.pipeline.detect(...).to_dict()'),
    'algorithm_version': result.algorithm_version,
    'source_sha256': sha,
    'run_id': 'run-eval-sample',
    'chunk_id': 'chunk-eval-sample',
    'rules': settings.to_dict(),
    'matches': [{'rule_id': rid, 'pattern': pattern} for rid, pattern in pairs],
    'alerts': [a.to_dict() for a in result.alerts],
    'metrics': result.metrics,
}
Path('docs/evaluation/sample-detections.json').write_text(
    json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
PY

# Rebuild both committed reports from the saved evidence.
PYTHONPATH=src .venv/bin/python -m sentinel.evaluation.cli evaluate \
  --labels docs/evaluation/sample-labels.json \
  --detections docs/evaluation/sample-detections.json \
  --out docs/evaluation/sample-evaluation.json
PYTHONPATH=src .venv/bin/python scripts/benchmark_report.py \
  --evaluation-report docs/evaluation/sample-evaluation.json \
  --out docs/evaluation/benchmark-report.md
```
