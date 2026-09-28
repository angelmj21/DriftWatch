"""Exact-rate and event-time edge tests for SlidingWindow."""
from datetime import datetime, timedelta, timezone

from backend.app.engine.window import SlidingWindow
from backend.app.ingest.parser import LogEvent


def _event(ts: datetime, service: str, level: str) -> LogEvent:
    return LogEvent(
        ts=ts,
        level=level,
        service=service,
        req_id="r1",
        method="GET",
        path="/resource",
        status=500 if level == "ERROR" else 200,
        latency_ms=10.0,
        message=None,
    )


def test_empty_window_rates_are_zero():
    window = SlidingWindow(window_short_s=60, window_long_s=300)
    snapshot = window.snapshot(datetime(2026, 9, 28, tzinfo=timezone.utc))

    assert snapshot.total_1m == 0
    assert snapshot.errors_1m == 0
    assert snapshot.error_rate_1m == 0.0
    assert snapshot.total_5m == 0
    assert snapshot.errors_5m == 0
    assert snapshot.error_rate_5m == 0.0


def test_exact_short_long_and_per_service_rates():
    start = datetime(2026, 9, 28, tzinfo=timezone.utc)
    window = SlidingWindow(window_short_s=60, window_long_s=300)
    events = [
        _event(start, "patient-records", "ERROR"),
        _event(start + timedelta(seconds=1), "patient-records", "INFO"),
        _event(start + timedelta(seconds=240), "patient-records", "ERROR"),
        _event(start + timedelta(seconds=241), "lab-results", "INFO"),
        _event(start + timedelta(seconds=300), "lab-results", "ERROR"),
    ]
    for event in events:
        window.add(event)

    snapshot = window.snapshot(start + timedelta(seconds=300))

    assert (snapshot.total_1m, snapshot.errors_1m, snapshot.error_rate_1m) == (2, 1, 50.0)
    assert (snapshot.total_5m, snapshot.errors_5m, snapshot.error_rate_5m) == (4, 2, 50.0)
    assert snapshot.per_service_errors_1m == {"lab-results": 1}
    assert snapshot.per_service_rate_1m == {"lab-results": 50.0}


def test_late_event_inside_tolerance_is_kept_outside_is_dropped():
    start = datetime(2026, 9, 28, tzinfo=timezone.utc)
    window = SlidingWindow(window_short_s=60, window_long_s=300, allowed_lateness_s=5.0)
    window.add(_event(start, "patient-records", "ERROR"))
    window.add(_event(start + timedelta(seconds=10), "patient-records", "INFO"))
    window.add(_event(start + timedelta(seconds=5), "lab-results", "ERROR"))
    window.add(_event(start + timedelta(seconds=4), "appointments", "ERROR"))

    snapshot = window.snapshot(start + timedelta(seconds=10))

    assert window.late_dropped == 1
    assert snapshot.total_1m == 3
    assert snapshot.errors_1m == 2
    assert snapshot.per_service_errors_1m == {
        "patient-records": 1,
        "lab-results": 1,
    }


def test_old_buckets_are_evicted_after_long_window():
    start = datetime(2026, 9, 28, tzinfo=timezone.utc)
    window = SlidingWindow(window_short_s=60, window_long_s=300)
    window.add(_event(start, "patient-records", "ERROR"))

    snapshot = window.snapshot(start + timedelta(seconds=301))

    assert snapshot.total_1m == 0
    assert snapshot.total_5m == 0
    assert not window._buckets