# 0013 — Streamlit signal console design

Status: Accepted

## Context

Sprint 7 requires a Streamlit dashboard that explains CPU/GPU comparison, detector mechanics, AWS resources and repository status. The brief explicitly asks for Tailwind and for a highly descriptive platform. A second constraint is honesty: the dashboard must not show unimplemented sprints as live.

## Token system (first design pass)

- **Palette**: `ink #10212B`, `paper #E9F0EE`, `steel #AFC3C4`, `cpu #E08A3C`, `gpu #2F80ED`, `alert #D64550`.
- **Type**: IBM Plex Sans for headings and prose; IBM Plex Mono for IDs, timings and chips. Two clearly distinct roles, no display/body serif pair.
- **Layout**: fixed sidebar rail plus ruled main column. Hero is a source → producer → queue → worker → commit lane schematic, followed by an asymmetric explanation/status ledger. Content is left aligned; tables use horizontal rules rather than uniform cards.
- **Principles**: every claim carries provenance; color encodes backend or severity only; sequence markers appear only where the pipeline really is a sequence; one memorable hero, quiet everything else.

ASCII concept:

```text
┌ rail ┐┌──────────────────────────────────────────────┐
│ Over ││ SIGNAL PATH  source → queue → worker → commit │
│ Comp ││  CPU ───────╲                                  │
│ Mech ││  GPU ───────╱  artifacts / fences / alerts    │
│ Topo │├──────────────────────────┬─────────────────────┤
│ Bench││ why two backends         │ shipped/evidence/   │
│ Run  ││ detector mechanics       │ not-run ledger      │
└──────┴──────────────────────────┴─────────────────────┘
```

## Critique and revision (second pass)

A similar prompt would ordinarily produce a near-black dashboard with one acid accent, a KPI-card grid, all-caps eyebrows and middle-dot meta strings. Each was rejected:

| Generic default | Revision | Reason |
|---|---|---|
| Near-black + acid green | Cool paper/ink with amber CPU and blue GPU lanes | Two real subsystems need two encodings; this is an instrument panel, not a terminal theme |
| Identical rounded cards | Ruled strips and tables | Structure should encode pipeline order and hierarchy |
| ALL-CAPS eyebrow above every heading | Sentence-case section tags used sparingly | Avoids template chrome |
| Big-number hero | Lane schematic with a small commit-order strip | The subject is a flow, not a single KPI |
| Accent one headline word | Plain headline, color reserved for lanes | Avoids a common generated-page tell |
| Arrow appended to links | Arrows only inside the flow diagram | Arrow is information there, decoration elsewhere |

`prefers-reduced-motion` disables the one entrance animation. Keyboard focus is visible through the shared focus rule. Text status is provided alongside color.

Interactions are native Streamlit controls rather than decorative JavaScript: clickable section buttons, a detector sandbox form that calls the CPU oracle locally, evidence-view radios, a staging checkbox, topology resource radios, and bounded pagination/filters on live pages. The sandbox runs in-process and never sends log text to the API or AWS.

## Consequences

The dashboard is intentionally read-only. Live data comes from `/healthz`, `/v1/runs/{id}`, `/v1/runs/{id}/jobs` and `/v1/runs/{id}/alerts`, with limit ≤ 200 and cursor pagination. Saved evidence is loaded from committed JSON. Capability status is derived from repository paths, so a missing module cannot be presented as shipped.
