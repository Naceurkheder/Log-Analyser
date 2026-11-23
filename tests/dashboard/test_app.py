from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[2] / "dashboard_app.py"

SECTIONS = [
    "Overview",
    "CPU vs GPU",
    "Detector mechanics",
    "AWS topology",
    "Runs & alerts",
    "Benchmarks",
    "Status & roadmap",
]


def test_all_sections_render_without_exceptions():
    at = AppTest.from_file(APP, default_timeout=20)
    at.run()

    assert not at.exception, [exc.value for exc in at.exception]
    overview_markup = " ".join(str(item.value) for item in at.markdown)
    assert "See what the pipeline actually does" in overview_markup

    for index, name in enumerate(SECTIONS):
        at.button[index].click().run()
        assert not at.exception, (name, [exc.value for exc in at.exception])

    at.button[SECTIONS.index("Runs & alerts")].click().run()
    live_markup = " ".join(str(item.value) for item in at.markdown)
    assert "Live API unavailable" in live_markup


def test_detector_sandbox_click_produces_an_alert():
    at = AppTest.from_file(APP, default_timeout=20)
    at.run()
    at.button[SECTIONS.index("Detector mechanics")].click().run()

    at.form[0].form_submit_button[0].click().run()

    assert not at.exception, [exc.value for exc in at.exception]
    metrics = {item.label: item.value for item in at.metric}
    assert metrics["Lines"] == "17"
    assert metrics["Alerts"] == "1"
