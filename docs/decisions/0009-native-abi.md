# ADR-0009: Versioned native C ABI with explicit timing and bounded staging

* Status: accepted (Sprint 5 / S5-A)
* Context: Sprint 2 shipped two swappable shared libraries with the same
  `sentinel_detect` symbol but no machine-checkable version, no documented
  allocation ownership, and an unbounded 64 MiB pinned staging buffer. Sprint 5
  needs randomized CPU/GPU equivalence tests and separate phase measurements
  without changing worker code at runtime.

## Decision

1. Freeze a C ABI (`include/sentinel/sentinel.h`) with `SENTINEL_ABI_VERSION`
   (v2), `sentinel_abi_version()` and `sentinel_build_id()`. Workers check the
   version before any other call. Both CMake and Make inject the short git hash.
2. Extend `sentinel_results` with `sentinel_timing` (peak device bytes plus
   allocation/H2D/kernel/D2H/total milliseconds). CPU builds zero it; GPU
   builds fill it. Any future layout change bumps the ABI version.
3. Use one stable error enum: invalid arguments, OOM, other CUDA errors and
   unsupported configurations. Device OOM maps to `SENTINEL_ERR_OOM` and is
   returned to the caller; there is no unbounded retry or silent CPU fallback.
4. Cap GPU staging at 16 MiB for the validated 16 GB profile. The
   `SENTINEL_GPU_STAGE_BYTES` override is clamped to `[1 MiB, 16 MiB]` and
   chunk boundaries remain line-aligned. Two pinned/device buffer pairs are
   alternated with CUDA events so a slot is never overwritten while its
   async copy is in flight.
5. Build with either Make (fast laptop workflow) or CMake
   (`SENTINEL_ENABLE_CUDA=ON|OFF`). CMake always builds the CPU-only target and
   optionally the CUDA library plus a minimal kernel smoke executable. Supported
   compiler is CUDA 12.4 with GCC 13 targeting `sm_86` unless overridden.

## Consequences

* Python and C++ tests use the same struct definitions; stale libraries fail
  fast with a clear ABI mismatch instead of silent memory corruption.
* Phase timings are explicitly enqueue/wall-clock measurements, not a claim
  about device-only execution time. VRAM is measured independently by
  `nvidia-smi` in the memory pipeline benchmark.
* The 16 MiB cap is below the 32–64 MiB buffers used during early prototyping;
  throughput may drop slightly, but host RAM and VRAM stay bounded on the
  laptop profile.
* The native K2 global-histogram contract remains distinct from the Sprint 4
  Python windowed scorer; `docs/algorithm-semantics.md` documents both.
