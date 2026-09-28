"""Incident scenarios and timeline definitions for the DriftWatch generator.

Implements realistic, time-bounded synthetic failure modes:
1. SpikeScenario: Sudden sharp error increase on a service (e.g. DB connection timeouts)
2. DriftScenario: Slow, linear creeping error rate over ~10 minutes
3. NewErrorScenario: Novel, previously unseen error message at low volume
4. CascadeScenario: Degradation starting in patient-records and cascading to appointments and pharmacy
"""
from __future__ import annotations

import abc
from datetime import datetime, timedelta, timezone
import math
import random
from typing import Callable, Dict, List, Optional, Tuple, Union
import uuid

try:
    from generator.catalog import INCIDENT_MESSAGES
    from generator.traffic import TrafficModifier
except ImportError:
    from catalog import INCIDENT_MESSAGES  # type: ignore
    from traffic import TrafficModifier  # type: ignore


def _to_utc_dt(t: Union[datetime, float, int]) -> datetime:
    """Ensure timestamp is tz-aware UTC datetime."""
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t, timezone.utc)
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


class Scenario(abc.ABC):
    """Abstract base class for all incident scenarios."""

    def __init__(
        self,
        scenario_id: Optional[str] = None,
        duration_s: float = 120.0,
        intensity: float = 1.0,
    ):
        self.id: str = scenario_id or f"inc-{uuid.uuid4().hex[:6]}"
        self.duration_s: float = duration_s
        self.intensity: float = max(0.1, min(2.0, intensity))
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None

    @property
    @abc.abstractmethod
    def scenario_type(self) -> str:
        """Type identifier: spike | drift | new_error | cascade."""
        ...

    @property
    @abc.abstractmethod
    def services(self) -> List[str]:
        """List of services directly affected by this scenario."""
        ...

    def start(self, sim_time: Union[datetime, float, int]) -> None:
        """Start the scenario at the specified simulated time."""
        self.start_time = _to_utc_dt(sim_time)
        self.end_time = self.start_time + timedelta(seconds=self.duration_s)

    def active(self, sim_time: Union[datetime, float, int]) -> bool:
        """Check if scenario is active at the given simulated time."""
        if self.start_time is None or self.end_time is None:
            return False
        cur = _to_utc_dt(sim_time)
        return self.start_time <= cur < self.end_time

    @abc.abstractmethod
    def apply_modifiers(
        self, sim_time: Union[datetime, float, int], modifier: TrafficModifier
    ) -> None:
        """Apply scenario modifications to the TrafficModifier."""
        ...


class SpikeScenario(Scenario):
    """Sudden spike in error rate on a service (e.g. database timeouts)."""

    def __init__(
        self,
        service: str = "patient-records",
        duration_s: float = 120.0,
        target_error_share: float = 0.30,
        intensity: float = 1.0,
        scenario_id: Optional[str] = None,
    ):
        super().__init__(scenario_id=scenario_id, duration_s=duration_s, intensity=intensity)
        self.service = service
        self.target_error_share = target_error_share * self.intensity

    @property
    def scenario_type(self) -> str:
        return "spike"

    @property
    def services(self) -> List[str]:
        return [self.service]

    def apply_modifiers(
        self, sim_time: Union[datetime, float, int], modifier: TrafficModifier
    ) -> None:
        if not self.active(sim_time):
            return

        cur = _to_utc_dt(sim_time)
        elapsed_s = (cur - self.start_time).total_seconds()

        # Ramp up quickly over ~5 seconds, then hold steady
        ramp = min(1.0, elapsed_s / 5.0)
        current_share = self.target_error_share * ramp

        msg = INCIDENT_MESSAGES.get(
            "db_timeout", "Database connection timeout after 5000ms"
        )
        modifier.extra_error_injection(
            service=self.service,
            message=msg,
            share=current_share,
            status=500,
        )


class DriftScenario(Scenario):
    """Slow creeping rise in error rate from ~1% to ~15% linearly over duration."""

    def __init__(
        self,
        service: str = "patient-records",
        duration_s: float = 600.0,  # 10 minutes default
        target_error_share: float = 0.15,
        intensity: float = 1.0,
        scenario_id: Optional[str] = None,
    ):
        super().__init__(scenario_id=scenario_id, duration_s=duration_s, intensity=intensity)
        self.service = service
        self.target_error_share = target_error_share * self.intensity

    @property
    def scenario_type(self) -> str:
        return "drift"

    @property
    def services(self) -> List[str]:
        return [self.service]

    def apply_modifiers(
        self, sim_time: Union[datetime, float, int], modifier: TrafficModifier
    ) -> None:
        if not self.active(sim_time):
            return

        cur = _to_utc_dt(sim_time)
        elapsed_s = (cur - self.start_time).total_seconds()
        progress = min(1.0, elapsed_s / self.duration_s)

        # Linearly ramp from 0.01 to target_error_share
        current_share = 0.01 + (self.target_error_share - 0.01) * progress

        msg = INCIDENT_MESSAGES.get(
            "connection_pool_exhausted",
            "Connection pool exhausted: max active connections (50) reached",
        )
        modifier.extra_error_injection(
            service=self.service,
            message=msg,
            share=current_share,
            status=500,
        )


class NewErrorScenario(Scenario):
    """Novel, never-seen error message (e.g. HL7 parse failure) at low volume (~0.3%)."""

    def __init__(
        self,
        service: str = "lab-results",
        duration_s: float = 300.0,
        error_share: float = 0.003,  # 0.3% so overall rate stays well within normal range
        intensity: float = 1.0,
        scenario_id: Optional[str] = None,
    ):
        super().__init__(scenario_id=scenario_id, duration_s=duration_s, intensity=intensity)
        self.service = service
        self.error_share = error_share * self.intensity

    @property
    def scenario_type(self) -> str:
        return "new_error"

    @property
    def services(self) -> List[str]:
        return [self.service]

    def apply_modifiers(
        self, sim_time: Union[datetime, float, int], modifier: TrafficModifier
    ) -> None:
        if not self.active(sim_time):
            return

        msg = INCIDENT_MESSAGES.get(
            "hl7_parse_failure", "HL7 message parse failure: segment PID missing"
        )
        modifier.extra_error_injection(
            service=self.service,
            message=msg,
            share=self.error_share,
            status=500,
        )


class CascadeScenario(Scenario):
    """Multi-service cascading failure: patient-records -> appointments (+60s) -> pharmacy (+120s)."""

    def __init__(
        self,
        duration_s: float = 360.0,
        intensity: float = 1.0,
        scenario_id: Optional[str] = None,
    ):
        super().__init__(scenario_id=scenario_id, duration_s=duration_s, intensity=intensity)

    @property
    def scenario_type(self) -> str:
        return "cascade"

    @property
    def services(self) -> List[str]:
        return ["patient-records", "appointments", "pharmacy"]

    def apply_modifiers(
        self, sim_time: Union[datetime, float, int], modifier: TrafficModifier
    ) -> None:
        if not self.active(sim_time):
            return

        cur = _to_utc_dt(sim_time)
        elapsed_s = (cur - self.start_time).total_seconds()

        # Stage 1: patient-records degrades immediately (DB timeout)
        modifier.extra_error_injection(
            service="patient-records",
            message=INCIDENT_MESSAGES.get(
                "circuit_breaker_open", "Circuit breaker OPEN for patient-records backend"
            ),
            share=0.25 * self.intensity,
            status=503,
        )

        # Stage 2: appointments degrades 60s later (upstream unavailable)
        if elapsed_s >= 60.0:
            modifier.extra_error_injection(
                service="appointments",
                message=INCIDENT_MESSAGES.get(
                    "upstream_unavailable", "Upstream service unavailable"
                ),
                share=0.20 * self.intensity,
                status=503,
            )

        # Stage 3: pharmacy degrades 120s later (gateway timeout)
        if elapsed_s >= 120.0:
            modifier.extra_error_injection(
                service="pharmacy",
                message=INCIDENT_MESSAGES.get(
                    "gateway_timeout", "Pharmacy formulary gateway timeout after 5000ms"
                ),
                share=0.18 * self.intensity,
                status=504,
            )


# Scenario factory mapping
SCENARIO_CLASSES: Dict[str, type] = {
    "spike": SpikeScenario,
    "drift": DriftScenario,
    "new_error": NewErrorScenario,
    "cascade": CascadeScenario,
}


def create_scenario(
    scenario_type: str,
    service: Optional[str] = None,
    duration_s: Optional[float] = None,
    intensity: float = 1.0,
) -> Scenario:
    """Factory helper to instantiate a Scenario by name."""
    cls = SCENARIO_CLASSES.get(scenario_type.lower())
    if not cls:
        raise ValueError(f"Unknown scenario type: {scenario_type}. Must be one of {list(SCENARIO_CLASSES.keys())}")

    kwargs: Dict[str, Any] = {"intensity": intensity}
    if duration_s is not None:
        kwargs["duration_s"] = duration_s

    if scenario_type.lower() in ("spike", "drift", "new_error") and service is not None:
        kwargs["service"] = service

    return cls(**kwargs)


def build_demo_timeline(seed: int = 42) -> List[Tuple[float, Callable[[], Scenario]]]:
    """Construct the default demo timeline.

    Timeline:
    - 0 to 300s: ~5 min normal baseline traffic
    - 300s: Spike on patient-records (duration: 120s)
    - 480s: Gap of normal traffic (60s)
    - 540s: Drift on patient-records (duration: 480s)
    - 1080s: Gap of normal traffic (60s)
    - 1140s: New error on lab-results (duration: 180s)
    - 1380s: Gap of normal traffic (60s)
    - 1440s: Cascade failure (duration: 360s)

    Jitters onset slightly per seed for natural variability.
    """
    rng = random.Random(seed)

    def jitter(base: float, max_jitter: float = 15.0) -> float:
        return base + rng.uniform(-max_jitter, max_jitter)

    timeline: List[Tuple[float, Callable[[], Scenario]]] = [
        (
            jitter(300.0),
            lambda: SpikeScenario(
                service="patient-records",
                duration_s=120.0,
                intensity=rng.uniform(0.9, 1.1),
            ),
        ),
        (
            jitter(540.0),
            lambda: DriftScenario(
                service="patient-records",
                duration_s=480.0,
                intensity=rng.uniform(0.9, 1.1),
            ),
        ),
        (
            jitter(1140.0),
            lambda: NewErrorScenario(
                service="lab-results",
                duration_s=180.0,
                intensity=rng.uniform(0.9, 1.1),
            ),
        ),
        (
            jitter(1440.0),
            lambda: CascadeScenario(
                duration_s=360.0,
                intensity=rng.uniform(0.9, 1.1),
            ),
        ),
    ]

    # Sort strictly by offset
    timeline.sort(key=lambda item: item[0])
    return timeline
