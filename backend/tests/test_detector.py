"""Tests for Detector and hysteresis (Angel's module) written by Joshua per issue #37."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List
import pytest

from backend.app.config import settings
from backend.app.engine.baseline import BaselineState

try:
    from backend.app.engine.detector import Detector, Detection
except ImportError:
    Detector = None  # type: ignore
    Detection = None  # type: ignore


@dataclass
class MockWindowSnapshot:
    ts: datetime
    total_1m: int
    errors_1m: int
    error_rate_1m: float
    total_5m: int = 0
    errors_5m: int = 0
    error_rate_5m: float = 0.0
    per_service_errors_1m: Dict[str, int] = field(default_factory=dict)
    per_service_rate_1m: Dict[str, float] = field(default_factory=dict)


@pytest.mark.skipif(Detector is None, reason="Detector component not yet merged")
def test_detector_inactive_during_learning():
    """Verify detector never fires an anomaly while baseline is in learning state."""
    det = Detector()
    base_state = BaselineState(
        baseline=2.0, scale=0.5, band_low=0.5, band_high=3.5, learning=True, frozen=False
    )
    snap = MockWindowSnapshot(
        ts=datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc),
        total_1m=100,
        errors_1m=50,
        error_rate_1m=50.0,
    )
    result = det.evaluate(snap, base_state)
    assert result.active is False


@pytest.mark.skipif(Detector is None, reason="Detector component not yet merged")
def test_detector_spike_fires_quickly():
    """Verify sudden spike above z_on triggers active=True within on_ticks consecutive ticks."""
    det = Detector()
    base_state = BaselineState(
        baseline=2.0, scale=0.5, band_low=0.5, band_high=3.5, learning=False, frozen=False
    )
    start = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)

    # Spike with 25% errors (z-score = (25-2)/0.5 = 46 >> z_on)
    results = []
    for i in range(settings.on_ticks + 2):
        snap = MockWindowSnapshot(
            ts=start + timedelta(seconds=i),
            total_1m=100,
            errors_1m=25,
            error_rate_1m=25.0,
            per_service_rate_1m={"patient-records": 25.0},
        )
        res = det.evaluate(snap, base_state)
        results.append(res.active)

    assert results[-1] is True


@pytest.mark.skipif(Detector is None, reason="Detector component not yet merged")
def test_detector_hysteresis_prevents_flapping():
    """Verify an active anomaly stays active until off_ticks consecutive ticks fall below z_off."""
    det = Detector()
    base_state = BaselineState(
        baseline=2.0, scale=0.5, band_low=0.5, band_high=3.5, learning=False, frozen=True
    )
    start = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)

    # 1. Trigger anomaly
    for i in range(settings.on_ticks):
        snap = MockWindowSnapshot(
            ts=start + timedelta(seconds=i),
            total_1m=100,
            errors_1m=20,
            error_rate_1m=20.0,
        )
        res = det.evaluate(snap, base_state)
    assert res.active is True

    # 2. Drop rate below z_off for fewer than off_ticks
    for i in range(settings.off_ticks - 2):
        snap = MockWindowSnapshot(
            ts=start + timedelta(seconds=10 + i),
            total_1m=100,
            errors_1m=2,
            error_rate_1m=2.0,
        )
        res = det.evaluate(snap, base_state)
        assert res.active is True, "Anomaly should stay active during hysteresis cooldown"

    # 3. Complete remaining off_ticks
    for i in range(settings.off_ticks - 2, settings.off_ticks + 1):
        snap = MockWindowSnapshot(
            ts=start + timedelta(seconds=10 + i),
            total_1m=100,
            errors_1m=2,
            error_rate_1m=2.0,
        )
        res = det.evaluate(snap, base_state)

    assert res.active is False, "Anomaly should clear after off_ticks consecutive clean samples"
