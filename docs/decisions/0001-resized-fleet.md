# ADR-0001 — Resized fleet for a 16 GB laptop host

* Status: accepted
* Date: 2026-11-17
* Deciders: project owner, agent

## Context

The original sprint plan allocates 10 containers with 18 GiB of combined
memory caps (provision, infra, monitor, dashboard, builder, api-gateway,
producer, gpu-worker-1, gpu-worker-2, cpu-worker-3). The development host is a
laptop with 15 GiB of RAM, ~10 GiB available while idle, one RTX 3050 (4 GiB),
and is also used for coursework.

## Decision

Run a reduced topology of 4 containers with 4.6 GiB of combined caps:

| Container     | Memory cap | CPU allowance | Role                                        |
|---------------|------------|---------------|---------------------------------------------|
| infra         | 1536 MiB   | 50%           | moto (emulated AWS: S3, SQS, DynamoDB)      |
| gpu-worker-1  | 1536 MiB   | 300%          | CUDA worker                                 |
| cpu-worker-1  | 1024 MiB   | 200%          | CPU reference worker                        |
| dashboard     | 512 MiB    | 50%           | Streamlit presentation layer                |

Consequences:

* Terraform runs on the host instead of a dedicated `provision` container
  (host already has Terraform; a container adds nothing but overhead).
* The second GPU worker (`gpu-worker-2`) is dropped: one GPU time-slicing
  experiment can be rehearsed later by running two worker processes against
  the same device; two identical containers buy nothing for the grading
  goals and cost 2 GiB.
* `monitor`, `builder`, `api-gateway`, `producer` containers are deferred to
  the sprints that first need them, and will be added with the same
  idempotent fleet tooling (`lxd/fleet.sh`) when reached.
* Caps are enforced with `limits.memory` + `limits.cpu.allowance` and
  re-asserted on every `make up`.

## Update — 2026-11-22 (Sprint 3)

Sprint 3 reached the point where the control API and the ingest producer are
needed, so the two deferred containers were added with the same tooling:

| Container | Memory cap | CPU allowance | Role                              |
|-----------|------------|---------------|-----------------------------------|
| api       | 512 MiB    | 100%          | control API (10.20.0.11:8080)     |
| producer  | 512 MiB    | 100%          | streaming chunker / ingest reader |

Total caps are now 5.5 GiB across 6 containers, still inside the 16 GB host
budget. The GPU/CPU workers and dashboard are unchanged. See
`docs/decisions/0006-run-contract.md` for the API contract.
