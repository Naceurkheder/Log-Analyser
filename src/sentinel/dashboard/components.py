"""Small HTML render helpers for the signal console."""

from __future__ import annotations

import html
from typing import Any, Iterable

import streamlit as st


def render_html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def section_header(title: str, description: str, tag: str | None = None) -> None:
    tag_markup = (
        f'<span class="mb-2 inline-block border border-steel px-2 py-0.5 '
        f'font-mono text-[11px] text-ink">{html.escape(tag)}</span>' if tag else ""
    )
    render_html(
        f"""
        <div class="signal-draw mb-8 border-b border-steel pb-5">
          {tag_markup}
          <h1 class="max-w-3xl text-3xl font-semibold leading-tight tracking-tight text-ink md:text-4xl">
            {html.escape(title)}
          </h1>
          <p class="mt-3 max-w-2xl text-[15px] leading-relaxed text-ink/80">
            {html.escape(description)}
          </p>
        </div>
        """
    )


def note(title: str, body: str, tone: str = "info") -> None:
    colors = {
        "info": "border-gpu bg-[#F4F8FF]",
        "warn": "border-cpu bg-[#FFF8EF]",
        "alert": "border-alert bg-[#FFF4F3]",
        "good": "border-moss bg-[#F1F8F4]",
    }
    render_html(
        f"""
        <div class="my-5 border-l-4 {colors.get(tone, colors['info'])} px-4 py-3">
          <p class="text-sm font-semibold text-ink">{html.escape(title)}</p>
          <p class="mt-1 text-sm leading-relaxed text-ink/80">{html.escape(body)}</p>
        </div>
        """
    )


def badge(label: str, tone: str = "neutral") -> str:
    tones = {
        "good": "border-moss text-moss",
        "warn": "border-cpu text-cpu",
        "alert": "border-alert text-alert",
        "info": "border-gpu text-gpu",
        "neutral": "border-steel text-ink/70",
    }
    return (
        f'<span class="inline-block border {tones.get(tone, tones["neutral"])} '
        f'px-2 py-0.5 font-mono text-[11px]">{html.escape(label)}</span>'
    )


def hero() -> None:
    render_html(
        """
        <div class="signal-draw relative overflow-hidden border border-steel bg-[#F7FAF9]">
          <div class="grid gap-8 px-6 py-7 lg:grid-cols-[1.15fr_0.85fr] lg:px-9">
            <div>
              <p class="font-mono text-[11px] tracking-signal text-ink/60">Sentinel-GPU · signal console</p>
              <h2 class="mt-3 max-w-xl text-3xl font-semibold leading-[1.08] tracking-tight text-ink md:text-[42px]">
                See what the pipeline actually does.
              </h2>
              <p class="mt-4 max-w-xl text-[15px] leading-relaxed text-ink/80">
                One dataset becomes immutable chunks. Two backends apply the same detector.
                A lease-protected worker commits one logical result. Every panel here separates
                shipped code, saved evidence, and planned work.
              </p>
              <div class="mt-6 flex flex-wrap gap-2">
                <span class="border border-cpu px-2 py-1 font-mono text-[11px] text-cpu">CPU · amber lane</span>
                <span class="border border-gpu px-2 py-1 font-mono text-[11px] text-gpu">GPU · blue lane</span>
                <span class="border border-steel px-2 py-1 font-mono text-[11px] text-ink/70">read-only dashboard</span>
              </div>
            </div>
            <div class="border-l-0 border-steel pl-0 lg:border-l lg:pl-8">
              <div class="grid grid-cols-2 gap-x-4 gap-y-5">
                <div>
                  <div class="h-px w-full bg-steel"></div>
                  <p class="mt-2 font-mono text-[11px] text-ink/60">Input</p>
                  <p class="text-sm font-medium text-ink">S3 object reference</p>
                </div>
                <div>
                  <div class="h-px w-full bg-steel"></div>
                  <p class="mt-2 font-mono text-[11px] text-ink/60">Dispatch</p>
                  <p class="text-sm font-medium text-ink">SQS + outbox</p>
                </div>
                <div>
                  <div class="h-px w-full bg-cpu"></div>
                  <p class="mt-2 font-mono text-[11px] text-cpu">CPU</p>
                  <p class="text-sm font-medium text-ink">Reference semantics</p>
                </div>
                <div>
                  <div class="h-px w-full bg-gpu"></div>
                  <p class="mt-2 font-mono text-[11px] text-gpu">GPU</p>
                  <p class="text-sm font-medium text-ink">K1 + K2 kernels</p>
                </div>
              </div>
              <div class="mt-6 border border-steel bg-paper p-3">
                <p class="font-mono text-[11px] text-ink/60">commit order</p>
                <p class="mt-1 text-sm leading-relaxed text-ink">
                  verify → compute → write attempt → fence → commit → acknowledge
                </p>
              </div>
            </div>
          </div>
          <div class="signal-track h-2 border-t border-steel"></div>
        </div>
        """
    )


def signal_path() -> None:
    render_html(
        """
        <div class="mt-5 grid gap-0 border border-steel md:grid-cols-5">
          <div class="border-b border-steel p-4 md:border-b-0 md:border-r">
            <p class="font-mono text-[11px] text-ink/60">01 · source</p>
            <p class="mt-2 text-sm font-semibold text-ink">incoming / raw-chunks</p>
            <p class="mt-1 text-xs leading-relaxed text-ink/70">Checksums make every chunk immutable.</p>
          </div>
          <div class="border-b border-steel p-4 md:border-b-0 md:border-r">
            <p class="font-mono text-[11px] text-ink/60">02 · producer</p>
            <p class="mt-2 text-sm font-semibold text-ink">Line-aligned chunks</p>
            <p class="mt-1 text-xs leading-relaxed text-ink/70">Stable job IDs survive replay.</p>
          </div>
          <div class="border-b border-steel p-4 md:border-b-0 md:border-r">
            <p class="font-mono text-[11px] text-ink/60">03 · queue</p>
            <p class="mt-2 text-sm font-semibold text-ink">jobs-cpu / jobs-gpu</p>
            <p class="mt-1 text-xs leading-relaxed text-ink/70">Capability routing keeps lanes separate.</p>
          </div>
          <div class="border-b border-steel p-4 md:border-b-0 md:border-r">
            <p class="font-mono text-[11px] text-ink/60">04 · worker</p>
            <p class="mt-2 text-sm font-semibold text-ink">Lease + fencing token</p>
            <p class="mt-1 text-xs leading-relaxed text-ink/70">Stale attempts cannot commit results.</p>
          </div>
          <div class="p-4">
            <p class="font-mono text-[11px] text-ink/60">05 · commit</p>
            <p class="mt-2 text-sm font-semibold text-ink">Results + alerts</p>
            <p class="mt-1 text-xs leading-relaxed text-ink/70">Durable output before SQS acknowledgement.</p>
          </div>
        </div>
        """
    )


def status_ledger(
    implemented: Iterable[str], evidence: Iterable[str], planned: Iterable[str]
) -> None:
    def column(title: str, items: Iterable[str], accent: str) -> str:
        rows = "".join(
            f'<li class="border-b border-steel/70 py-2 text-sm leading-snug text-ink/85">{html.escape(item)}</li>'
            for item in items
        )
        return f"""
          <div class="border-t-2 {accent} pt-3">
            <p class="text-sm font-semibold text-ink">{html.escape(title)}</p>
            <ul class="mt-2">{rows}</ul>
          </div>
        """

    render_html(
        '<div class="grid gap-7 md:grid-cols-3">'
        + column("Implemented", implemented, "border-moss")
        + column("Evidence", evidence, "border-gpu")
        + column("Planned / not run", planned, "border-cpu")
        + "</div>"
    )


def ruled_table(headers: Iterable[str], rows: Iterable[Iterable[Any]]) -> None:
    head = "".join(
        f'<th class="border-b border-steel px-3 py-2 text-left font-mono text-[11px] font-medium text-ink/60">{html.escape(str(h))}</th>'
        for h in headers
    )
    body_rows = []
    for row in rows:
        cells = "".join(
            f'<td class="border-b border-steel/60 px-3 py-2 align-top text-sm leading-relaxed text-ink/85">{cell}</td>'
            for cell in row
        )
        body_rows.append(f"<tr>{cells}</tr>")
    render_html(
        f"""
        <div class="overflow-x-auto border border-steel bg-[#F7FAF9]">
          <table class="w-full border-collapse">
            <thead><tr>{head}</tr></thead>
            <tbody>{''.join(body_rows)}</tbody>
          </table>
        </div>
        """
    )


def degraded(message: str, detail: str | None = None) -> None:
    render_html(
        f"""
        <div class="border border-alert bg-[#FFF4F3] px-4 py-3">
          <p class="text-sm font-semibold text-alert">Live API unavailable</p>
          <p class="mt-1 text-sm text-ink/85">{html.escape(message)}</p>
          {f'<p class="mt-1 font-mono text-xs text-ink/60">{html.escape(detail)}</p>' if detail else ''}
          <p class="mt-2 text-xs leading-relaxed text-ink/70">
            Saved evidence and architecture pages still work. Start the API with
            <span class="signal-kbd">make api</span>, then retry.
          </p>
        </div>
        """
    )
