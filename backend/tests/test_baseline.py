"""Tests for AdaptiveBaseline (Angel's module) written by Joshua per issue #37."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import pytest

from backend.app.config import settings
from backend.app.engine.baseline import AdaptiveBaseline, BaselineState


@dataclass
class MockSnapshot:
    ts: datetime
    total_1m: int
    error_rate_1m: float


def test_baseline_learning_phase():
    """Verify baseline stays in learning=True until settings.baseline_min_samples are seen."""
    base = AdaptiveBaseline()
    start = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)

    for i in range(settings.baseline_min_samples - 1):
        snap = MockSnapshot(
            ts=start + timedelta(seconds=i),
            total_1m=100,
            error_rate_1m=2.0,
        )
        state = base.update(snap, anomaly_active=False)
        assert state.learning is True, f"Expected learning at sample {i}"

    # After N samples, learning should transition to False
    snap_n = MockSnapshot(
        ts=start + timedelta(seconds=settings.baseline_min_samples),
        total_1m=100,
        error_rate_1m=2.0,
    )
    state_n = base.update(snap_n, anomaly_active=False)
    assert state_n.learning is False, "Expected learning=False after min_samples"


def test_baseline_convergence():
    """Verify baseline converges around the true median for noisy stable signal."""
    base = AdaptiveBaseline()
    start = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)

    # Alternate between 1.8% and 2.2% (mean/median = 2.0%)
    for i in range(150):
        val = 1.8 if i % 2 == 0 else 2.2
        snap = MockSnapshot(
            ts=start + timedelta(seconds=i),
            total_1m=100,
            error_rate_1m=val,
        )
        state = base.update(snap, anomaly_active=False)

    assert abs(state.baseline - 2.0) < 0.1, f"Expected baseline ~2.0, got {state.baseline}"
    assert state.band_low < state.baseline < state.band_high


def test_baseline_frozen_during_anomaly():
    """Verify baseline does not update history while an anomaly is active."""
    base = AdaptiveBaseline()
    start = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)

    # Train baseline to 2.0%
    for i in range(130):
        snap = MockSnapshot(
            ts=start + timedelta(seconds=i),
            total_1m=100,
            error_rate_1m=2.0,
        )
        base.update(snap, anomaly_active=False)

    baseline_before = base.update(
        MockSnapshot(ts=start + timedelta(seconds=130), total_1m=100, error_rate_1m=2.0),
        anomaly_active=False,
    ).baseline

    # Anomaly occurs with 30% error rate for 60 seconds
    for i in range(131, 191):
        snap = MockSnapshot(
            ts=start + timedelta(seconds=i),
            total_1m=100,
            error_rate_1m=30.0,
        )
        state = base.update(snap, anomaly_active=True)
        assert state.frozen is True

    # Baseline should remain frozen near 2.0, NOT dragged up to 30%
    assert abs(state.baseline - baseline_before) < 0.2, (
        f"Baseline moved during anomaly: before={baseline_before}, current={state.baseline}"
    )


def test_baseline_quiet_traffic_ignored():
    """Verify samples with total_1m < settings.min_events_per_window are ignored."""
    base = AdaptiveBaseline()
    start = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)

    # Feed 50 low-traffic samples with 50% errors
    for i in range(50):
        snap = MockSnapshot(
            ts=start + timedelta(seconds=i),
            total_1m=settings.min_events_per_window - 5,
            error_rate_1m=50.0,
        )
        state = base.update(snap, anomaly_active=False)

    # Samples seen should remain 0 because traffic is below threshold
    assert base._samples_seen == 0
    assert len(base._history) == 0
    assert state.learning is True


def test_baseline_scale_floor_no_divide_by_zero():
    """Verify scale never drops below settings.min_scale even with identical 0.0 variance."""
    base = AdaptiveBaseline()
    start = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)

    for i in range(50):
        snap = MockSnapshot(
            ts=start + timedelta(seconds=i),
            total_1m=100,
            error_rate_1m=1.5,  # Exactly constant
        )
        state = base.update(snap, anomaly_active=False)

    assert state.scale >= settings.min_scale
    assert state.scale > 0.0
    assert state.band_low >= 0.0
