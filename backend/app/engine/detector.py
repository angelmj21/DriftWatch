"""Detect sustained error-rate deviations from the learned baseline."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from ..config import settings
from .baseline import BaselineState
from .window import WindowSnapshot


@dataclass
class Detection:
    active: bool
    observed: float
    baseline: float
    deviation: float
    slope: float
    services: list[str]


class Detector:
    def __init__(self) -> None:
        self._observations: deque[tuple[datetime, float]] = deque()
        self._active = False
        self._on_count = 0
        self._off_count = 0

    def evaluate(self, snap: WindowSnapshot, base: BaselineState) -> Detection:
        observed = snap.error_rate_1m
        scale = max(base.scale, settings.min_scale)
        deviation = (observed - base.baseline) / scale

        if base.learning:
            self._observations.clear()
            self._active = False
            self._on_count = 0
            self._off_count = 0
            return Detection(
                active=False,
                observed=observed,
                baseline=base.baseline,
                deviation=deviation,
                slope=0.0,
                services=[],
            )

        self._record_observation(snap.ts, observed)
        slope = self._slope_per_minute()
        spike = deviation >= settings.z_on
        drift = deviation >= settings.z_soft and slope >= settings.slope_on

        if self._active:
            if deviation <= settings.z_off:
                self._off_count += 1
                if self._off_count >= settings.off_ticks:
                    self._active = False
                    self._off_count = 0
                    self._on_count = 0
            else:
                self._off_count = 0
        elif spike or drift:
            self._on_count += 1
            if self._on_count >= settings.on_ticks:
                self._active = True
                self._on_count = 0
        else:
            self._on_count = 0

        services = sorted(
            service
            for service, service_rate in snap.per_service_rate_1m.items()
            if service_rate > base.band_high
        )

        return Detection(
            active=self._active,
            observed=observed,
            baseline=base.baseline,
            deviation=deviation,
            slope=slope,
            services=services,
        )

    def _record_observation(self, ts: datetime, observed: float) -> None:
        cutoff = ts.timestamp() - settings.slope_window_s
        while self._observations and self._observations[0][0].timestamp() < cutoff:
            self._observations.popleft()
        self._observations.append((ts, observed))

    def _slope_per_minute(self) -> float:
        if len(self._observations) < 2:
            return 0.0

        timestamps = [ts.timestamp() for ts, _ in self._observations]
        values = [value for _, value in self._observations]
        mean_time = sum(timestamps) / len(timestamps)
        mean_value = sum(values) / len(values)
        time_variance = sum((timestamp - mean_time) ** 2 for timestamp in timestamps)
        if time_variance == 0:
            return 0.0

        covariance = sum(
            (timestamp - mean_time) * (value - mean_value)
            for timestamp, value in zip(timestamps, values)
        )
        return covariance / time_variance * 60.0