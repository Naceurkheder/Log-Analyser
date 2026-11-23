from __future__ import annotations

from sentinel.dashboard import evidence as ev


def test_benchmark_evidence_reads_committed_files():
    data = ev.benchmark_evidence()
    assert set(data["paths"]) == {"cpu", "cuda", "parity", "evaluation", "report"}
    assert data["cpu"]["schema_version"] == 1
    assert data["parity"]["timings_ms"]["cpu"] == 2769.1
    assert any("speedup claim" in note for note in data["cuda"]["notes"])


def test_capability_rows_cover_all_sprints():
    rows = ev.capability_rows()
    assert {row["sprint"] for row in rows} == {
        "S1–S2", "S3", "S4", "S5", "S6", "S7", "S8"
    }
    assert all(row["status"] in {"Shipped", "Planned"} for row in rows)
    assert all(row["check"] for row in rows)


def test_not_run_items_stay_visible():
    items = ev.not_run_items()
    assert any("five-repetition" in item for item in items)
    assert any("pageable-memory" in item for item in items)
    assert ev.format_ms(12.34) == "12.3 ms"
    assert ev.format_ms(None) == "—"
