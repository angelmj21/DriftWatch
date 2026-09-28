"""Sliding window with per-second buckets for live error-rate computation.

Provides O(1) snapshots of 1-minute and 5-minute error rates with
per-service breakdowns. Supports late-line tolerance and watermark tracking.
All rates are percent (0-100).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.app.ingest.parser import LogEvent

# Import settings with graceful fallback
try:
    from backend.app.config import settings as _settings
except ImportError:
    try:
        from app.config import settings as _settings  # type: ignore
    except ImportError:
        _settings = None

# Defaults pulled from config, with hardcoded fallback
_WINDOW_SHORT_S: int = getattr(_settings, "window_short_s", 60) if _settings else 60
_WINDOW_LONG_S: int = getattr(_settings, "window_long_s", 300) if _settings else 300
_ALLOWED_LATENESS_S: float = getattr(_settings, "allowed_lateness_s", 5.0) if _settings else 5.0


@dataclass
class WindowSnapshot:
    """Point-in-time snapshot of sliding window metrics."""
    ts: datetime
    total_1m: int
    errors_1m: int
    error_rate_1m: float  # percent 0-100
    total_5m: int
    errors_5m: int
    error_rate_5m: float  # percent 0-100
    per_service_errors_1m: Dict[str, int] = field(default_factory=dict)
    per_service_rate_1m: Dict[str, float] = field(default_factory=dict)  # percent


@dataclass
class _Bucket:
    """Per-second accumulator bucket."""
    total: int = 0
    errors: int = 0
    per_service_total: Dict[str, int] = field(default_factory=dict)
    per_service_errors: Dict[str, int] = field(default_factory=dict)


class SlidingWindow:
    """Sliding window over per-second buckets with running sums for O(1) snapshots."""

    def __init__(
        self,
        window_short_s: int = _WINDOW_SHORT_S,
        window_long_s: int = _WINDOW_LONG_S,
        allowed_lateness_s: float = _ALLOWED_LATENESS_S,
    ):
        self.window_short_s = window_short_s
        self.window_long_s = window_long_s
        self.allowed_lateness_s = allowed_lateness_s

        # Per-second buckets keyed by integer epoch second
        self._buckets: Dict[int, _Bucket] = {}

        # Running sums for short (1m) window
        self._total_short: int = 0
        self._errors_short: int = 0
        self._svc_total_short: Dict[str, int] = {}
        self._svc_errors_short: Dict[str, int] = {}

        # Running sums for long (5m) window
        self._total_long: int = 0
        self._errors_long: int = 0

        # Watermark: maximum event timestamp seen (epoch seconds)
        self._watermark_s: Optional[int] = None

        # Late-dropped counter
        self.late_dropped: int = 0

    def _bucket_key(self, ts: datetime) -> int:
        """Convert a datetime to integer epoch second for bucket keying."""
        return int(ts.timestamp())

    def _is_error(self, ev: "LogEvent") -> bool:
        """Determine if an event counts as an ERROR (only 5xx / ERROR level)."""
        return ev.level == "ERROR"

    def add(self, ev: "LogEvent") -> None:
        """Add a parsed log event to the window."""
        ev_sec = self._bucket_key(ev.ts)

        # Update watermark
        if self._watermark_s is None:
            self._watermark_s = ev_sec
        elif ev_sec > self._watermark_s:
            self._watermark_s = ev_sec

        # Late-line check: reject events older than watermark - allowed_lateness_s
        if self._watermark_s is not None and ev_sec < (self._watermark_s - self.allowed_lateness_s):
            self.late_dropped += 1
            return

        is_err = self._is_error(ev)

        # Get or create bucket
        if ev_sec not in self._buckets:
            self._buckets[ev_sec] = _Bucket()
        bucket = self._buckets[ev_sec]

        bucket.total += 1
        if is_err:
            bucket.errors += 1

        svc = ev.service
        bucket.per_service_total[svc] = bucket.per_service_total.get(svc, 0) + 1
        if is_err:
            bucket.per_service_errors[svc] = bucket.per_service_errors.get(svc, 0) + 1

    def _evict(self, now_sec: int) -> None:
        """Remove buckets older than the long window and update running sums."""
        cutoff_long = now_sec - self.window_long_s
        to_remove = [k for k in self._buckets if k <= cutoff_long]
        for k in to_remove:
            del self._buckets[k]

    def _compute_range(self, now_sec: int, window_s: int):
        """Sum totals and errors for buckets within [now_sec - window_s + 1, now_sec]."""
        cutoff = now_sec - window_s
        total = 0
        errors = 0
        svc_total: Dict[str, int] = {}
        svc_errors: Dict[str, int] = {}

        for sec, bucket in self._buckets.items():
            if sec > cutoff and sec <= now_sec:
                total += bucket.total
                errors += bucket.errors
                for svc, cnt in bucket.per_service_total.items():
                    svc_total[svc] = svc_total.get(svc, 0) + cnt
                for svc, cnt in bucket.per_service_errors.items():
                    svc_errors[svc] = svc_errors.get(svc, 0) + cnt

        return total, errors, svc_total, svc_errors

    def snapshot(self, now: datetime) -> WindowSnapshot:
        """Compute a point-in-time snapshot of the sliding window.

        Parameters
        ----------
        now : datetime
            The current pipeline time (typically the watermark).

        Returns
        -------
        WindowSnapshot
            Snapshot with 1m/5m totals, error rates (percent 0-100),
            and per-service error breakdowns for the short window.
        """
        now_sec = self._bucket_key(now)

        # Evict old buckets beyond the long window
        self._evict(now_sec)

        # Short window (1m)
        total_1m, errors_1m, svc_total_1m, svc_errors_1m = self._compute_range(
            now_sec, self.window_short_s
        )
        error_rate_1m = (errors_1m / total_1m * 100.0) if total_1m > 0 else 0.0

        # Long window (5m)
        total_5m, errors_5m, _, _ = self._compute_range(
            now_sec, self.window_long_s
        )
        error_rate_5m = (errors_5m / total_5m * 100.0) if total_5m > 0 else 0.0

        # Per-service error rates for 1m window (percent)
        per_service_rate_1m: Dict[str, float] = {}
        for svc, svc_t in svc_total_1m.items():
            svc_e = svc_errors_1m.get(svc, 0)
            per_service_rate_1m[svc] = (svc_e / svc_t * 100.0) if svc_t > 0 else 0.0

        return WindowSnapshot(
            ts=now,
            total_1m=total_1m,
            errors_1m=errors_1m,
            error_rate_1m=round(error_rate_1m, 4),
            total_5m=total_5m,
            errors_5m=errors_5m,
            error_rate_5m=round(error_rate_5m, 4),
            per_service_errors_1m=svc_errors_1m,
            per_service_rate_1m={k: round(v, 4) for k, v in per_service_rate_1m.items()},
        )
