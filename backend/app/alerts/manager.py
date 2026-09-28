"""Build deduplicated alert messages from per-tick pipeline state."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from ..config import settings
from ..engine.detector import Detection
from ..engine.severity import Severity
from ..engine.signature_tracker import SigCount, SigFinding
from .schema import Alert


_SEVERITY_RANK = {"INFO": 0, "WARN": 1, "CRITICAL": 2}


@dataclass
class _Incident:
    id: str
    started_at: datetime
    last_activity: datetime
    last_emitted_at: datetime
    severity: str
    services: set[str] = field(default_factory=set)
    new_signature: bool = False
    new_template: str | None = None
    is_open: bool = True
    resolved_at: datetime | None = None
    last_payload: tuple[object, ...] | None = None


class AlertManager:
    def __init__(self) -> None:
        self._incident: _Incident | None = None

    def process(
        self,
        now: datetime,
        det: Detection,
        sev: Severity,
        sigs: SigFinding | None,
        top: list[SigCount],
    ) -> list[Alert]:
        new_signatures = sigs.new_signatures if sigs is not None else []
        has_new_signature = bool(new_signatures)
        triggered = det.active or has_new_signature

        if self._incident is None:
            if not triggered:
                return []
            self._incident = self._start_incident(now, det, sev, new_signatures)
            return [self._emit(now, "open", det, top, self._incident)]

        incident = self._incident
        if not incident.is_open:
            if not triggered:
                return []
            if incident.resolved_at is None or (
                now - incident.resolved_at
            ).total_seconds() > settings.cooldown_s:
                self._incident = self._start_incident(now, det, sev, new_signatures)
                return [self._emit(now, "open", det, top, self._incident)]

            incident.is_open = True
            incident.resolved_at = None
            incident.last_activity = now
            self._update_incident(incident, det, sev, new_signatures)
            return [self._emit(now, "updated", det, top, incident)]

        service_set_grew, severity_rose, new_signature_joined = self._update_incident(
            incident, det, sev, new_signatures
        )
        if triggered:
            incident.last_activity = now

        if not triggered and (now - incident.last_activity).total_seconds() >= settings.resolve_after_s:
            incident.is_open = False
            incident.resolved_at = now
            alert = self._build_alert(now, "resolved", det, top, incident)
            incident.last_emitted_at = now
            incident.last_payload = self._payload(det, top, incident)
            return [alert]

        if not service_set_grew and not severity_rose and not new_signature_joined:
            if (now - incident.last_emitted_at).total_seconds() < settings.update_interval_s:
                return []

        payload = self._payload(det, top, incident)
        if payload == incident.last_payload:
            return []
        return [self._emit(now, "updated", det, top, incident)]

    def _start_incident(
        self,
        now: datetime,
        det: Detection,
        sev: Severity,
        new_signatures: list[SigCount],
    ) -> _Incident:
        incident = _Incident(
            id=uuid4().hex[:8],
            started_at=now,
            last_activity=now,
            last_emitted_at=now,
            severity=sev.level,
            services=set(det.services),
            new_signature=bool(new_signatures),
            new_template=new_signatures[0].template if new_signatures else None,
        )
        if not incident.services:
            incident.services.add("unknown")
        return incident

    def _update_incident(
        self,
        incident: _Incident,
        det: Detection,
        sev: Severity,
        new_signatures: list[SigCount],
    ) -> tuple[bool, bool, bool]:
        added_services = set(det.services) - incident.services
        if added_services:
            incident.services.update(added_services)
            incident.services.discard("unknown")

        severity_rose = _SEVERITY_RANK.get(sev.level, 0) > _SEVERITY_RANK.get(incident.severity, 0)
        if severity_rose:
            incident.severity = sev.level

        new_signature_joined = bool(new_signatures) and not incident.new_signature
        if new_signatures:
            incident.new_signature = True
            if incident.new_template is None:
                incident.new_template = new_signatures[0].template

        return bool(added_services), severity_rose, new_signature_joined

    def _emit(
        self,
        now: datetime,
        status: str,
        det: Detection,
        top: list[SigCount],
        incident: _Incident,
    ) -> Alert:
        alert = self._build_alert(now, status, det, top, incident)
        incident.last_emitted_at = now
        incident.last_payload = self._payload(det, top, incident)
        return alert

    def _build_alert(
        self,
        now: datetime,
        status: str,
        det: Detection,
        top: list[SigCount],
        incident: _Incident,
    ) -> Alert:
        updated_at = now.astimezone(timezone.utc)
        resolved_at = updated_at if status == "resolved" else None
        if status == "resolved":
            duration = max((now - incident.started_at).total_seconds(), 0.0)
            explanation = (
                f"Incident resolved: error rate is back to {det.observed:.1f}% "
                f"(baseline {det.baseline:.1f}%). Lasted {self._format_duration(duration)} "
                f"and peaked at {incident.severity}."
            )
        else:
            explanation = self._explanation(now, det, top, incident)

        return Alert(
            id=incident.id,
            status=status,
            severity=incident.severity,
            services=sorted(incident.services),
            metric="error_rate",
            observed=det.observed,
            baseline=det.baseline,
            deviation=det.deviation,
            slope=det.slope,
            top_signatures=[
                {"signature": item.signature, "template": item.template, "count": item.count}
                for item in top
            ],
            new_signature=incident.new_signature,
            explanation=explanation,
            started_at=incident.started_at.astimezone(timezone.utc),
            updated_at=updated_at,
            resolved_at=resolved_at,
        )

    def _explanation(
        self,
        now: datetime,
        det: Detection,
        top: list[SigCount],
        incident: _Incident,
    ) -> str:
        service_names = sorted(incident.services)
        if len(service_names) == 1:
            service_phrase = f"on {service_names[0]}"
        else:
            service_phrase = f"across {', '.join(service_names[:-1])} and {service_names[-1]}"

        if det.slope > 0:
            trend = f"rising {det.slope:.1f} pts/min"
        elif det.slope < 0:
            trend = f"falling {abs(det.slope):.1f} pts/min"
        else:
            trend = "steady"

        started = incident.started_at.astimezone(timezone.utc).strftime("%H:%M:%S")
        explanation = (
            f"Error rate {service_phrase} is {det.observed:.1f}% vs learned baseline "
            f"{det.baseline:.1f}% ({det.deviation:.1f} sigma), {trend} since {started}."
        )
        if incident.new_signature and incident.new_template:
            explanation = (
                f"New error type never seen before: '{incident.new_template}'. "
                f"{explanation}"
            )

        if top:
            top_total = sum(item.count for item in top)
            share = round(top[0].count / top_total * 100) if top_total > 0 else 0
            explanation += (
                f" Top cause: '{top[0].template}' "
                f"({share}% of counted top-signature errors)."
            )
        return explanation

    def _payload(
        self,
        det: Detection,
        top: list[SigCount],
        incident: _Incident,
    ) -> tuple[object, ...]:
        return (
            incident.severity,
            tuple(sorted(incident.services)),
            det.observed,
            det.baseline,
            det.deviation,
            det.slope,
            tuple((item.signature, item.template, item.count) for item in top),
            incident.new_signature,
        )

    @staticmethod
    def _format_duration(duration_s: float) -> str:
        minutes, seconds = divmod(int(duration_s), 60)
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"