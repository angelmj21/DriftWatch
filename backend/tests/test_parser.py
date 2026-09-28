"""Edge-case tests for the streaming log parser."""
from datetime import timezone

import pytest

from backend.app.ingest.parser import LogParser


def test_header_parses_fields_and_escaped_message_quotes():
    parser = LogParser()
    line = r'2026-09-28T10:15:32.412Z ERROR patient-records req=c77d GET /patients/8433 status=500 latency_ms=5002 msg="HL7 segment \"PID\" rejected"'

    assert parser.feed(line) == []
    events = parser.flush()

    assert len(events) == 1
    event = events[0]
    assert event.ts.tzinfo == timezone.utc
    assert event.level == "ERROR"
    assert event.service == "patient-records"
    assert event.req_id == "c77d"
    assert event.method == "GET"
    assert event.path == "/patients/8433"
    assert event.status == 500
    assert event.latency_ms == 5002.0
    assert event.message == 'HL7 segment "PID" rejected'


def test_error_stack_lines_are_grouped_into_one_event():
    parser = LogParser()
    header = (
        '2026-09-28T10:15:34.870Z ERROR patient-records req=c77d GET '
        '/patients/8433 status=500 latency_ms=5002 msg="Database timeout"'
    )
    next_header = (
        "2026-09-28T10:15:35.000Z INFO lab-results req=b22c POST "
        "/results status=200 latency_ms=51"
    )

    assert parser.feed(header) == []
    assert parser.feed("    at patient.records.Repository.load(Repository.py:42)") == []
    assert parser.feed("Caused by: connection refused") == []
    first = parser.feed(next_header)
    last = parser.flush()

    assert len(first) == 1
    assert first[0].message == "Database timeout"
    assert first[0].stack == [
        "    at patient.records.Repository.load(Repository.py:42)",
        "Caused by: connection refused",
    ]
    assert len(last) == 1
    assert last[0].level == "INFO"


@pytest.mark.xfail(strict=True, reason="Parser currently requires latency_ms although LogEvent declares it optional; report to Joshua on issue #8.")
def test_missing_latency_is_parsed_as_none():
    parser = LogParser()
    line = (
        "2026-09-28T10:15:36.000Z WARN lab-results req=b22c POST "
        '/results status=422 msg="Invalid observation code"'
    )

    parser.feed(line)
    events = parser.flush()

    assert len(events) == 1
    assert events[0].latency_ms is None


def test_garbage_lines_increment_counter_without_raising():
    parser = LogParser()

    assert parser.feed("not a log line") == []
    assert parser.feed("2026-09-28Tbroken ERROR patient-records") == []
    assert parser.flush() == []
    assert parser.bad_lines == 2


def test_out_of_order_timestamps_are_parsed_without_reordering():
    parser = LogParser()
    later = (
        "2026-09-28T10:15:38.000Z INFO patient-records req=a1 GET "
        "/patients status=200 latency_ms=20"
    )
    earlier = (
        "2026-09-28T10:15:37.000Z INFO patient-records req=a2 GET "
        "/patients status=200 latency_ms=21"
    )

    emitted = parser.feed(later) + parser.feed(earlier) + parser.flush()

    assert [event.req_id for event in emitted] == ["a1", "a2"]
    assert emitted[0].ts > emitted[1].ts