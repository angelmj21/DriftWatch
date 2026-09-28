"""Benchmark incident scoring against ground truth labels.

Implements scoring logic for detector evaluation:
- LabeledIncident dataclass
- load_labels(path) -> list[LabeledIncident]
- Report dataclass
- score(alert_times, labels, grace_s=30.0) -> Report

Pure functions, no dependencies on backend/app.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


def _parse_iso(iso_str: str) -> datetime:
    """Parse an ISO-8601 string into a timezone-aware UTC datetime."""
    # Handle standard Z or offset
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class LabeledIncident:
    id: str
    type: str
    services: List[str]
    start: datetime
    end: datetime


def load_labels(path: Union[str, Path]) -> List[LabeledIncident]:
    """Load ground-truth incident records from a JSON Lines file."""
    labels: List[LabeledIncident] = []
    p = Path(path).resolve()
    if not p.exists():
        return labels

    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if not line_str:
                continue
            entry = json.loads(line_str)
            labels.append(
                LabeledIncident(
                    id=entry["id"],
                    type=entry["type"],
                    services=list(entry.get("services", [])),
                    start=_parse_iso(entry["start"]),
                    end=_parse_iso(entry["end"]),
                )
            )

    return labels


@dataclass
class Report:
    incidents_total: int
    detected: int
    missed: int
    false_alarms: int
    detection_delays_s: List[float]
    mean_delay_s: Optional[float]
    per_incident: List[Dict[str, Any]]  # {id, type, detected, delay_s}


def score(
    alert_times: List[datetime],
    labels: List[LabeledIncident],
    grace_s: float = 30.0,
) -> Report:
    """Score detector alerts against ground-truth incident labels.

    Rules from docs/interfaces.md:
    - alert_times: timestamps at which a detector opened an incident.
    - An open inside [start - grace_s, end + grace_s] of a label detects it.
    - delay = open time - label start (first open only; negative delay clamps to 0.0).
    - Further opens inside the same label are ignored (not counted as false alarms).
    - An open matching no label is a false alarm.
    - Both detectors (fixed-threshold and DriftWatch) are scored by this same function.
    """
    # Normalize alert times to tz-aware UTC and sort chronologically
    sorted_alerts: List[datetime] = sorted(
        [t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc) for t in alert_times]
    )

    # State tracking per label
    detected_labels: set[str] = set()
    label_first_delays: Dict[str, float] = {}

    # Track false alarms
    false_alarms = 0

    for a_time in sorted_alerts:
        matched = False
        for lbl in labels:
            lbl_start_grace = lbl.start.timestamp() - grace_s
            lbl_end_grace = lbl.end.timestamp() + grace_s
            a_ts = a_time.timestamp()

            if lbl_start_grace <= a_ts <= lbl_end_grace:
                matched = True
                if lbl.id not in detected_labels:
                    detected_labels.add(lbl.id)
                    # delay = open time - label start, clamp negative delay to 0.0
                    delay_s = max(0.0, a_ts - lbl.start.timestamp())
                    label_first_delays[lbl.id] = round(delay_s, 3)
                # Extra opens inside the same label are ignored
                break

        if not matched:
            false_alarms += 1

    # Build per-incident results
    per_incident: List[Dict[str, Any]] = []
    detection_delays: List[float] = []

    for lbl in labels:
        is_det = lbl.id in detected_labels
        delay = label_first_delays.get(lbl.id, None)
        if delay is not None:
            detection_delays.append(delay)
        per_incident.append(
            {
                "id": lbl.id,
                "type": lbl.type,
                "detected": is_det,
                "delay_s": delay,
            }
        )

    incidents_total = len(labels)
    detected_count = len(detected_labels)
    missed_count = incidents_total - detected_count
    mean_delay = (
        round(sum(detection_delays) / len(detection_delays), 3)
        if detection_delays
        else None
    )

    return Report(
        incidents_total=incidents_total,
        detected=detected_count,
        missed=missed_count,
        false_alarms=false_alarms,
        detection_delays_s=detection_delays,
        mean_delay_s=mean_delay,
        per_incident=per_incident,
    )
