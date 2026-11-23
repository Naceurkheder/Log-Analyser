# Sentinel-GPU signal console

The dashboard is a **read-only presentation layer**. It explains the system and inspects live state through the FastAPI read endpoints. It never writes to DynamoDB, SQS or S3, and it never loads raw datasets.

## Run it

```bash
make dashboard          # http://127.0.0.1:8501, loopback only
make dashboard-css      # rebuild the committed Tailwind bundle after UI edits
make test-dashboard     # unit tests for evidence loaders and the API client
```

If the API is not running, live pages show a degraded-state message while architecture and saved-evidence pages keep working. Start it with `make api`.

## Pages

| Page | What it answers |
|---|---|
| Overview | What the platform does, and which parts are shipped, evidenced or unrun |
| CPU vs GPU | How the two backends differ, plus kernel-scope timing and staging diagnostics from saved files |
| Detector mechanics | How K1 matches, timestamps, K2 windows, baselines and deterministic alert IDs work |
| AWS topology | Buckets, queues, tables, redrive policy and the LXD container map |
| Runs & alerts | Live run progress, bounded job pages and bounded alert samples from the API |
| Benchmarks | Clickable evidence views (kernel parity, CPU baselines, CUDA staging, detection quality) from committed JSON, with raw paths and an explicit `Not run` list |
| Status & roadmap | Code presence by sprint and the release gate |

## Click interactions

- Section buttons across the top (and the sidebar radio) switch pages without a URL reload.
- **Detector mechanics** has a real sandbox: edit log lines, patterns, window size, baseline length and z-threshold, then click **Run detector**. It executes the same CPU oracle used by the tests and shows matches, features and alerts. The sample input produces one alert at the default settings.
- **CPU vs GPU** has a checkbox that expands the staging-phase table.
- **AWS topology** has resource buttons that swap the displayed bucket, queue or table contracts.
- **Benchmarks** has four evidence views that switch the rendered table while keeping the raw file paths visible.
- **Runs & alerts** has bounded pagination and page-level status/rule/severity filters.

## Design decisions

The visual concept is a **signal console**, not a generic card dashboard. A cool paper/ink base keeps dense technical text readable. Amber marks the CPU lane and blue marks the GPU lane; color is never the only status signal. Ruled sections and sequence numbers encode real pipeline stages. The only entrance motion is a single page-load draw and it is disabled under `prefers-reduced-motion`.

Typography uses IBM Plex Sans for prose and IBM Plex Mono for identifiers, timings and status chips. There are no tracked-out all-caps eyebrows, gradient washes or decoration-only shadows.

Compiled Tailwind lives at `src/sentinel/dashboard/static/tailwind.css`. Source CSS is `tailwind.input.css`; the config scans the dashboard’s Python files for utility classes. The bundle is committed so running the dashboard needs no Node toolchain.

## Evidence policy

- **Shipped** means the code and tests are in the repository.
- **Evidence** means a committed file contains the measured value.
- **Planned / not run** stays visible; no unrun experiment is rendered as a result.

`docs/benchmarks/kernel-parity.json` explicitly records that its CPU/GPU timing was transcribed from S5 verification output, not generated as a raw machine-readable log. `cuda-memory-pipeline.json` is a staging/VRAM diagnostic and is never presented as a speedup result.
