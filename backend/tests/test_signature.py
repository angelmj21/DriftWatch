"""Stability and masking tests for make_signature."""
from datetime import datetime, timezone

from backend.app.ingest.parser import LogEvent
from backend.app.ingest.signature import make_signature


def _event(message: str) -> LogEvent:
    return LogEvent(
        ts=datetime(2026, 9, 28, tzinfo=timezone.utc),
        level="ERROR",
        service="patient-records",
        req_id="request1",
        method="GET",
        path="/patients/8421",
        status=500,
        latency_ms=10.0,
        message=message,
        stack=[],
    )


def test_numbers_and_ids_are_masked_to_same_signature():
    first = make_signature(_event("Patient 8421 request c77d timed out after 5000ms"))
    second = make_signature(_event("Patient 9123 request a91f timed out after 6200ms"))

    assert first[0] == second[0]
    assert first[1] == "Patient <ID> request <ID> timed out after <N>"
    assert second[1] == first[1]


def test_distinct_error_templates_have_distinct_signatures():
    database_error = make_signature(_event("Database connection timeout"))
    hl7_error = make_signature(_event("HL7 observation code rejected"))

    assert database_error[0] != hl7_error[0]
    assert database_error[1] != hl7_error[1]


def test_signature_is_deterministic():
    event = _event("Upstream service unavailable for request b07d")

    assert make_signature(event) == make_signature(event)