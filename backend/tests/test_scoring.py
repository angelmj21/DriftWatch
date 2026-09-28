"""Tests for benchmark scoring against ground-truth incident labels."""
from datetime import datetime, timedelta, timezone
import json
import os
import sys
import tempfile

from benchmarks.scoring import LabeledIncident, Report, load_labels, score


def test_load_labels():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        f.write('{"id": "inc-1", "type": "spike", "services": ["patient-records"], "start": "2026-09-28T10:00:00.000Z", "end": "2026-09-28T10:02:00.000Z"}\n')
        f.write('{"id": "inc-2", "type": "drift", "services": ["pharmacy"], "start": "2026-09-28T10:05:00.000Z", "end": "2026-09-28T10:15:00.000Z"}\n')
        tmp_name = f.name

    try:
        labels = load_labels(tmp_name)
        assert len(labels) == 2
        assert labels[0].id == "inc-1"
        assert labels[0].type == "spike"
        assert labels[0].services == ["patient-records"]
        assert labels[0].start == datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)
        assert labels[0].end == datetime(2026, 9, 28, 10, 2, 0, tzinfo=timezone.utc)

        assert labels[1].id == "inc-2"
        assert labels[1].type == "drift"
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def test_score_detected_and_delay():
    t0 = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)
    lbl = LabeledIncident(
        id="inc-1",
        type="spike",
        services=["patient-records"],
        start=t0,
        end=t0 + timedelta(seconds=120),
    )

    # Alert opened 15s after start
    alert_time = t0 + timedelta(seconds=15)
    report = score([alert_time], [lbl])

    assert report.incidents_total == 1
    assert report.detected == 1
    assert report.missed == 0
    assert report.false_alarms == 0
    assert report.detection_delays_s == [15.0]
    assert report.mean_delay_s == 15.0
    assert report.per_incident[0]["detected"] is True
    assert report.per_incident[0]["delay_s"] == 15.0


def test_score_early_alert_within_grace_clamps_to_zero():
    t0 = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)
    lbl = LabeledIncident(
        id="inc-1",
        type="spike",
        services=["patient-records"],
        start=t0,
        end=t0 + timedelta(seconds=120),
    )

    # Alert opened 10s BEFORE label start (within 30s grace)
    alert_time = t0 - timedelta(seconds=10)
    report = score([alert_time], [lbl], grace_s=30.0)

    assert report.detected == 1
    assert report.false_alarms == 0
    # Delay clamped to 0.0
    assert report.detection_delays_s == [0.0]
    assert report.mean_delay_s == 0.0


def test_score_duplicate_opens_ignored():
    t0 = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)
    lbl = LabeledIncident(
        id="inc-1",
        type="spike",
        services=["patient-records"],
        start=t0,
        end=t0 + timedelta(seconds=120),
    )

    # First open at +10s, subsequent opens at +20s and +30s
    alerts = [
        t0 + timedelta(seconds=10),
        t0 + timedelta(seconds=20),
        t0 + timedelta(seconds=30),
    ]
    report = score(alerts, [lbl])

    assert report.detected == 1
    assert report.false_alarms == 0
    # Delay reflects the FIRST open only
    assert report.detection_delays_s == [10.0]


def test_score_missed_incident():
    t0 = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)
    lbl = LabeledIncident(
        id="inc-1",
        type="spike",
        services=["patient-records"],
        start=t0,
        end=t0 + timedelta(seconds=120),
    )

    # No alerts generated
    report = score([], [lbl])

    assert report.incidents_total == 1
    assert report.detected == 0
    assert report.missed == 1
    assert report.false_alarms == 0
    assert report.detection_delays_s == []
    assert report.mean_delay_s is None
    assert report.per_incident[0]["detected"] is False
    assert report.per_incident[0]["delay_s"] is None


def test_score_false_alarms():
    t0 = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)
    lbl = LabeledIncident(
        id="inc-1",
        type="spike",
        services=["patient-records"],
        start=t0,
        end=t0 + timedelta(seconds=60),
    )

    # Alerts matching outside grace (> 30s away)
    alerts = [
        t0 - timedelta(seconds=60),  # FA 1 (too early)
        t0 + timedelta(seconds=20),  # Valid detection
        t0 + timedelta(seconds=150), # FA 2 (too late)
    ]
    report = score(alerts, [lbl], grace_s=30.0)

    assert report.incidents_total == 1
    assert report.detected == 1
    assert report.missed == 0
    assert report.false_alarms == 2
    assert report.detection_delays_s == [20.0]
