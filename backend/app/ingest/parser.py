"""Log event parser for DriftWatch synthetic healthcare backend.

Parses raw log lines into LogEvent objects, groups multiline stack traces
with their parent ERROR events, tracks bad lines gracefully without raising,
and extracts structured fields at high throughput (>20k lines/s).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import List, Optional

# Compiled regex matching the standard DriftWatch log header line:
# 2026-09-28T10:15:32.412Z INFO  patient-records req=a91f GET /patients/8421 status=200 latency_ms=34
# 2026-09-28T10:15:33.101Z WARN  lab-results req=b22c POST /lab-results status=422 latency_ms=51 msg="Invalid observation code"
# 2026-09-28T10:15:34.870Z ERROR patient-records req=c77d GET /patients/8433 status=500 latency_ms=5002 msg="Database connection timeout after 5000ms"
HEADER_PATTERN = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))"
    r"\s+(?P<level>[A-Z]+)"
    r"\s+(?P<service>[a-zA-Z0-9_\-]+)"
    r"\s+req=(?P<req_id>[a-zA-Z0-9]+)"
    r"\s+(?P<method>[A-Z]+)"
    r"\s+(?P<path>\S+)"
    r"\s+status=(?P<status>\d+)"
    r"\s+latency_ms=(?P<latency_ms>\d+(?:\.\d+)?)"
    r'(?:\s+msg="(?P<msg>(?:\\.|[^"\\])*)")?'
    r"\s*$"
)

# Unescape regex for escaped characters in msg="..."
UNESCAPE_PATTERN = re.compile(r"\\(.)")


@dataclass
class LogEvent:
    """Represents a structured parsed log event, matching docs/interfaces.md."""
    ts: datetime  # tz-aware UTC
    level: str  # INFO | WARN | ERROR
    service: str
    req_id: str
    method: Optional[str]
    path: Optional[str]
    status: Optional[int]
    latency_ms: Optional[float]
    message: Optional[str]  # msg="..." text, None if absent
    stack: List[str] = field(default_factory=list)  # continuation stack lines


class LogParser:
    """Robust streaming log parser with multiline stack trace grouping."""

    def __init__(self):
        self.bad_lines: int = 0
        self._pending_event: Optional[LogEvent] = None

    def feed(self, raw_line: str) -> List[LogEvent]:
        """Feed a single raw physical line to the parser.

        Returns
        -------
        list[LogEvent]
            Any events that are now complete. When a header line is followed by
            continuation lines (stack traces), the event is held until the next
            header line arrives or flush() is called.
        """
        # Strip trailing newline and carriage return, preserving leading indentation
        line = raw_line.rstrip("\r\n")

        # Empty lines
        if not line:
            return []

        # Header lines start with a 4-digit year timestamp (e.g. 2026-...)
        # A quick slice check is significantly faster than regex on continuation lines
        if len(line) >= 10 and line[0:4].isdigit() and line[4] == "-":
            # This is a header line candidates
            emitted: List[LogEvent] = []
            if self._pending_event is not None:
                emitted.append(self._pending_event)
                self._pending_event = None

            match = HEADER_PATTERN.match(line)
            if match is None:
                self.bad_lines += 1
                return emitted

            try:
                ts_raw = match.group("ts")
                if ts_raw.endswith("Z"):
                    ts = datetime.fromisoformat(ts_raw[:-1] + "+00:00")
                else:
                    ts = datetime.fromisoformat(ts_raw)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    else:
                        ts = ts.astimezone(timezone.utc)

                level = match.group("level")
                service = match.group("service")
                req_id = match.group("req_id")
                method = match.group("method")
                path = match.group("path")
                status = int(match.group("status"))
                latency_ms = float(match.group("latency_ms"))

                raw_msg = match.group("msg")
                if raw_msg is not None:
                    message = UNESCAPE_PATTERN.sub(r"\1", raw_msg)
                else:
                    message = None

                self._pending_event = LogEvent(
                    ts=ts,
                    level=level,
                    service=service,
                    req_id=req_id,
                    method=method,
                    path=path,
                    status=status,
                    latency_ms=latency_ms,
                    message=message,
                    stack=[],
                )
            except Exception:
                self.bad_lines += 1

            return emitted

        else:
            # Continuation line (e.g. \tat com.hospital...)
            if self._pending_event is not None:
                self._pending_event.stack.append(line)
            else:
                # Orphan continuation line with no preceding header
                self.bad_lines += 1
            return []

    def flush(self) -> List[LogEvent]:
        """Emit any pending grouped event (call on idle or stream end)."""
        if self._pending_event is not None:
            ev = self._pending_event
            self._pending_event = None
            return [ev]
        return []
