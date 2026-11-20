#!/usr/bin/env bash
#
# scripts/run_bench.sh — Sprint 2 kernel benchmark orchestration.
#
# Runs the CPU reference and the CUDA implementation over the same chunk of
# synthetic data and prints the measured speedup. The dataset is generated
# (fixed seed) if absent — the benchmark never downloads anything.

set -euo pipefail

cd "$(dirname "$0")/.."

SIZE_MB="${BENCH_SIZE_MB:-100}"
REPEATS="${BENCH_REPEATS:-3}"
DATA="${BENCH_DATA:-data/bench.log}"

if [[ ! -f "$DATA" ]]; then
    echo ">>> generating ${SIZE_MB}MB benchmark dataset (fixed seed)"
    python3 scripts/gen_dataset.py --size-mb "$SIZE_MB" --out "$DATA"
fi

echo ">>> building harness"
make lib lib-cpu bench-cpu bench-gpu >/dev/null

echo ">>> benchmarking 100MB chunk through both paths ($REPEATS reps, best time)"
CPU_JSON="$(./build/bench_cpu "$DATA" "$REPEATS")"
GPU_JSON="$(./build/bench_gpu "$DATA" "$REPEATS")"

echo "  cpu: $CPU_JSON"
echo "  gpu: $GPU_JSON"

python3 - "$CPU_JSON" "$GPU_JSON" <<'EOF'
import json, sys
cpu = json.loads(sys.argv[1]); gpu = json.loads(sys.argv[2])
sp = cpu["best_ms"] / gpu["best_ms"]
print(f"\n  CPU: {cpu['best_ms']:8.1f} ms  ({cpu['mb_per_s']:7.1f} MB/s)")
print(f"  GPU: {gpu['best_ms']:8.1f} ms  ({gpu['mb_per_s']:7.1f} MB/s)")
print(f"  speedup: {sp:.1f}x   (parity inputs: lines={cpu['lines']}, "
      f"hits cpu={cpu['pattern_hits']} gpu={gpu['pattern_hits']}, "
      f"anomalies cpu={cpu['anomalies']} gpu={gpu['anomalies']})")
if cpu["pattern_hits"] != gpu["pattern_hits"] or cpu["anomalies"] != gpu["anomalies"]:
    print("  WARNING: result counts differ between backends!", file=sys.stderr)
    sys.exit(1)
if sp < 10:
    print("  WARNING: speedup below the 10x target", file=sys.stderr)
    sys.exit(1)
EOF
