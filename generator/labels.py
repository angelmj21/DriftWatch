"""Ground-truth labels writer for the DriftWatch synthetic generator.

Records exactly when each incident really started and ended in JSON Lines format:
{"id": "...", "type": "...", "services": [...], "start": "<iso>", "end": "<iso>"}

Flushed immediately on incident resolution, closes active incidents cleanly on shutdown.
Never read by the backend detection engine; only read by benchmarks/scoring.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Union


def _to_utc_iso(ts: Union[datetime, float, int]) -> str:
    """Format timestamp as UTC ISO-8601 string with milliseconds and Z."""
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts, timezone.utc)
    elif ts.tzinfo is None:
        dt = ts.replace(tzinfo=timezone.utc)
    else:
        dt = ts.astimezone(timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class _ActiveIncident:
    id: str
    incident_type: str
    services: List[str]
    start_ts: Union[datetime, float, int]


class LabelWriter:
    """Writes ground-truth incident intervals to a JSON Lines file."""

    def __init__(self, path: Union[str, Path] = "./logs/labels.jsonl"):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._active: Dict[str, _ActiveIncident] = {}
        self._file = open(self.path, "a", encoding="utf-8")

    def start(
        self,
        incident_id: str,
        incident_type: str,
        services: List[str],
        ts: Union[datetime, float, int],
    ) -> None:
        """Record the start of an incident."""
        self._active[incident_id] = _ActiveIncident(
            id=incident_id,
            incident_type=incident_type,
            services=list(services),
            start_ts=ts,
        )

    def end(self, incident_id: str, ts: Union[datetime, float, int]) -> None:
        """Record the resolution of an incident and append the label entry."""
        if incident_id not in self._active:
            return

        inc = self._active.pop(incident_id)
        entry = {
            "id": inc.id,
            "type": inc.incident_type,
            "services": inc.services,
            "start": _to_utc_iso(inc.start_ts),
            "end": _to_utc_iso(ts),
        }
        self._file.write(json.dumps(entry) + "\n")
        self._file.flush()

    def close(self, ts: Optional[Union[datetime, float, int]] = None) -> None:
        """Close writer, recording end time for any remaining open incidents."""
        end_time = ts if ts is not None else datetime.now(timezone.utc)

        # End all remaining active incidents cleanly
        for incident_id in list(self._active.keys()):
            self.end(incident_id, end_time)

        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> LabelWriter:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
