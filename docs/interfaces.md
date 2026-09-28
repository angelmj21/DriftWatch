# Module interfaces (v1, frozen)

This is the contract between people's modules. Build to these signatures exactly so parts plug together without rework.
Change it only via a PR approved by all four (see CONTRIBUTING.md rule 5).

## Conventions
- Python timestamps: timezone-aware UTC `datetime`. JSON timestamps: ISO-8601 with `Z` (e.g. `2026-09-28T10:15:32.412Z`).
- `error_rate` is a **percent, 0-100 float** (14.2 means 14.2%). Rate = ERROR-level lines / all lines in the window.
- Log levels: `INFO | WARN | ERROR` (2xx / 4xx / 5xx). Only `ERROR` counts as an error for the error rate.
- Services: `patient-records`, `lab-results`, `appointments`, `pharmacy`.
- Window lengths and all tunables come from `config.py` (`settings.window_short_s=60`, `settings.window_long_s=300`). No magic numbers in modules.
- Generator and engine share **no** constants or code.

## Pipeline
```
generator ──(log file)──► tailer ► parser ► signature ─┬► window ► baseline ► detector ─┐
                                                      └► signature_tracker ─────────────┤
                                                                 severity ◄────────────┘
                                                                    ▼
                                                              alert manager ─► ws / sns / cloudwatch
                                                                    ▼
                                             REST (/alerts /metrics /signatures /health) + WebSocket ─► React
```

## Ingest (owner Joshua)
```python
# ingest/tailer.py
class LogTailer:
    def __init__(self, path: str, state_path: str | None = None): ...
    async def lines(self) -> AsyncIterator[str]:  # yields raw physical lines, no trailing newline

# ingest/parser.py
@dataclass
class LogEvent:
    ts: datetime              # tz-aware UTC
    level: str                # INFO | WARN | ERROR
    service: str
    req_id: str
    method: str | None
    path: str | None
    status: int | None
    latency_ms: float | None
    message: str | None       # msg="..." text, None if absent
    stack: list[str]          # continuation (stack trace) lines, [] if none
class LogParser:
    def feed(self, raw_line: str) -> list[LogEvent]   # returns events that are now complete (multiline grouped)
    def flush(self) -> list[LogEvent]                 # emit a pending event (call on idle)
    bad_lines: int                                    # counter of unparseable lines (never raises)

# ingest/signature.py
def make_signature(event: LogEvent) -> tuple[str, str]:
    """returns (signature_id, masked_template). IDs, numbers, hex, paths masked.
    signature_id = short stable hash (e.g. first 10 hex of sha1) of the template."""
```

## Engine
```python
# engine/window.py (Joshua)
@dataclass
class WindowSnapshot:
    ts: datetime
    total_1m: int; errors_1m: int; error_rate_1m: float      # percent
    total_5m: int; errors_5m: int; error_rate_5m: float
    per_service_errors_1m: dict[str, int]
    per_service_rate_1m: dict[str, float]                    # percent, per service
class SlidingWindow:
    def add(self, ev: LogEvent) -> None                      # per-second buckets, allowed lateness from config
    def snapshot(self, now: datetime) -> WindowSnapshot

# engine/baseline.py (Angel)
@dataclass
class BaselineState:
    baseline: float; scale: float        # robust centre (median/EWMA) and MAD-based scale, percent
    band_low: float; band_high: float
    learning: bool                       # True until enough samples (settings.baseline_min_samples)
    frozen: bool                         # True while an anomaly is active
class AdaptiveBaseline:
    def update(self, snap: WindowSnapshot, anomaly_active: bool) -> BaselineState

# engine/detector.py (Angel)
@dataclass
class Detection:
    active: bool                         # after hysteresis
    observed: float; baseline: float     # percent
    deviation: float                     # robust z-score
    slope: float                         # percent per minute
    services: list[str]                  # services contributing (per-service rate above their share)
class Detector:
    def evaluate(self, snap: WindowSnapshot, base: BaselineState) -> Detection

# engine/signature_tracker.py (Angel)
@dataclass
class SigCount: signature: str; template: str; count: int
@dataclass
class SigFinding:
    new_signatures: list[SigCount]       # never seen before (after learning period)
    shifted: list[SigCount]              # share jumped versus its own history
class SignatureTracker:
    def observe(self, ts: datetime, signature: str, template: str, service: str) -> None
    def top(self, n: int = 5) -> list[SigCount]              # recent window, for UI + alerts
    def check(self, now: datetime) -> SigFinding

# engine/severity.py (Angel)
@dataclass
class Severity: level: str; score: float                     # level INFO | WARN | CRITICAL
def score(det: Detection, duration_s: float, sig_variety: int,
          services: list[str]) -> Severity                   # criticality table lives in config.py
```

## Alerts (Angel)
```python
# alerts/schema.py  -> pydantic models Alert and LiveMetric, exactly as docs/alert-schema.md
# alerts/manager.py
class AlertManager:
    def process(self, now: datetime, det: Detection, sev: Severity,
                sigs: SigFinding, top: list[SigCount]) -> list[Alert]
    # returns 0..n Alert messages to publish this tick (open / updated / resolved);
    # dedupe + cooldown + hysteresis live here. Returns [] when nothing changed.
```

## Publishers
```python
# publishers/ws.py (Joshua)
class WSPublisher:
    async def broadcast(self, msg_type: str, data: dict) -> None   # envelope below
# publishers/sns.py, publishers/cloudwatch.py (Angel)
class SNSPublisher:        async def publish(self, alert: Alert) -> None    # never raises into the pipeline
class CloudWatchPublisher: async def publish(self, alert: Alert) -> None    # never raises into the pipeline
```

## WebSocket envelope (Joshua sends, Ayush consumes)
Endpoint `ws://localhost:8000/ws`. Every message is JSON:
```json
{"type": "metric | alert | signatures | heartbeat", "data": { } }
```
- `metric`     -> `LiveMetric` (about every 1 s)
- `alert`      -> `Alert` (on open, update, resolve)
- `signatures` -> `{"top": [{"signature","template","count"}], "new": ["<signature>", ...]}` (about every 5 s)
- `heartbeat`  -> `{"ts": "..."}` (every 10 s; client treats 25 s of silence as disconnected)

## REST polling fallback (Joshua serves, Ayush consumes) - base `http://localhost:8000`
| Endpoint | Returns |
|---|---|
| `GET /health` | `{"status":"ok","state":"learning|normal|anomaly","lines_processed":N,"uptime_s":N}` |
| `GET /metrics?limit=300` | last N `LiveMetric`, oldest first |
| `GET /alerts?limit=50&status=` | `Alert` messages, newest first; optional status filter |
| `GET /signatures` | same payload as the WebSocket `signatures` message |
CORS must allow `http://localhost:5173`.

## Generator control API (Joshua serves, Gautham consumes) - base `http://localhost:8001`
| Endpoint | Body / returns |
|---|---|
| `GET /scenarios` | `["spike","drift","new_error","cascade"]` |
| `POST /scenario` | body `{"type":"spike","service":"patient-records","duration_s":120}` (`service`, `duration_s` optional) -> `{"started":true,"id":"...","type":"..."}` |
| `GET /status` | `{"running":bool,"active":[{"id","type","service","started_at"}]}` |
CORS must allow `http://localhost:5173`.

## Files on disk
- Log file: `settings.log_path` - format exactly as section 4 of the plan.
- Labels file: `settings.labels_path`, JSON lines: `{"id","type","services":[...],"start":"<iso>","end":"<iso>"}`. The engine never reads it; only `benchmarks/` does.
