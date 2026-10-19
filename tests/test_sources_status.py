"""SourceRegistry behavior: attach, fetch/error recording, cached cycles."""

from datetime import UTC, datetime
from pathlib import Path

from app.sources import SourceRegistry
from app.sources.fixture_source import fixture_sources

FIXTURES = Path(__file__).parent / "fixtures"


def make_registry() -> SourceRegistry:
    return SourceRegistry()


def test_unattached_sources_report_unavailable():
    reg = make_registry()
    assert all(not s.available for s in reg.statuses())
    assert all(s.latest_cycle is None for s in reg.statuses())


def test_attach_marks_available_and_reports_real_cycles():
    reg = make_registry()
    reg.attach(fixture_sources(FIXTURES))
    by_name = {s.name: s for s in reg.statuses()}
    for name in ("hrrr", "gfs", "era5", "mrms"):
        assert by_name[name].available, name
        # refresh_cached_cycles (called by attach) pulled the fixture cycle.
        assert by_name[name].latest_cycle is not None, name
        assert by_name[name].latest_cycle.startswith("2024-"), name


def test_record_fetch_updates_status():
    reg = make_registry()
    reg.attach(fixture_sources(FIXTURES))
    cycle = datetime(2024, 6, 24, 12, tzinfo=UTC)
    reg.record_fetch("hrrr", cycle, 1.234)
    status = {s.name: s for s in reg.statuses()}["hrrr"]
    assert status.latest_cycle == cycle.isoformat()
    assert status.last_fetch_latency_s == 1.234
    assert status.available


def test_record_error_increments_and_truncates():
    reg = make_registry()
    reg.record_error("gfs", "boom " * 100)
    status = {s.name: s for s in reg.statuses()}["gfs"]
    assert status.error_count == 1
    assert len(status.detail) <= 200
    reg.record_error("gfs", "again")
    assert {s.name: s for s in reg.statuses()}["gfs"].error_count == 2


def test_record_on_unknown_source_is_noop():
    reg = make_registry()
    reg.record_fetch("nope", datetime.now(UTC), 0.1)
    reg.record_error("nope", "x")
    assert "nope" not in {s.name for s in reg.statuses()}
