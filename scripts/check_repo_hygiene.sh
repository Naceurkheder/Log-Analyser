#!/usr/bin/env bash
# Fail CI when generated or private files are accidentally tracked.
# Only `git ls-files` is inspected: ignored local build/dataset/result files are
# intentionally allowed to exist in a developer's working tree.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

failures=0
report() {
  printf 'hygiene: %s\n' "$1" >&2
  failures=$((failures + 1))
}

source_exts='.(py|sh|bash|cpp|cc|c|cu|cuh|h|hpp|txt|md|json|yml|yaml)'

while IFS= read -r -d '' file; do
  base="${file##*/}"

  case "$file" in
    build/*|*/build/*|dist/*|*/dist/*)
      report "tracked build output: $file"
      continue
      ;;
  esac

  case "$file" in
    .env|.env.*)
      if [[ "$file" != ".env.example" && "$file" != *".env.example" ]]; then
        report "tracked environment file: $file"
      fi
      ;;
  esac

  case "$file" in
    *.tfstate|*.tfstate.*|*.tfplan|*.tfplan.json|*.tfvars|*.tfvars.json|*.backup)
      report "tracked Terraform state/plan: $file"
      ;;
  esac

  case "$file" in
    .local/*)
      report "tracked local lifecycle state: $file"
      ;;
  esac

  case "$file" in
    data/*|results/*)
      if [[ "$file" != "*/.gitkeep" && "$base" != ".gitkeep" ]]; then
        report "tracked generated data/result file: $file"
      fi
      ;;
  esac

  case "$file" in
    *.so|*.so.*|*.o|*.a|*.dylib|*.dll|*.exe)
      report "tracked compiled binary: $file"
      ;;
  esac

  # Source files such as bench/bench_kernels.cpp are allowed; built bench_*
  # executables (bench_cpu, bench_gpu, ...) are not.
  if [[ "$base" == bench_* ]] && [[ ! "$base" =~ $source_exts$ ]]; then
    report "tracked benchmark binary: $file"
  fi

  lower="${base,,}"
  if [[ "$file" != "SENTINEL-GPU-PERSONAL-EXECUTION-PLAN.md" ]]; then
    if [[ "$lower" == *private*plan* || "$lower" == *plan*private* ||
          "$lower" == *personal*plan* || "$lower" == *plan*personal* ||
          "$lower" == my_plan* || "$lower" == plan_private* ]]; then
      report "tracked private plan file: $file"
    fi
  fi
done < <(git ls-files -z)

if ((failures > 0)); then
  printf 'repo hygiene: %d violation(s)\n' "$failures" >&2
  exit 1
fi

printf 'repo hygiene: clean\n'
