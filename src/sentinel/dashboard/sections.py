"""Dashboard sections. Each page explains one part of the built platform."""

from __future__ import annotations

import hashlib
import html
from typing import Any

import streamlit as st

from ..detector.pipeline import detect
from ..detector.semantics import DetectorSettings
from . import evidence as ev
from .components import (
    badge,
    degraded,
    hero,
    note,
    ruled_table,
    section_header,
    signal_path,
    status_ledger,
)

SANDBOX_LOGS = """Jan  5 03:12:00 sshd[221]: Failed password for root from 10.0.0.8 port 22
Jan  5 03:12:10 nginx[118]: "GET /login HTTP/1.1" 404 12
Jan  5 03:13:00 sshd[221]: Failed password for root from 10.0.0.8 port 22
Jan  5 03:13:01 sshd[222]: Failed password for root from 10.0.0.8 port 22
Jan  5 03:13:02 sshd[223]: Failed password for root from 10.0.0.8 port 22
Jan  5 03:13:10 nginx[118]: "GET /login HTTP/1.1" 404 12
Jan  5 03:14:00 sshd[221]: Failed password for root from 10.0.0.8 port 22
Jan  5 03:14:01 sshd[224]: Failed password for admin from 10.0.0.8 port 22
Jan  5 03:14:02 sshd[225]: Failed password for guest from 10.0.0.8 port 22
Jan  5 03:14:03 sshd[226]: Failed password for test from 10.0.0.8 port 22
Jan  5 03:14:04 sshd[227]: Failed password for root from 10.0.0.8 port 22
Jan  5 03:14:05 sshd[228]: Failed password for root from 10.0.0.8 port 22
Jan  5 03:14:06 sshd[229]: Failed password for root from 10.0.0.8 port 22
Jan  5 03:14:07 sshd[230]: Failed password for root from 10.0.0.8 port 22
Jan  5 03:14:08 sshd[231]: Failed password for root from 10.0.0.8 port 22
Jan  5 03:14:09 sshd[232]: Failed password for root from 10.0.0.8 port 22
Jan  5 03:15:00 kernel: segfault at 0 in app-server[44]
"""


def _implemented_groups() -> tuple[list[str], list[str], list[str]]:
    rows = ev.capability_rows()
    implemented = [f"{r['sprint']} · {r['component']}" for r in rows if r["status"] == "Shipped"]
    evidence = [
        "Kernel parity: CPU 2,769.1 ms vs GPU 81.5 ms (saved evidence)",
        "S5 staging sweep: 2/4/8/16 MiB, VRAM peak 157 MiB",
        "S5 stream diagnostic: 4,352 MiB > 4,096 MiB VRAM, 272 chunks",
        "CPU oracle baselines with stable config hash",
    ]
    return implemented, evidence, list(ev.not_run_items())


def render_overview(_: dict[str, Any]) -> None:
    section_header(
        "Overview",
        "Sentinel-GPU turns one object reference into bounded jobs, runs a deterministic detector on CPU or CUDA, and commits one logical result per job.",
        "start here",
    )
    hero()
    st.markdown("")
    signal_path()
    st.markdown("")
    implemented, evidence, planned = _implemented_groups()
    status_ledger(implemented, evidence, planned)
    st.markdown("")
    note(
        "Read the status literally",
        "‘Shipped’ means the code and tests are present in this repository. Saved benchmark files are evidence for one measured environment; they are not a promise for every workload. ‘Not run’ items remain visible instead of being presented as results.",
        "info",
    )
    with st.expander("Click to see what each stage guarantees"):
        ruled_table(
            ["Stage", "Guarantee", "Failure behavior"],
            [
                ("Chunk", "Content-addressed key + SHA-256 manifest", "Checksum mismatch rejects the job"),
                ("Queue", "Capability-specific routing, at-least-once delivery", "Repeated failures reach the matching DLQ"),
                ("Worker", "Lease, heartbeat and fencing token", "Stale attempts cannot commit"),
                ("Commit", "Attempt output before logical result", "Acknowledge happens only after durable commit"),
            ],
        )


def _bar(label: str, value_ms: float, max_ms: float, color: str) -> str:
    width = max(2, min(100, int(value_ms / max_ms * 100))) if max_ms else 2
    return f"""
      <div class="mb-3">
        <div class="flex items-baseline justify-between">
          <span class="text-sm text-ink/80">{html.escape(label)}</span>
          <span class="signal-kbd text-xs text-ink/70">{value_ms:,.1f} ms</span>
        </div>
        <div class="mt-1 h-2 w-full bg-steel/30">
          <div class="h-2 {color}" style="width:{width}%"></div>
        </div>
      </div>
    """


def render_compare(data: dict[str, Any]) -> None:
    section_header(
        "CPU vs GPU",
        "Same configuration, same chunk boundaries, same logical output. The backends differ in how they compute it, not in what they mean.",
        "comparison",
    )
    ruled_table(
        ["Dimension", "CPU reference", "CUDA path"],
        [
            ("Role", "Deterministic oracle and correctness baseline", "Accelerated execution of the same semantics"),
            ("Pattern stage", "Byte-pattern scan in the reference pipeline", "K1 kernel marks matching lines"),
            ("Window stage", "Per-key counters and z-score over baseline windows", "K2/window kernels build the same feature cells under bounded staging"),
            ("Memory", "Streams chunks; no device allocation", "Bounded double-buffered staging; dataset larger than VRAM is streamed"),
            ("Strength", "Portable, debuggable, exact semantics", "High throughput when work is parallel enough"),
            ("Cost", "Serial Python/native path is slower on large chunks", "Launch, transfer and allocation overhead can dominate small inputs"),
            ("Failure policy", "Always available in a CPU-only lab", "A GPU-only job fails visibly; no silent CPU fallback"),
        ],
    )

    parity = data.get("parity") or {}
    cpu_ms = float(parity.get("cpu_ms") or 0)
    gpu_ms = float(parity.get("gpu_ms") or 0)
    st.markdown("")
    if cpu_ms and gpu_ms:
        st.markdown(
            '<p class="text-sm font-semibold text-ink">Kernel-scope timing (saved evidence)</p>',
            unsafe_allow_html=True,
        )
        st.markdown(
            _bar("CPU · native detect path", cpu_ms, cpu_ms, "bg-cpu")
            + _bar("GPU · native detect path", gpu_ms, cpu_ms, "bg-gpu"),
            unsafe_allow_html=True,
        )
        speedup = float(parity.get("speedup") or cpu_ms / gpu_ms)
        note(
            f"{speedup:.1f}x on this one workload, at this one scope",
            str(parity.get("scope_note") or "Kernel-scope benchmark only; not an end-to-end platform speedup claim."),
            "info",
        )
    else:
        note(
            "Kernel timing evidence is incomplete",
            "The dashboard only renders numbers present in committed JSON. Run the documented kernel benchmark and save its raw output before treating this panel as evidence.",
            "warn",
        )

    cuda = data.get("cuda") or {}
    sweep = cuda.get("micro_batch_sweep") or []
    show_staging = st.checkbox("Show CUDA staging phases", value=False)
    if sweep and show_staging:
        st.markdown("")
        st.markdown(
            '<p class="text-sm font-semibold text-ink">Staging sweep · memory pipeline diagnostic</p>',
            unsafe_allow_html=True,
        )
        ruled_table(
            ["Stage", "Kernel ms", "H2D ms", "D2H ms", "Peak VRAM", "MB/s"],
            [
                (
                    f"{row.get('stage_mib')} MiB",
                    ev.format_ms(row.get("phase_totals_ms", {}).get("kernel_ms")),
                    ev.format_ms(row.get("phase_totals_ms", {}).get("h2d_ms")),
                    ev.format_ms(row.get("phase_totals_ms", {}).get("d2h_ms")),
                    f"{row.get('vram', {}).get('peak_mib')} MiB",
                    str(row.get("throughput_mb_s")),
                )
                for row in sweep
            ],
        )
        note(
            "This table is not a speedup chart",
            "It records allocation, transfer and kernel phases for a bounded GPU staging pipeline. The larger stream deliberately exceeds physical VRAM; its purpose is to prove bounded memory behavior.",
            "warn",
        )
    elif sweep:
        st.caption("Click the checkbox above to expand the staging diagnostic.")


def render_detector(_: dict[str, Any]) -> None:
    section_header(
        "Detector mechanics",
        "Alerts are not raw pattern hits. A rule match only becomes a candidate event after timestamp parsing; a window baseline then decides whether the burst is anomalous.",
        "how it works",
    )
    ruled_table(
        ["Step", "What happens", "Why it matters"],
        [
            ("1 · Parse", "Bytes become line records with stable line IDs; oversize records are rejected, never silently truncated.", "Line identity survives chunk boundaries and replay."),
            ("2 · K1 match", "Each rule’s UTF-8 pattern is matched against the raw record. Matches carry rule_id, severity and line_id.", "Rule identity is part of the eventual alert identity."),
            ("3 · Events", "Valid timestamps are bucketed into window_id by window_seconds; missing/invalid timestamps are counted, not guessed.", "Out-of-order lines can be sorted before feature construction."),
            ("4 · K2 features", "Events are grouped by (service, ip, status, window, rule) into counts and line sets.", "This is the shared evaluation unit for CPU and GPU."),
            ("5 · Baseline", "For each series, the previous baseline_windows counts form mean and standard deviation; at least two history points are required.", "Cold-start windows do not produce confident alerts."),
            ("6 · Alert", "If std > 0 and (count − mean) / std >= z_threshold, an alert is emitted with deterministic alert_id.", "Duplicate processing converges on the same logical alerts."),
        ],
    )
    st.markdown("")
    st.markdown(
        """
        <div class="border border-steel bg-[#F7FAF9] p-5">
          <p class="font-mono text-[11px] text-ink/60">score rule</p>
          <p class="mt-2 text-lg font-medium text-ink">score = (count − mean(history)) / std(history)</p>
          <p class="mt-3 text-sm leading-relaxed text-ink/75">
            Defaults: 60-second windows, 10 baseline windows, z-threshold 3.0,
            65,536-byte maximum line. The API hashes the bounded detector config
            into every job ID, so changing a rule changes the logical job.
          </p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("")
    ruled_table(
        ["Concern", "Rule in this repository"],
        [
            ("Determinism", "docs/algorithm-semantics.md is normative; the CPU pipeline is the oracle for CUDA parity tests."),
            ("Alert identity", "Derived from run, chunk, ip, window, service, rule and status; stable across duplicate delivery."),
            ("Native ABI", "ABI v2 carries schema/ABI version, staging buffers and explicit error codes; ownership is documented in docs/native-abi.md."),
            ("Numerical tolerance", "Parity tests compare logical outputs; floating-point K2 statistics use the documented tolerance rather than exact bit equality."),
        ],
    )

    st.markdown("")
    st.markdown(
        '<p class="text-sm font-semibold text-ink">Detector sandbox · click to run</p>',
        unsafe_allow_html=True,
    )
    with st.form("detector_sandbox", clear_on_submit=False):
        logs = st.text_area("Log lines", value=SANDBOX_LOGS, height=190)
        patterns = st.text_input("Patterns (comma separated)", value="Failed password, segfault")
        c1, c2, c3 = st.columns(3)
        window_seconds = c1.number_input("Window seconds", min_value=1, max_value=3600, value=60)
        baseline_windows = c2.number_input("Baseline windows", min_value=1, max_value=20, value=2)
        z_threshold = c3.number_input("Z threshold", min_value=0.1, max_value=20.0, value=3.0, step=0.1)
        clicked = st.form_submit_button("Run detector")

    if clicked:
        data = logs.encode("utf-8")
        rule_patterns = [p.strip() for p in patterns.split(",") if p.strip()]
        if not rule_patterns:
            st.error("Enter at least one pattern.")
        else:
            settings = DetectorSettings.from_dict(
                {
                    "rules": [
                        {"rule_id": f"r{i}", "pattern": pattern, "severity": "high"}
                        for i, pattern in enumerate(rule_patterns)
                    ],
                    "window_seconds": int(window_seconds),
                    "baseline_windows": int(baseline_windows),
                    "z_threshold": float(z_threshold),
                    "max_line_bytes": 65536,
                }
            )
            try:
                result = detect(
                    data,
                    settings=settings,
                    source_sha256=hashlib.sha256(data).hexdigest(),
                    run_id="sandbox",
                    chunk_id="sandbox",
                )
            except Exception as exc:  # noqa: BLE001 - sandbox surfaces the error
                st.error(f"Detector rejected the input: {exc}")
            else:
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Lines", result.metrics.get("line_count", 0))
                m2.metric("Matches", result.metrics.get("matches", 0))
                m3.metric("Features", result.metrics.get("features", 0))
                m4.metric("Alerts", len(result.alerts))
                if result.alerts:
                    ruled_table(
                        ["Window", "Rule", "Count", "Mean", "Std", "Score"],
                        [
                            (
                                str(a.window_id),
                                html.escape(a.rule_id),
                                str(a.count),
                                f"{a.mean:.2f}",
                                f"{a.std:.2f}",
                                f"{a.score:.2f}",
                            )
                            for a in result.alerts[:50]
                        ],
                    )
                else:
                    st.caption(
                        "No alerts: the z-score threshold was not crossed. Try a lower "
                        "threshold or repeat a pattern across more windows."
                    )
                with st.expander("Show feature cells"):
                    ruled_table(
                        ["Window", "Key", "Rule", "Count"],
                        [
                            (
                                str(f.window_id),
                                ":".join(str(part) for part in f.key),
                                html.escape(f.rule_id),
                                str(f.count),
                            )
                            for f in result.features[:50]
                        ]
                        or [("—", "—", "—", "—")],
                    )
                st.caption("Runs locally in the dashboard process. No log text is sent to the API or to AWS.")


def render_topology(_: dict[str, Any]) -> None:
    section_header(
        "AWS-style topology",
        "Terraform models buckets, queues and tables; moto emulates the AWS plane inside the LXD fleet. The dashboard reads the API and never writes to infrastructure.",
        "infrastructure",
    )
    ruled_table(
        ["Resource", "Names", "Role"],
        [
            ("S3 buckets", "incoming · raw-chunks · artifacts · results", "Source objects, immutable chunks/manifests, pinned builds, attempt outputs"),
            ("SQS queues", "ingest-queue · jobs-cpu · jobs-gpu", "At-least-once work delivery; each has a matching DLQ"),
            ("DynamoDB", "SentinelRuns · SentinelJobs · SentinelAlerts", "Run contract, job state/fencing, logical alert projections"),
            ("Redrive", "maxReceiveCount = 3", "Repeated failures move to the queue-specific DLQ"),
        ],
    )
    st.markdown("")
    st.markdown(
        '<p class="text-sm font-semibold text-ink">Request path</p>',
        unsafe_allow_html=True,
    )
    signal_path()
    st.markdown("")
    ruled_table(
        ["Container", "Address", "Responsibility"],
        [
            ("infra", "10.20.0.10:4566", "moto AWS emulation"),
            ("api", "10.20.0.11:8080", "FastAPI control boundary and read APIs"),
            ("producer", "10.20.0.12", "Ingest consumer, chunking, manifests, jobs"),
            ("gpu-worker-1", "10.20.0.21", "GPU queue consumer with pinned native artifact"),
            ("cpu-worker-1", "10.20.0.22", "CPU queue consumer and reference execution"),
            ("dashboard", "10.20.0.23", "This read-only Streamlit surface"),
        ],
    )
    st.markdown("")
    resource = st.radio(
        "Click a resource to inspect its contract",
        ["Buckets", "Queues", "Tables"],
        horizontal=True,
        key="topology_resource",
    )
    details = {
        "Buckets": [
            ("incoming", "Source objects only; the API never streams log bytes."),
            ("raw-chunks", "Immutable chunks, manifests and producer progress checkpoints."),
            ("artifacts", "Pinned build libraries and manifests addressed by digest."),
            ("results", "Attempt-scoped worker output written before logical commit."),
        ],
        "Queues": [
            ("ingest-queue", "Run handoff to the producer; duplicate delivery is harmless."),
            ("jobs-cpu", "Only the CPU worker polls this queue."),
            ("jobs-gpu", "Only the GPU worker polls this queue."),
            ("*-dlq", "Poison messages retain last_error and object references."),
        ],
        "Tables": [
            ("SentinelRuns", "Run contract, progress and idempotency key."),
            ("SentinelJobs", "Job state, worker_id, attempt_count and fencing_token."),
            ("SentinelAlerts", "Deterministic logical alerts with run_id-index pagination."),
        ],
    }
    ruled_table([resource, "Contract"], details[resource])

    note(
        "What is distributed, and what is not",
        "Queues, containers and independent worker processes are real. They run on one physical host in this lab; there is no multi-host failover claim and only one physical GPU is available.",
        "warn",
    )


def _reset_pagination(run_id: str) -> None:
    for key in ("job_cursor", "alert_cursor"):
        if st.session_state.get(f"last_run_{key}") != run_id:
            st.session_state[key] = None
            st.session_state[f"last_run_{key}"] = run_id


def render_runs(client: Any) -> None:
    section_header(
        "Runs, jobs and alerts",
        "Live state comes from GET /v1/runs, /jobs and /alerts. Tables stay bounded; filters apply to the current page only.",
        "live inspection",
    )
    health = client.health()
    if not health.ok:
        degraded(
            health.error or "The API did not answer.",
            f"Expected base URL: {client.base_url}",
        )
        return

    run_id = st.text_input(
        "Run ID", value=st.session_state.get("run_id", ""), placeholder="run_..."
    )
    st.session_state["run_id"] = run_id
    if not run_id:
        note(
            "Enter a run ID to inspect it",
            "Create a run with POST /v1/runs or the operator CLI, then paste the returned run_id here. The dashboard does not list or load raw datasets.",
            "info",
        )
        return

    _reset_pagination(run_id)
    run_response = client.get_run(run_id)
    if not run_response.ok:
        degraded(run_response.error or "Run lookup failed.", client.base_url)
        return

    run = run_response.data or {}
    progress = run.get("progress") or {}
    st.markdown(
        f"""
        <div class="border border-steel bg-[#F7FAF9] p-4">
          <div class="flex flex-wrap items-center gap-2">
            <span class="signal-kbd text-xs text-ink/60">{html.escape(str(run.get('run_id', '')))}</span>
            {badge(str(run.get('status', 'unknown')), 'good' if run.get('status') == 'SUCCEEDED' else 'info')}
            {badge('policy: ' + str(run.get('backend_policy', 'unknown')), 'neutral')}
          </div>
          <p class="mt-3 text-sm leading-relaxed text-ink/80">
            Dataset <span class="signal-kbd">{html.escape(str((run.get('dataset') or {}).get('key', '—')))}</span>
            · config <span class="signal-kbd">{html.escape(str(run.get('detector_config_hash', '—'))[:16])}</span>
            · updated {html.escape(str(run.get('updated_at', '—')))}
          </p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Jobs total", progress.get("jobs_total", 0))
    c2.metric("Completed", progress.get("jobs_completed", 0))
    c3.metric("Failed", progress.get("jobs_failed", 0))
    c4.metric("Alerts", progress.get("alerts_total", 0))

    st.markdown("")
    st.markdown('<p class="text-sm font-semibold text-ink">Jobs · current page</p>', unsafe_allow_html=True)
    jobs = client.list_jobs(run_id, limit=50, cursor=st.session_state.get("job_cursor"))
    if not jobs.ok:
        degraded(jobs.error or "Job page failed.")
    else:
        items = (jobs.data or {}).get("items") or []
        statuses = sorted({str(item.get("status", "unknown")) for item in items})
        chosen = st.selectbox("Status filter (this page)", ["all", *statuses], key="job_status_filter")
        shown = items if chosen == "all" else [i for i in items if i.get("status") == chosen]
        ruled_table(
            ["Job", "Status", "Backend", "Artifact", "Attempts"],
            [
                (
                    f"<span class='signal-kbd text-xs'>{html.escape(str(i.get('job_id',''))[:18])}…</span>",
                    html.escape(str(i.get("status", "—"))),
                    html.escape(str(i.get("backend") or i.get("actual_backend") or "—")),
                    html.escape(str(i.get("artifact_id") or i.get("artifact_sha256") or "—")[:16]),
                    str(i.get("attempts", 0)),
                )
                for i in shown
            ]
            or [("—", "No jobs on this page", "—", "—", "—")],
        )
        if st.button("Next jobs page") and (jobs.data or {}).get("next_cursor"):
            st.session_state["job_cursor"] = (jobs.data or {}).get("next_cursor")
            st.rerun()

    st.markdown("")
    st.markdown('<p class="text-sm font-semibold text-ink">Alerts · bounded sample</p>', unsafe_allow_html=True)
    alerts = client.list_alerts(run_id, limit=50, cursor=st.session_state.get("alert_cursor"))
    if not alerts.ok:
        degraded(alerts.error or "Alert page failed.")
    else:
        items = (alerts.data or {}).get("items") or []
        rules = sorted({str(i.get("rule_id")) for i in items if i.get("rule_id")})
        severities = sorted({str(i.get("severity")) for i in items if i.get("severity")})
        f1, f2 = st.columns(2)
        rule = f1.selectbox("Rule (this page)", ["all", *rules], key="alert_rule_filter")
        severity = f2.selectbox("Severity (this page)", ["all", *severities], key="alert_severity_filter")
        shown = [
            i for i in items
            if (rule == "all" or i.get("rule_id") == rule)
            and (severity == "all" or i.get("severity") == severity)
        ]
        ruled_table(
            ["Window", "Rule", "Severity", "Score", "Line"],
            [
                (
                    str(i.get("window_id", "—")),
                    html.escape(str(i.get("rule_id") or "—")),
                    html.escape(str(i.get("severity") or "—")),
                    str(i.get("score", "—")),
                    html.escape(str(i.get("line_id_first") or i.get("line_id") or "—")[:18]),
                )
                for i in shown
            ]
            or [("—", "No alerts on this page", "—", "—", "—")],
        )
        if st.button("Next alerts page") and (alerts.data or {}).get("next_cursor"):
            st.session_state["alert_cursor"] = (alerts.data or {}).get("next_cursor")
            st.rerun()


def render_benchmarks(data: dict[str, Any]) -> None:
    section_header(
        "Benchmarks and evidence",
        "Numbers here are read from committed files. The raw path is shown beside every table so a claim can be checked rather than trusted.",
        "evidence",
    )
    paths = data.get("paths") or {}
    ruled_table(
        ["Evidence file", "State", "What it supports"],
        [
            (paths.get("cpu", "—"), "Committed", "CPU oracle and threaded probe with stable config hash"),
            (paths.get("parity", "—"), "Committed", "Transcribed S5 kernel-scope CPU/GPU timing and parity result"),
            (paths.get("cuda", "—"), "Committed", "GPU staging, VRAM and >VRAM streaming diagnostics"),
            (paths.get("evaluation", "—"), "Generated" if data.get("evaluation") else "Not present", "Detection-quality metrics from the S8 harness"),
        ],
    )

    view = st.radio(
        "Click an evidence view",
        ["Kernel parity", "CPU baselines", "CUDA staging", "Detection quality"],
        horizontal=True,
        key="benchmark_view",
    )
    st.markdown("")

    if view == "Kernel parity":
        parity = data.get("parity") or {}
        cpu_ms = float(parity.get("timings_ms", {}).get("cpu") or 0)
        gpu_ms = float(parity.get("timings_ms", {}).get("gpu") or 0)
        if cpu_ms and gpu_ms:
            st.markdown(
                _bar("CPU", cpu_ms, cpu_ms, "bg-cpu")
                + _bar("GPU", gpu_ms, cpu_ms, "bg-gpu"),
                unsafe_allow_html=True,
            )
            ruled_table(
                ["Field", "Value"],
                [
                    ("Scope", html.escape(str(parity.get("scope", "—")))),
                    ("Speedup", f"{parity.get('timings_ms', {}).get('speedup', '—')}x"),
                    ("Source", html.escape(str(parity.get("source", "—")))),
                ],
            )
        else:
            st.caption("No kernel parity JSON is committed.")

    elif view == "CPU baselines":
        rows = (data.get("cpu") or {}).get("rows") or []
        ruled_table(
            ["Mode", "Mean ms", "Throughput MB/s", "Repeats", "Alerts"],
            [
                (
                    html.escape(str(r.get("mode", "—"))),
                    ev.format_ms(r.get("mean_ms")),
                    str(r.get("throughput_mb_s", "—")),
                    str(r.get("repeats", "—")),
                    str(r.get("alerts", "—")),
                )
                for r in rows
            ]
            or [("—", "—", "—", "—", "—")],
        )

    elif view == "CUDA staging":
        cuda = data.get("cuda") or {}
        large = cuda.get("large_stream") or {}
        ruled_table(
            ["Measurement", "Value"],
            [
                ("Stream size", f"{large.get('input_mib', '—')} MiB"),
                ("Chunks", str(large.get("chunks", "—"))),
                ("Physical VRAM", f"{(cuda.get('gpu') or {}).get('total_vram_mib', '—')} MiB"),
                ("Peak sampled VRAM", f"{(large.get('vram') or {}).get('peak_mib', '—')} MiB"),
                ("OOM retries", str(cuda.get("oom_retries", "—"))),
            ],
        )
        st.caption("Staging and VRAM evidence only. Not a CPU/GPU speedup result.")

    else:
        evaluation = data.get("evaluation")
        if evaluation:
            st.json(evaluation)
        else:
            note(
                "Evaluation report not yet present",
                "The S8 harness writes docs/evaluation/sample-evaluation.json. This page will render it when available; no quality numbers are invented here.",
                "warn",
            )

    st.markdown("")
    st.markdown('<p class="text-sm font-semibold text-ink">Not run · do not infer</p>', unsafe_allow_html=True)
    ruled_table(["Limitation"], [(item,) for item in ev.not_run_items()])


def render_roadmap(data: dict[str, Any]) -> None:
    section_header(
        "Status and roadmap",
        "The final project is honest about scope: code presence, saved evidence and unrun experiments are three different things.",
        "release view",
    )
    rows = ev.capability_rows()
    ruled_table(
        ["Sprint", "Component", "Status", "Repository check"],
        [
            (
                r["sprint"],
                html.escape(r["component"]),
                badge(r["status"], "good" if r["status"] == "Shipped" else "warn"),
                f"<span class='signal-kbd text-xs'>{html.escape(r['check'])}</span>",
            )
            for r in rows
        ],
    )
    st.markdown("")
    implemented, evidence, planned = _implemented_groups()
    status_ledger(implemented, evidence, planned)
    st.markdown("")
    note(
        "Release gate",
        "The v1.0 tag is applied only after the CPU suite, native parity, CUDA smoke tests and documented smoke path pass on the target host. This dashboard does not change that gate.",
        "info",
    )
