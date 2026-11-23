# Native detector ABI (v2)

Status: accepted (Sprint 5 / S5-A). Related: ADR-0009,
`include/sentinel/sentinel.h`, `docs/algorithm-semantics.md` §§5–7.

## 1. Build matrix and toolchain

| Artifact | Sources | Build command | Toolkit |
|---|---|---|---|
| `libsentinel_cpu.so` | tokenizer, ABI identity, CPU detector, CPU backend | `make lib-cpu` or `cmake -DSENTINEL_ENABLE_CUDA=OFF` | host C++17 compiler only |
| `libsentinel.so` | tokenizer, ABI identity, CPU detector, CUDA detector, GPU backend | `make lib` or `cmake -DSENTINEL_ENABLE_CUDA=ON` | CUDA 12.4, host GCC 13, `sm_86` by default |

The validated development host is CUDA 12.4 (nvcc 12.4.131), GCC 13.5,
driver 550.163.01 and an sm_86 GPU. `CMakeLists.txt` overrides the CUDA
host compiler because CUDA 12.4's math headers clash with glibc ≥ 2.41
(`cospi`/`sinpi`/`rsqrt` exception specifications). For another GPU pass
`-DCMAKE_CUDA_ARCHITECTURES=<arch>`; CPU-only builds never require CUDA.

`cmake -S . -B build/cmake-cpu -DSENTINEL_ENABLE_CUDA=OFF` is checked by
`make check-cmake-cpu`. `make test-cuda-smoke` builds the CUDA CMake target
and runs the minimal kernel/ABI executable `tests/cuda/smoke.cu`.

## 2. Versioning and identity

```c
#define SENTINEL_ABI_VERSION 2u
uint32_t sentinel_abi_version(void);
const char* sentinel_build_id(void);
```

Callers must check `sentinel_abi_version()` before using any other symbol.
The build identifier is the short git hash injected by both build systems
(`dev` for source snapshots without git). It is diagnostic only; do not parse
it. ABI v2 adds the `sentinel_timing` block and the two identity functions.
Any struct layout, enum value or semantic change requires a version bump.

## 3. Error codes

| Value | Symbol | Meaning |
|---:|---|---|
| 0 | `SENTINEL_OK` | success |
| -1 | `SENTINEL_ERR_INVALID` | null pointer, invalid count, pattern too long, unsupported shape |
| -2 | `SENTINEL_ERR_OOM` | host or device allocation failed (including `cudaErrorMemoryAllocation`) |
| -3 | `SENTINEL_ERR_CUDA` | any other CUDA runtime/launch error |
| -4 | `SENTINEL_ERR_UNSUPPORTED` | configuration outside the supported ABI envelope |

Every entry point returns one of these; CUDA failures are never silently
ignored. All kernel launches call `cudaGetLastError()` immediately after the
launch, before any subsequent synchronizing call.

## 4. Struct layouts

`sentinel_config` holds up to 32 patterns of at most 63 bytes and a `float`
K2 z-score threshold. `sentinel_input` carries the raw bytes, `N` line
offsets (`N+1` entries), and per-line IP (host-order `uint32_t`), minute
bucket (`uint16_t`) and service id (`uint8_t`). Offsets are produced by
`sentinel_tokenize`, which owns malloc'd arrays released with `free()`.

`sentinel_results` contains:

* `line_flags` — `N` `uint32_t` bitmasks; bit *i* means pattern *i* matched.
* `anomaly_count` / `anomaly_records` — `count * 4` words in
  `(ip, bucket, service, count)` order, sorted by `(bucket, service, ip)`.
* `cells`, `mean`, `std` — K2 baseline statistics over non-empty cells.
* `timing` — see §6.

`anomaly_records` and `line_flags` are malloc'd by the library and released
with `sentinel_free_results()`. The function is safe on zeroed results and
resets every field, including timing. The CPU and GPU libraries produce
byte-identical `line_flags` and `anomaly_records` for the same input; the
windowed Python scorer from Sprint 4 has separate documented semantics
(`docs/algorithm-semantics.md` §§5–7) and is **not** the native K2 contract.

## 5. Bounded staging and ownership

The GPU path stages raw bytes through one pinned host buffer and one device
buffer. The default and hard cap for the 16 GB host profile are both 16 MiB;
`SENTINEL_GPU_STAGE_BYTES` is clamped to `[1 MiB, 16 MiB]`. A line is never
split across staging chunks: the next chunk starts at the first line boundary
after the byte budget is reached. The worker streams immutable chunks from
S3; neither the host nor the device holds the whole dataset at once.
Peak device allocation (reported in `timing.peak_device_bytes`) is the sum of
the staging buffers, per-line arrays, the `2^21`-slot K2 hash table and the
worst-case anomaly output array. OOM is a bounded error (`-2`), never a
retry loop or a silent fallback.

## 6. Timing semantics

```c
typedef struct {
    uint64_t peak_device_bytes;
    double alloc_ms, h2d_ms, kernel_ms, d2h_ms, total_ms;
} sentinel_timing;
```

`alloc_ms`, `h2d_ms`, `kernel_ms` and `d2h_ms` are CPU-side wall-clock
measurements of the corresponding **enqueue/allocation** phases within one
call; `h2d_ms` covers bulk chunk copies, not the small metadata copies.
`total_ms` includes the final stream synchronization and host-side sorting,
so it is always the authoritative end-to-end number. These phase numbers are
for bottleneck diagnosis, not for claiming device-only throughput. The CPU
backend leaves all six fields at zero. `scripts/cuda_memory_pipeline.py`
samples VRAM externally with `nvidia-smi` and records both.
