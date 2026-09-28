"""Tests for Severity scoring (Angel's module) written by Joshua per issue #37."""
from datetime import datetime, timezone
import pytest

from backend.app.config import settings

try:
    from backend.app.engine.detector import Detection
    from backend.app.engine.severity import Severity, score as score_severity
except ImportError:
    Detection = None  # type: ignore
    Severity = None  # type: ignore
    score_severity = None  # type: ignore


@pytest.mark.skipif(score_severity is None, reason="Severity component not yet merged")
def test_severity_criticality_ranking():
    """Verify patient-records produces higher score than appointments for identical deviations."""
    det_records = Detection(
        active=True,
        observed=15.0,
        baseline=2.0,
        deviation=8.0,
        slope=1.0,
        services=["patient-records"],
    )
    det_appointments = Detection(
        active=True,
        observed=15.0,
        baseline=2.0,
        deviation=8.0,
        slope=1.0,
        services=["appointments"],
    )

    sev_records = score_severity(det_records, duration_s=60.0, sig_variety=1, services=["patient-records"])
    sev_appointments = score_severity(det_appointments, duration_s=60.0, sig_variety=1, services=["appointments"])

    assert sev_records.score > sev_appointments.score, (
        f"patient-records ({sev_records.score}) must outrank appointments ({sev_appointments.score})"
    )


@pytest.mark.skipif(score_severity is None, reason="Severity component not yet merged")
def test_severity_monotonic_with_duration_and_variety():
    """Verify severity score increases monotonically as duration and signature variety grow."""
    det = Detection(
        active=True,
        observed=15.0,
        baseline=2.0,
        deviation=6.0,
        slope=1.0,
        services=["patient-records"],
    )

    s1 = score_severity(det, duration_s=10.0, sig_variety=1, services=["patient-records"])
    s2 = score_severity(det, duration_s=120.0, sig_variety=1, services=["patient-records"])
    s3 = score_severity(det, duration_s=120.0, sig_variety=4, services=["patient-records"])

    assert s2.score >= s1.score
    assert s3.score >= s2.score


@pytest.mark.skipif(score_severity is None, reason="Severity component not yet merged")
def test_severity_threshold_mapping():
    """Verify scores map to INFO, WARN, and CRITICAL according to config thresholds."""
    # Check that levels are one of the allowed literals
    det = Detection(
        active=True,
        observed=25.0,
        baseline=2.0,
        deviation=15.0,
        slope=3.0,
        services=["patient-records", "pharmacy"],
    )
    sev = score_severity(det, duration_s=300.0, sig_variety=3, services=["patient-records", "pharmacy"])
    assert sev.level in ("INFO", "WARN", "CRITICAL")
    if sev.score >= settings.sev_critical_score:
        assert sev.level == "CRITICAL"
    elif sev.score >= settings.sev_warn_score:
        assert sev.level == "WARN"
    else:
        assert sev.level == "INFO"
