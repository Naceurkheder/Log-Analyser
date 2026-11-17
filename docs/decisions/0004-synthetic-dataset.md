# ADR-0004 — Synthetic dataset generator instead of LogHub downloads

* Status: accepted
* Date: 2026-11-17
* Deciders: project owner, agent

## Context

The sprint plan references the LogHub dataset collection for input data and
benchmarks. Downloading the full LogHub archives means hundreds of megabytes
of external traffic, no control over anomaly density, and non-trivial license
and reproducibility questions. The project owner explicitly does not want
bulk dataset downloads for this academic build.

## Decision

`scripts/gen_dataset.py` generates the entire corpus locally:

* syslog-shaped mixed traffic across sshd / nginx / app-server / kernel,
* deterministic injected bursts (failed logins, invalid-user scans, 404
  floods from a single IP, error-code spikes, rare SEGFAULT lines),
* fixed RNG seed — byte-identical output for identical parameters, so the
  same generation call doubles as labeled ground truth (Sprint 6),
* streaming writer — bounded memory for any `--size-mb`.

## Consequences

* Speedup and precision numbers measure the pipeline, not download luck;
  reruns are comparable bit-for-bit.
* The 100 MB benchmark file lives in `data/` (gitignored) and is rebuilt with
  `make dataset` in seconds.
* If a real-world realism check is ever required, a single small LogHub
  sample can be added as an opt-in fixture without changing the pipeline.
