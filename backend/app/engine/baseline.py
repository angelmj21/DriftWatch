"""Adaptive error-rate baseline built from timestamped window snapshots."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Protocol

from ..config import settings


class _WindowSnapshot(Protocol):
    ts: datetime
    total_1m: int
    error_rate_1m: float


@dataclass
class BaselineState:
    baseline: float
    scale: float
    band_low: float
    band_high: float
    learning: bool
    frozen: bool


class AdaptiveBaseline:
    def __init__(self) -> None:
        self._history: deque[float] = deque(maxlen=settings.baseline_history_len)
        self._ewma: float | None = None
        self._samples_seen = 0
        self._freeze_started_at: datetime | None = None

    def update(self, snap: _WindowSnapshot, anomaly_active: bool) -> BaselineState:
        sample = snap.error_rate_1m
        has_signal = snap.total_1m >= settings.min_events_per_window

        if anomaly_active:
            if self._freeze_started_at is None:
                self._freeze_started_at = snap.ts
            freeze_elapsed_s = (snap.ts - self._freeze_started_at).total_seconds()
            relearning = freeze_elapsed_s >= settings.freeze_max_s
            if has_signal and relearning:
                self._update_ewma(sample)
        else:
            self._freeze_started_at = None
            relearning = False
            if has_signal:
                self._history.append(sample)
                self._samples_seen += 1
                self._update_ewma(sample)

        history_median = median(self._history) if self._history else 0.0
        if relearning and self._ewma is not None:
            centre = self._ewma
        elif self._ewma is not None:
            alpha = settings.ewma_alpha
            centre = (1.0 - alpha) * history_median + alpha * self._ewma
        else:
            centre = history_median

        mad = median(abs(value - history_median) for value in self._history) if self._history else 0.0
        scale = max(settings.min_scale, 1.4826 * mad)
        band_low = max(0.0, centre - settings.sensitivity * scale)
        band_high = centre + settings.sensitivity * scale

        return BaselineState(
            baseline=centre,
            scale=scale,
            band_low=band_low,
            band_high=band_high,
            learning=self._samples_seen < settings.baseline_min_samples,
            frozen=anomaly_active,
        )

    def _update_ewma(self, sample: float) -> None:
        if self._ewma is None:
            self._ewma = sample
        else:
            alpha = settings.ewma_alpha
            self._ewma = alpha * sample + (1.0 - alpha) * self._ewma