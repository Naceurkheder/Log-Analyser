"""Streamlit entry point for the Sentinel-GPU signal console."""

from __future__ import annotations

import os

import streamlit as st

from . import sections
from .api_client import SentinelApiClient
from . import evidence as ev
from .theme import inject_theme

SECTIONS = {
    "Overview": sections.render_overview,
    "CPU vs GPU": sections.render_compare,
    "Detector mechanics": sections.render_detector,
    "AWS topology": sections.render_topology,
    "Runs & alerts": sections.render_runs,
    "Benchmarks": sections.render_benchmarks,
    "Status & roadmap": sections.render_roadmap,
}


def _click_navigation(current: str) -> str:
    """Render clickable section buttons and return the selected section."""
    st.markdown(
        '<p class="mb-2 font-mono text-[11px] text-ink/50">Click a section</p>',
        unsafe_allow_html=True,
    )
    selected = current
    columns = st.columns(len(SECTIONS), gap="small")
    for index, name in enumerate(SECTIONS):
        label = f"● {name}" if name == current else name
        if columns[index].button(label, key=f"nav_{index}", use_container_width=True):
            selected = name
    st.session_state["section"] = selected
    return selected


def main() -> None:
    st.set_page_config(
        page_title="Sentinel-GPU · Signal Console",
        page_icon="◈",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    inject_theme()

    st.session_state.setdefault("section", "Overview")
    section = _click_navigation(st.session_state["section"])
    st.markdown("")

    st.sidebar.markdown(
        """
        <div class="mb-5 border-b border-steel pb-4">
          <p class="font-mono text-[11px] text-gpu">Sentinel-GPU</p>
          <p class="mt-1 text-lg font-semibold leading-snug text-paper">Signal Console</p>
          <p class="mt-1 text-xs leading-relaxed text-paper/60">
            Read-only inspection and system explanation.
          </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Keep the explicit radio key in sync with click navigation before the
    # widget is instantiated; otherwise its remembered value would override the
    # button selection on the same run.
    st.session_state["section_radio"] = section
    sidebar_choice = st.sidebar.radio(
        "Section", list(SECTIONS), key="section_radio"
    )
    if sidebar_choice != section:
        section = sidebar_choice
        st.session_state["section"] = section

    base_url = st.sidebar.text_input(
        "API base URL",
        value=os.environ.get("SENTINEL_API_URL", "http://127.0.0.1:8000"),
    )
    st.sidebar.caption("Runs on 127.0.0.1:8501. The dashboard never writes to AWS resources.")

    client = SentinelApiClient(base_url)
    if section == "Runs & alerts":
        SECTIONS[section](client)
    else:
        SECTIONS[section](ev.benchmark_evidence())


if __name__ == "__main__":
    main()
