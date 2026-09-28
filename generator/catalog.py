"""Service catalog and endpoint data tables for the DriftWatch synthetic healthcare generator.

Data tables only: defines the 4 hospital services, endpoints, typical latency profiles,
routine error distributions, and routine & incident error messages.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Endpoint:
    """An HTTP endpoint belonging to a service."""
    method: str
    path_template: str
    weight: float = 1.0
    latency_mean_ms: Optional[float] = None
    latency_std_ms: Optional[float] = None
    success_status: int = 200


@dataclass(frozen=True)
class RoutineErrorDef:
    """A routine error template with status code, message text, and relative weight."""
    status: int
    message: str
    weight: float = 1.0


@dataclass(frozen=True)
class ServiceDef:
    """Definition and normal operating characteristics of a hospital service."""
    name: str
    endpoints: Tuple[Endpoint, ...]
    latency_mean_ms: float
    latency_std_ms: float
    routine_error_rate: float  # Baseline error probability (e.g. 0.015 for ~1.5%)
    routine_errors: Tuple[RoutineErrorDef, ...]


# Baseline service mix distribution (weights across the 4 services)
BASE_SERVICE_WEIGHTS: Dict[str, float] = {
    "patient-records": 0.35,
    "lab-results": 0.25,
    "appointments": 0.20,
    "pharmacy": 0.20,
}

# Incident error messages used during anomalous scenarios
INCIDENT_MESSAGES: Dict[str, str] = {
    "db_timeout": "Database connection timeout after 5000ms",
    "hl7_parse_failure": "HL7 message parse failure: segment PID missing",
    "upstream_unavailable": "Upstream service unavailable",
    "connection_pool_exhausted": "Connection pool exhausted: max active connections (50) reached",
    "circuit_breaker_open": "Circuit breaker OPEN for patient-records backend",
    "lock_timeout": "Failed to acquire lock for schedule reservation after 4000ms",
    "gateway_timeout": "Pharmacy formulary gateway timeout after 5000ms",
}

# The 4 hospital backend services
SERVICES: Dict[str, ServiceDef] = {
    "patient-records": ServiceDef(
        name="patient-records",
        endpoints=(
            Endpoint(method="GET", path_template="/patients/{id}", weight=45.0, latency_mean_ms=32.0, latency_std_ms=8.0),
            Endpoint(method="GET", path_template="/patients/{id}/history", weight=25.0, latency_mean_ms=52.0, latency_std_ms=14.0),
            Endpoint(method="POST", path_template="/patients", weight=10.0, latency_mean_ms=45.0, latency_std_ms=12.0, success_status=201),
            Endpoint(method="PUT", path_template="/patients/{id}", weight=10.0, latency_mean_ms=40.0, latency_std_ms=10.0),
            Endpoint(method="GET", path_template="/patients/search", weight=10.0, latency_mean_ms=60.0, latency_std_ms=18.0),
        ),
        latency_mean_ms=38.0,
        latency_std_ms=12.0,
        routine_error_rate=0.015,
        routine_errors=(
            RoutineErrorDef(status=404, message="Patient record not found: ID does not exist", weight=3.0),
            RoutineErrorDef(status=422, message="Validation failure: Invalid medical record number format", weight=3.0),
            RoutineErrorDef(status=500, message="Database query execution failure: internal query error", weight=4.0),
        ),
    ),
    "lab-results": ServiceDef(
        name="lab-results",
        endpoints=(
            Endpoint(method="GET", path_template="/lab-results/{id}", weight=40.0, latency_mean_ms=42.0, latency_std_ms=10.0),
            Endpoint(method="POST", path_template="/lab-results", weight=30.0, latency_mean_ms=58.0, latency_std_ms=15.0, success_status=201),
            Endpoint(method="GET", path_template="/patients/{id}/lab-results", weight=20.0, latency_mean_ms=70.0, latency_std_ms=20.0),
            Endpoint(method="PUT", path_template="/lab-results/{id}/verify", weight=10.0, latency_mean_ms=48.0, latency_std_ms=12.0),
        ),
        latency_mean_ms=52.0,
        latency_std_ms=16.0,
        routine_error_rate=0.015,
        routine_errors=(
            RoutineErrorDef(status=404, message="Lab order not found for specimen identifier", weight=3.0),
            RoutineErrorDef(status=422, message="Invalid observation code", weight=3.0),
            RoutineErrorDef(status=500, message="Analyzer interface communication failure", weight=4.0),
        ),
    ),
    "appointments": ServiceDef(
        name="appointments",
        endpoints=(
            Endpoint(method="GET", path_template="/appointments/{id}", weight=35.0, latency_mean_ms=28.0, latency_std_ms=7.0),
            Endpoint(method="GET", path_template="/appointments/schedule", weight=30.0, latency_mean_ms=45.0, latency_std_ms=12.0),
            Endpoint(method="POST", path_template="/appointments", weight=20.0, latency_mean_ms=48.0, latency_std_ms=14.0, success_status=201),
            Endpoint(method="PUT", path_template="/appointments/{id}/reschedule", weight=10.0, latency_mean_ms=42.0, latency_std_ms=10.0),
            Endpoint(method="DELETE", path_template="/appointments/{id}", weight=5.0, latency_mean_ms=26.0, latency_std_ms=6.0),
        ),
        latency_mean_ms=36.0,
        latency_std_ms=10.0,
        routine_error_rate=0.015,
        routine_errors=(
            RoutineErrorDef(status=404, message="Appointment not found for specified identifier", weight=3.0),
            RoutineErrorDef(status=422, message="Scheduling conflict: Selected time slot is already booked", weight=3.0),
            RoutineErrorDef(status=500, message="Failed to commit schedule reservation transaction", weight=4.0),
        ),
    ),
    "pharmacy": ServiceDef(
        name="pharmacy",
        endpoints=(
            Endpoint(method="GET", path_template="/prescriptions/{id}", weight=40.0, latency_mean_ms=30.0, latency_std_ms=8.0),
            Endpoint(method="POST", path_template="/prescriptions", weight=25.0, latency_mean_ms=46.0, latency_std_ms=12.0, success_status=201),
            Endpoint(method="GET", path_template="/prescriptions/active", weight=20.0, latency_mean_ms=50.0, latency_std_ms=14.0),
            Endpoint(method="PUT", path_template="/prescriptions/{id}/dispense", weight=15.0, latency_mean_ms=44.0, latency_std_ms=11.0),
        ),
        latency_mean_ms=40.0,
        latency_std_ms=12.0,
        routine_error_rate=0.015,
        routine_errors=(
            RoutineErrorDef(status=404, message="Prescription not found in pharmacy inventory", weight=3.0),
            RoutineErrorDef(status=422, message="Prescription validation failed: dosage out of safety bounds", weight=3.0),
            RoutineErrorDef(status=500, message="Formulary verification service timeout", weight=4.0),
        ),
    ),
}
