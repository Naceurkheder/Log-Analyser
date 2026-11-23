# ADR-0008 — Synthetic ground truth as a separate labeled manifest

* Status: accepted
* Date: 2026-11-23
* Deciders: project owner, agent

## Context

Sprint 3-C needs controlled attack-like patterns so precision/recall/F1 can be
computed later (Sprint 6). The dataset generator already emits failed-login,
404 and error-code bursts from a fixed seed, but the injected records were
implicit: nothing recorded their positions or the evaluation window they
belong to. Embedding labels in the dataset would let the detector read the
answer; keeping them only in prose would make evaluation non-reproducible.

The chunker (`ADR-0007`) assigns stable line IDs of the form
`L + sha256(source_sha256|line_no)[:16]` and chunk manifests record byte/line
ranges. Label coordinates must therefore be expressible in that same space.

## Decision

* The generator writes a **derived dataset** at a new path; the original input
  is never edited. `--labels-out PATH` emits a separate JSON manifest:
  `sentinel.ground-truth`, schema_version 1, `synthetic: true`, generator
  version, seed, source name/hash/size/line count, parser version, evaluation
  window length, and a list of scenarios.
* Evaluation unit is the **window** (`window_seconds`, default 60). Each
  scenario also carries an inclusive 1-based `line_range`, its timestamp and
  window ID, the rule pattern that should fire, the aggregation key
  (service/IP/status where applicable), and the expected label. This supports
  both window-level scoring and record-level auditing.
* Scenario kinds: `failed_login_burst`, `invalid_user_burst`,
  `http_404_burst`, `error_code_burst`, and `event_rate_burst` (a single
  key/service generating normal-looking 200 responses at high rate).
* The manifest contains no wall-clock generation time and no absolute paths,
  so identical seed/parameters/output basename produce byte-identical labels.
* Line IDs are **not** pre-expanded in the manifest. `src/sentinel/labels.py`
  resolves `line_range` against the source SHA using the chunker's formula.
  `labels_for_chunk(manifest, chunk)` clips a scenario to a chunk's line range.
* Label manifests are stored outside the object references given to workers.
  Ingest messages, run records and detector configuration contain **no**
  label reference; the detector cannot read ground truth during execution.

## Consequences

* Evaluation can join outputs to labels by `(window_id, key)` and audit exact
  lines via stable line IDs, including when a scenario spans several chunks.
* Tests verify byte-identical regeneration, scenario coverage, isolation from
  detector input, and alignment after bounded chunking.
* Results obtained on this corpus must always be reported as **synthetic**;
  they do not establish real-world detection quality (ADR-0004).
