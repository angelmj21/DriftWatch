"""Realistic traffic generator for DriftWatch synthetic healthcare backend.

Generates realistic request traffic across 4 hospital services:
- Dynamic request rate (base + diurnal sine wave + slow wander + noise + microbursts)
- Slowly drifting service mix
- Uncorrelated, stochastic routine error trickle (~0.5 - 2% overall error rate)
- Modifier hook for scenario simulations (error_share_override, latency_multiplier, extra_error_injection)
- Deterministic output given the same seed
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
import random
from typing import Dict, List, Optional, Union

try:
    from generator.catalog import (
        BASE_SERVICE_WEIGHTS,
        INCIDENT_MESSAGES,
        SERVICES,
        Endpoint,
        RoutineErrorDef,
        ServiceDef,
    )
except ImportError:
    from catalog import (  # type: ignore
        BASE_SERVICE_WEIGHTS,
        INCIDENT_MESSAGES,
        SERVICES,
        Endpoint,
        RoutineErrorDef,
        ServiceDef,
    )


@dataclass
class Request:
    """Represents a single HTTP request in the simulated hospital backend."""
    ts: datetime
    service: str
    method: str
    path: str
    status: int
    latency_ms: float
    msg: Optional[str]
    req_id: str

    @property
    def level(self) -> str:
        """Log level following the project plan: 2xx INFO, 4xx WARN, 5xx ERROR."""
        if 200 <= self.status < 300:
            return "INFO"
        elif 400 <= self.status < 500:
            return "WARN"
        elif 500 <= self.status < 600:
            return "ERROR"
        return "INFO"


@dataclass
class ErrorInjection:
    """An injected error definition for a specific service."""
    service: str
    message: str
    share: float  # Fraction of service requests to fail with this error (0.0 to 1.0)
    status: int = 500


@dataclass
class TrafficModifier:
    """Plain data structure holding runtime modifier hooks for scenarios.

    No scenario logic is stored here; only modifier overrides.
    """
    error_share_override: Dict[str, float] = field(default_factory=dict)
    latency_multiplier: Union[Dict[str, float], float] = field(default_factory=dict)
    extra_injections: List[ErrorInjection] = field(default_factory=list)

    def extra_error_injection(
        self, service: str, message: str, share: float, status: int = 500
    ) -> None:
        """Inject or update an extra error pattern for a specific service."""
        for inj in self.extra_injections:
            if inj.service == service and inj.message == message:
                inj.share = share
                inj.status = status
                return
        self.extra_injections.append(
            ErrorInjection(service=service, message=message, share=share, status=status)
        )

    def remove_error_injection(
        self, service: str, message: Optional[str] = None
    ) -> None:
        """Remove error injection(s) for a service."""
        if message is None:
            self.extra_injections = [
                inj for inj in self.extra_injections if inj.service != service
            ]
        else:
            self.extra_injections = [
                inj
                for inj in self.extra_injections
                if not (inj.service == service and inj.message == message)
            ]

    def clear(self) -> None:
        """Clear all active modifications."""
        if isinstance(self.latency_multiplier, dict):
            self.latency_multiplier.clear()
        else:
            self.latency_multiplier = {}
        self.error_share_override.clear()
        self.extra_injections.clear()


class TrafficModel:
    """Realistic hospital traffic model with dynamic request rate, service mix, and errors."""

    def __init__(self, seed: Optional[int] = None, base_rate: float = 20.0):
        self.seed = seed if seed is not None else 42
        self.rng = random.Random(self.seed)
        self.base_rate = base_rate

        # Rate dynamics state
        self.wander: float = 0.0
        self.burst_remaining_s: float = 0.0
        self.burst_intensity: float = 0.0

        # Slowly drifting service mix weights
        self.service_weights: Dict[str, float] = dict(BASE_SERVICE_WEIGHTS)

        # Modifier hook for scenarios
        self.modifier: TrafficModifier = TrafficModifier()

    def set_modifier(self, modifier: TrafficModifier) -> None:
        """Replace the active traffic modifier."""
        self.modifier = modifier

    def reset_modifier(self) -> None:
        """Reset all active modifiers back to normal traffic."""
        self.modifier.clear()

    def extra_error_injection(
        self, service: str, message: str, share: float, status: int = 500
    ) -> None:
        """Helper to register an extra error injection on the current modifier."""
        self.modifier.extra_error_injection(service, message, share, status)

    def _sample_poisson(self, lam: float) -> int:
        """Sample from Poisson distribution using the internal seeded RNG."""
        if lam <= 0:
            return 0
        if lam < 30.0:
            limit = math.exp(-lam)
            k = 0
            prod = 1.0
            while prod > limit:
                k += 1
                prod *= self.rng.random()
            return k - 1
        else:
            # Gaussian approximation for large lambda
            sampled = int(round(self.rng.gauss(lam, math.sqrt(lam))))
            return max(0, sampled)

    def _get_latency_multiplier(self, service: str) -> float:
        """Compute latency multiplier for a given service."""
        mult_conf = self.modifier.latency_multiplier
        if isinstance(mult_conf, (int, float)):
            return float(mult_conf)
        if isinstance(mult_conf, dict):
            if service in mult_conf:
                return float(mult_conf[service])
            if "*" in mult_conf:
                return float(mult_conf["*"])
        return 1.0

    def _calc_latency(
        self, service_def: ServiceDef, endpoint: Endpoint, multiplier: float = 1.0
    ) -> float:
        """Calculate realistic request latency in ms."""
        mean = (
            endpoint.latency_mean_ms
            if endpoint.latency_mean_ms is not None
            else service_def.latency_mean_ms
        )
        std = (
            endpoint.latency_std_ms
            if endpoint.latency_std_ms is not None
            else service_def.latency_std_ms
        )
        sampled = self.rng.gauss(mean, std)
        return max(4.0, sampled) * multiplier

    def _check_injections(self, service: str) -> Optional[ErrorInjection]:
        """Check if an injected error triggers for this request."""
        for inj in self.modifier.extra_injections:
            if inj.service == service:
                if self.rng.random() < inj.share:
                    return inj
        return None

    def next_requests(
        self, sim_time: Union[datetime, float, int], dt: float
    ) -> List[Request]:
        """Advance simulation by dt seconds and generate requests within [sim_time, sim_time + dt).

        Parameters
        ----------
        sim_time : datetime or float
            Current simulation timestamp. Converted to tz-aware UTC if datetime or unix timestamp.
        dt : float
            Simulation step interval in seconds.

        Returns
        -------
        list[Request]
            List of requests generated in the time slice, sorted chronologically.
        """
        if dt <= 0:
            return []

        # Ensure tz-aware UTC datetime
        if isinstance(sim_time, (int, float)):
            cur_time = datetime.fromtimestamp(sim_time, timezone.utc)
        elif isinstance(sim_time, datetime):
            if sim_time.tzinfo is None:
                cur_time = sim_time.replace(tzinfo=timezone.utc)
            else:
                cur_time = sim_time
        else:
            raise TypeError(f"Unsupported sim_time type: {type(sim_time)}")

        # 1. Base rate
        base = self.base_rate

        # 2. Daily-ish sine wave (24-hour diurnal cycle)
        sec_of_day = (
            cur_time.hour * 3600.0
            + cur_time.minute * 60.0
            + cur_time.second
            + cur_time.microsecond * 1e-6
        )
        daily_sine = 5.0 * math.sin(
            2.0 * math.pi * (sec_of_day - 8.0 * 3600.0) / 86400.0
        )

        # 3. Slow wander (Ornstein-Uhlenbeck mean-reverting drift)
        dt_clamped = min(dt, 10.0)
        wander_pull = -0.015 * self.wander * dt_clamped
        wander_noise = self.rng.gauss(0.0, 0.4 * math.sqrt(max(0.001, dt_clamped)))
        self.wander = max(-6.0, min(6.0, self.wander + wander_pull + wander_noise))

        # 4. Occasional short micro-bursts (1-3 seconds, +8 to +16 rps)
        if self.burst_remaining_s > 0:
            burst_rate = self.burst_intensity
            self.burst_remaining_s = max(0.0, self.burst_remaining_s - dt)
        else:
            burst_rate = 0.0
            burst_prob = 1.0 - math.exp(-0.02 * dt)
            if self.rng.random() < burst_prob:
                self.burst_remaining_s = self.rng.uniform(1.0, 3.0)
                self.burst_intensity = self.rng.uniform(8.0, 16.0)
                burst_rate = self.burst_intensity

        # 5. Gaussian noise
        noise = self.rng.gauss(0.0, 1.2)

        rate = max(3.0, base + daily_sine + self.wander + burst_rate + noise)

        # Slowly update service mix weights
        for s_name, base_w in BASE_SERVICE_WEIGHTS.items():
            drift = -0.01 * (self.service_weights[s_name] - base_w) * dt_clamped
            jitter = self.rng.gauss(0.0, 0.004 * math.sqrt(max(0.001, dt_clamped)))
            self.service_weights[s_name] = max(
                0.05, self.service_weights[s_name] + drift + jitter
            )

        # Sample total request count in this dt window
        expected_requests = rate * dt
        count = self._sample_poisson(expected_requests)
        if count <= 0:
            return []

        # Generate timestamps distributed within [sim_time, sim_time + dt)
        offsets = [self.rng.uniform(0.0, dt) for _ in range(count)]
        offsets.sort()

        service_names = list(SERVICES.keys())
        service_weights = [self.service_weights[s] for s in service_names]

        requests: List[Request] = []
        for offset in offsets:
            req_ts = cur_time + timedelta(seconds=offset)
            service_name = self.rng.choices(
                service_names, weights=service_weights, k=1
            )[0]
            service_def = SERVICES[service_name]

            # Choose endpoint according to weights
            endpoint = self.rng.choices(
                service_def.endpoints,
                weights=[ep.weight for ep in service_def.endpoints],
                k=1,
            )[0]

            # Substitute path template
            if "{id}" in endpoint.path_template:
                item_id = self.rng.randint(1000, 9999)
                path = endpoint.path_template.format(id=item_id)
            else:
                path = endpoint.path_template

            # Generate 4-hex digit request ID (req=a91f)
            req_id = f"{self.rng.randint(0, 0xffff):04x}"

            lat_mult = self._get_latency_multiplier(service_name)

            # 1. Injected errors take priority
            injected = self._check_injections(service_name)
            if injected is not None:
                status = injected.status
                msg = injected.message
                if "timeout" in msg.lower() or "5000ms" in msg:
                    latency_ms = (5000.0 + self.rng.uniform(2.0, 120.0)) * lat_mult
                else:
                    latency_ms = self._calc_latency(service_def, endpoint, lat_mult)
                requests.append(
                    Request(
                        ts=req_ts,
                        service=service_name,
                        method=endpoint.method,
                        path=path,
                        status=status,
                        latency_ms=round(latency_ms, 1),
                        msg=msg,
                        req_id=req_id,
                    )
                )
                continue

            # 2. Check error_share_override or routine error
            if service_name in self.modifier.error_share_override:
                error_prob = self.modifier.error_share_override[service_name]
            else:
                error_prob = service_def.routine_error_rate

            if self.rng.random() < error_prob:
                routine_err = self.rng.choices(
                    service_def.routine_errors,
                    weights=[re.weight for re in service_def.routine_errors],
                    k=1,
                )[0]
                status = routine_err.status
                msg = routine_err.message
                if status == 500 and ("timeout" in msg.lower() or "5000ms" in msg):
                    latency_ms = (5000.0 + self.rng.uniform(2.0, 120.0)) * lat_mult
                else:
                    latency_ms = self._calc_latency(service_def, endpoint, lat_mult)
            else:
                status = endpoint.success_status
                msg = None
                latency_ms = self._calc_latency(service_def, endpoint, lat_mult)

            requests.append(
                Request(
                    ts=req_ts,
                    service=service_name,
                    method=endpoint.method,
                    path=path,
                    status=status,
                    latency_ms=round(latency_ms, 1),
                    msg=msg,
                    req_id=req_id,
                )
            )

        return requests
