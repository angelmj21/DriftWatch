"""Log writer for DriftWatch synthetic healthcare generator.

Formats requests to exact specifications, flushes immediately for tailing,
attaches realistic multiline stack traces to ~1/3 of ERROR events,
and supports size- and time-based log rotation.
"""

from datetime import datetime, timezone
import os
from pathlib import Path
import random
from typing import List, Optional, Sequence, Union

try:
    from generator.traffic import Request
except ImportError:
    from traffic import Request  # type: ignore


# Realistic Java-style hospital backend stack trace templates
STACK_TRACE_TEMPLATES = {
    "patient-records": [
        [
            "\tat com.hospital.records.PatientService.getPatient(PatientService.java:142)",
            "\tat com.hospital.records.PatientController.findPatient(PatientController.java:88)",
            "\tat com.hospital.db.ConnectionPool.getConnection(ConnectionPool.java:215)",
            "\tat com.hospital.db.DatabaseDriver.connect(DatabaseDriver.java:64)",
            "\tat com.hospital.db.SocketTimeout.waitForResponse(SocketTimeout.java:91)",
        ],
        [
            "\tat com.hospital.records.PatientRepository.findById(PatientRepository.java:77)",
            "\tat com.hospital.records.PatientService.loadRecord(PatientService.java:105)",
            "\tat com.hospital.orm.EntityManager.find(EntityManager.java:240)",
            "\tat com.hospital.orm.TransactionImpl.execute(TransactionImpl.java:118)",
        ],
    ],
    "lab-results": [
        [
            "\tat com.hospital.lab.HL7MessageParser.parse(HL7MessageParser.java:73)",
            "\tat com.hospital.lab.HL7Validator.validateSegment(HL7Validator.java:45)",
            "\tat com.hospital.lab.ObservationController.ingest(ObservationController.java:112)",
            "\tat com.hospital.common.security.AuthFilter.doFilter(AuthFilter.java:55)",
        ],
        [
            "\tat com.hospital.lab.AnalyzerInterface.readData(AnalyzerInterface.java:89)",
            "\tat com.hospital.lab.SpecimenProcessor.process(SpecimenProcessor.java:134)",
            "\tat com.hospital.lab.ResultService.saveResult(ResultService.java:62)",
            "\tat com.hospital.net.TcpClient.receive(TcpClient.java:178)",
        ],
    ],
    "appointments": [
        [
            "\tat com.hospital.appointments.ScheduleService.reserveSlot(ScheduleService.java:190)",
            "\tat com.hospital.appointments.CalendarLockManager.acquireLock(CalendarLockManager.java:54)",
            "\tat com.hospital.appointments.AppointmentController.book(AppointmentController.java:95)",
            "\tat com.hospital.common.TransactionManager.commit(TransactionManager.java:132)",
        ],
        [
            "\tat com.hospital.appointments.NotificationClient.sendReminder(NotificationClient.java:78)",
            "\tat com.hospital.appointments.AppointmentService.confirm(AppointmentService.java:120)",
            "\tat com.hospital.http.HttpClient.execute(HttpClient.java:204)",
            "\tat com.hospital.http.ConnectionRoute.connect(ConnectionRoute.java:95)",
        ],
    ],
    "pharmacy": [
        [
            "\tat com.hospital.pharmacy.FormularyClient.checkAvailability(FormularyClient.java:108)",
            "\tat com.hospital.pharmacy.PrescriptionValidator.verify(PrescriptionValidator.java:67)",
            "\tat com.hospital.pharmacy.DispenseController.dispense(DispenseController.java:83)",
            "\tat com.hospital.http.HttpClient.execute(HttpClient.java:204)",
        ],
        [
            "\tat com.hospital.pharmacy.InventoryService.deductStock(InventoryService.java:145)",
            "\tat com.hospital.pharmacy.PrescriptionService.fill(PrescriptionService.java:92)",
            "\tat com.hospital.db.TransactionContext.commit(TransactionContext.java:58)",
        ],
    ],
}


def format_request_line(req: Request) -> str:
    """Format a single Request object into the standard DriftWatch log line format.

    Example:
    2026-09-28T10:15:32.412Z INFO  patient-records req=a91f GET /patients/8421 status=200 latency_ms=34
    2026-09-28T10:15:34.870Z ERROR patient-records req=c77d GET /patients/8433 status=500 latency_ms=5002 msg="Database connection timeout after 5000ms"
    """
    # Timestamp formatted as UTC ISO-8601 with 3-digit millisecond precision and Z
    utc_ts = req.ts.astimezone(timezone.utc)
    ts_str = utc_ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    # Level padded to 5 characters (INFO , WARN , ERROR)
    level_str = f"{req.level:<5}"

    # Rounded integer latency as shown in plan section 4
    lat_val = int(round(req.latency_ms))

    line = (
        f"{ts_str} {level_str} {req.service} req={req.req_id} "
        f"{req.method} {req.path} status={req.status} latency_ms={lat_val}"
    )

    if req.msg is not None:
        # Quote and escape backslashes and double quotes in msg
        escaped_msg = (
            req.msg.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\r", " ")
            .replace("\n", " ")
        )
        line += f' msg="{escaped_msg}"'

    return line


class LogWriter:
    """Writes formatted request log lines to disk with auto-flushing, stack traces, and rotation."""

    def __init__(
        self,
        log_path: Union[str, Path] = "./logs/hospital.log",
        max_bytes: Optional[int] = None,
        rotate_interval_s: Optional[float] = None,
        flush_every: int = 1,
        stack_trace_prob: float = 0.333,
        seed: Optional[int] = None,
    ):
        self.log_path = Path(log_path).resolve()
        self.max_bytes = max_bytes
        self.rotate_interval_s = rotate_interval_s
        self.flush_every = max(1, flush_every)
        self.stack_trace_prob = stack_trace_prob
        self.rng = random.Random(seed) if seed is not None else random.Random()

        # Ensure directory exists
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        self._file = None
        self._current_size = 0
        self._last_rotation_time = datetime.now(timezone.utc).timestamp()
        self._writes_since_flush = 0
        self.total_lines_written = 0
        self.total_rotations = 0

        self._open_file()

    def _open_file(self) -> None:
        """Open or reopen the log file in append mode."""
        self._file = open(self.log_path, mode="a", encoding="utf-8", newline="\n")
        self._current_size = self.log_path.stat().st_size if self.log_path.exists() else 0

    def should_rotate(self) -> bool:
        """Check if rotation conditions (size or elapsed time) have been met."""
        if self._current_size <= 0:
            return False

        if self.max_bytes is not None and self._current_size >= self.max_bytes:
            return True

        if self.rotate_interval_s is not None:
            now = datetime.now(timezone.utc).timestamp()
            if (now - self._last_rotation_time) >= self.rotate_interval_s:
                return True

        return False

    def rotate(self) -> None:
        """Perform log rotation: close current file, rename to .1, and start fresh."""
        if self._file:
            self._file.flush()
            self._file.close()
            self._file = None

        backup_path = self.log_path.with_name(f"{self.log_path.name}.1")

        if self.log_path.exists():
            try:
                os.replace(self.log_path, backup_path)
            except OSError:
                if backup_path.exists():
                    backup_path.unlink(missing_ok=True)
                os.rename(self.log_path, backup_path)

        self.total_rotations += 1
        self._last_rotation_time = datetime.now(timezone.utc).timestamp()
        self._open_file()

    def generate_stack_trace(self, service: str) -> List[str]:
        """Generate a realistic 3-6 line tab-indented stack trace."""
        service_templates = STACK_TRACE_TEMPLATES.get(service)
        if service_templates:
            template = self.rng.choice(service_templates)
            # Pick a subset of 3 to 6 frames
            count = min(len(template), self.rng.randint(3, 6))
            return template[:count]
        else:
            return [
                "\tat com.hospital.common.InternalHandler.handle(InternalHandler.java:101)",
                "\tat com.hospital.common.DispatchFilter.doFilter(DispatchFilter.java:62)",
                "\tat com.hospital.http.Server.process(Server.java:210)",
            ]

    def write(self, req: Request) -> List[str]:
        """Format and write a single request (plus stack trace if ERROR), flushing to disk.

        Returns
        -------
        list[str]
            The physical lines written to disk (main line + any stack trace lines).
        """
        if self.should_rotate():
            self.rotate()

        lines_to_write = [format_request_line(req)]

        # Append multiline stack trace to ~1 in 3 ERROR lines
        if req.level == "ERROR" and self.rng.random() < self.stack_trace_prob:
            stack_lines = self.generate_stack_trace(req.service)
            lines_to_write.extend(stack_lines)

        content = "\n".join(lines_to_write) + "\n"
        encoded = content.encode("utf-8")

        self._file.write(content)
        self._current_size += len(encoded)
        self.total_lines_written += len(lines_to_write)
        self._writes_since_flush += 1

        if self._writes_since_flush >= self.flush_every:
            self._file.flush()
            self._writes_since_flush = 0

        return lines_to_write

    def write_batch(self, reqs: Sequence[Request]) -> List[str]:
        """Format and write a batch of requests."""
        all_written: List[str] = []
        for req in reqs:
            all_written.extend(self.write(req))
        return all_written

    def flush(self) -> None:
        """Force flush buffer to disk."""
        if self._file:
            self._file.flush()
            self._writes_since_flush = 0

    def close(self) -> None:
        """Close open file handles."""
        if self._file:
            self._file.flush()
            self._file.close()
            self._file = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
