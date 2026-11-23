"""Visual tokens and CSS injection for the Streamlit signal console."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

STATIC_DIR = Path(__file__).resolve().parent / "static"
TAILWIND_CSS = STATIC_DIR / "tailwind.css"

# The design pass deliberately avoids the common dark-dashboard/acid-accent
# treatment. This is a cool instrument panel: cool paper, ink, steel rules,
# one amber lane for CPU and one blue lane for GPU.
PALETTE = {
    "ink": "#10212B",
    "paper": "#E9F0EE",
    "steel": "#AFC3C4",
    "cpu": "#E08A3C",
    "gpu": "#2F80ED",
    "alert": "#D64550",
    "moss": "#2E6F5E",
}

BOOT_CSS = """
<style>
.signal-kbd { font-family: 'IBM Plex Mono', monospace; }
.signal-focus:focus-visible { outline: 2px solid #2F80ED; outline-offset: 2px; }
@media (prefers-reduced-motion: no-preference) {
  .signal-draw { animation: signal-draw 900ms cubic-bezier(.2,.8,.2,1) both; }
  @keyframes signal-draw { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
}
</style>
"""


def inject_theme() -> None:
    """Inject the compiled Tailwind bundle and the small motion layer."""
    css = TAILWIND_CSS.read_text(encoding="utf-8") if TAILWIND_CSS.exists() else ""
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)
    st.markdown(BOOT_CSS, unsafe_allow_html=True)
