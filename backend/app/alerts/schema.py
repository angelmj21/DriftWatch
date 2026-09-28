"""Alert + live-metric wire contract (FROZEN after merge).
Change only via a PR approved by all four people. See docs/alert-schema.md."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import (BaseModel, ConfigDict, Field, field_serializer,
                      field_validator, model_validator)

Status = Literal["open", "updated", "resolved"]
Severity = Literal["INFO", "WARN", "CRITICAL"]
State = Literal["learning", "normal", "anomaly"]

_TS_FIELDS = ("timestamp", "started_at", "updated_at", "resolved_at")


class _Wire(BaseModel):
    """Base: rejects unknown fields, forces UTC, serialises timestamps as ...Z."""
    model_config = ConfigDict(extra="forbid")

    @field_validator(*_TS_FIELDS, mode="after", check_fields=False)
    @classmethod
    def _require_utc(cls, v: datetime | None):
        if v is None:
            return v
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (UTC)")
        return v.astimezone(timezone.utc)

    @field_serializer(*_TS_FIELDS, when_used="json", check_fields=False)
    def _iso_z(self, v: datetime | None):
        if v is None:
            return None
        return v.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def to_dict(self) -> dict:
        """Exactly what goes on the wire, as a dict (for the WebSocket envelope / REST)."""
        return self.model_dump(mode="json")

    def to_json(self) -> str:
        """Exactly what goes on the wire, as a JSON string."""
        return self.model_dump_json()


class TopSignature(_Wire):
    signature: str = Field(min_length=1, description="Stable id of the error type (10 hex chars)")
    template: str = Field(description="Human-readable masked message, e.g. 'Database connection timeout after <N>ms'")
    count: int = Field(ge=0, description="Occurrences in the current short window")


class Alert(_Wire):
    id: str = Field(min_length=1, description="Incident id; identical across open/updated/resolved")
    status: Status
    severity: Severity
    services: list[str] = Field(min_length=1)
    metric: Literal["error_rate"] = "error_rate"
    observed: float = Field(ge=0, description="Percent (14.2 = 14.2%)")
    baseline: float = Field(ge=0, description="Learned normal, percent. Never null in an alert")
    deviation: float = Field(description="Robust z-score (sigma) of observed vs baseline")
    slope: float = Field(description="Change in error rate, percentage points per minute")
    top_signatures: list[TopSignature] = Field(default_factory=list)
    new_signature: bool = False
    explanation: str
    started_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def _lifecycle(self):
        if self.status == "resolved" and self.resolved_at is None:
            raise ValueError("resolved alert needs resolved_at")
        if self.status != "resolved" and self.resolved_at is not None:
            raise ValueError("resolved_at only allowed when status == 'resolved'")
        return self


class LiveMetric(_Wire):
    timestamp: datetime
    error_rate: float = Field(ge=0, le=100, description="Percent over the 1-minute window")
    baseline: float | None = Field(default=None, ge=0, description="null while state == 'learning'")
    band_low: float | None = Field(default=None, ge=0, description="null while state == 'learning'")
    band_high: float | None = Field(default=None, ge=0, description="null while state == 'learning'")
    state: State

    @model_validator(mode="after")
    def _learning_nulls(self):
        vals = (self.baseline, self.band_low, self.band_high)
        if self.state == "learning":
            if any(v is not None for v in vals):
                raise ValueError("baseline/band must be null while learning (never invent a baseline)")
        else:
            if any(v is None for v in vals):
                raise ValueError("baseline/band required once state != 'learning'")
            if self.band_low > self.band_high:
                raise ValueError("band_low must be <= band_high")
        return self