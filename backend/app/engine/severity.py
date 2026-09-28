"""Pure severity scoring for detected incidents and signature-only findings."""
from __future__ import annotations

from dataclasses import dataclass

from ..config import settings
from .detector import Detection


@dataclass
class Severity:
    level: str
    score: float


def score(
    det: Detection,
    duration_s: float,
    sig_variety: int,
    services: list[str],
) -> Severity:
    deviation = _normalize(det.deviation, settings.z_on + settings.z_soft)
    duration = _normalize(duration_s, settings.window_long_s)
    slope_limit = max(settings.slope_on, settings.slope_on * settings.z_on)
    slope = _normalize(det.slope, slope_limit)
    variety = _normalize(sig_variety, settings.sig_min_count)

    highest_criticality = max(settings.service_criticality.values(), default=0.0)
    incident_criticality = max(
        (settings.service_criticality.get(service, 0.0) for service in services),
        default=0.0,
    )
    criticality = _normalize(incident_criticality, highest_criticality)

    weights = (
        (deviation, settings.sev_w_deviation),
        (duration, settings.sev_w_duration),
        (slope, settings.sev_w_slope),
        (variety, settings.sev_w_variety),
        (criticality, settings.sev_w_criticality),
    )
    weight_total = sum(weight for _, weight in weights)
    weighted_score = (
        sum(component * weight for component, weight in weights) / weight_total
        if weight_total > 0
        else 0.0
    )
    weighted_score = min(max(weighted_score, 0.0), 1.0)

    if not det.active and sig_variety > 0:
        weighted_score = max(weighted_score, settings.sev_warn_score)

    if weighted_score >= settings.sev_critical_score:
        level = "CRITICAL"
    elif weighted_score >= settings.sev_warn_score:
        level = "WARN"
    else:
        level = "INFO"

    return Severity(level=level, score=weighted_score)


def _normalize(value: float, maximum: float) -> float:
    if maximum <= 0:
        return 0.0
    return min(max(value, 0.0) / maximum, 1.0)