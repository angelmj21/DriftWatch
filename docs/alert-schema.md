# Alert and live-metric schema (v1, FROZEN)

Source of truth: `backend/app/alerts/schema.py`. Changes only by a PR approved by all four, announced in the group first.
Transport (WebSocket envelope, REST endpoints): see [interfaces.md](interfaces.md).

Conventions: timestamps are ISO-8601 UTC with milliseconds and `Z`. Rates are **percent 0-100** (`14.2` = 14.2%). Unknown fields are rejected.

## Alert
Pushed on open, update and resolve. All three carry the same `id`; the frontend keeps the latest message per `id`.

| Field | Type | Unit / values | Nullable | Meaning |
|---|---|---|---|---|
| `id` | string | - | no | Incident id, same for the whole lifecycle |
| `status` | string | `open` \| `updated` \| `resolved` | no | Lifecycle step |
| `severity` | string | `INFO` \| `WARN` \| `CRITICAL` | no | Never decreases within an incident |
| `services` | string[] | service names | no (>=1) | Affected services (a cascade grows this list) |
| `metric` | string | `error_rate` | no | Metric that triggered the incident |
| `observed` | number | percent | no | Current error rate |
| `baseline` | number | percent | no | Learned normal (alerts only exist after learning) |
| `deviation` | number | sigma (robust z-score) | no | How far observed is from baseline |
| `slope` | number | percentage points / minute | no | Growth speed (negative = recovering) |
| `top_signatures` | object[] | see below | no (may be `[]`) | Most frequent error types now |
| `new_signature` | boolean | - | no | True if a never-seen error type triggered/joined this incident |
| `explanation` | string | plain language | no | Why this alert fired, from real numbers |
| `started_at` | string | ISO-8601 UTC | no | Incident start |
| `updated_at` | string | ISO-8601 UTC | no | Time of this message |
| `resolved_at` | string | ISO-8601 UTC | **yes** | `null` unless `status == "resolved"` (then required) |

`top_signatures[]` item: `signature` (string, stable id), `template` (string, readable masked message), `count` (int, occurrences in the short window).

### Example: open
```json
{"id": "inc-a91f3c", "status": "open", "severity": "WARN", "services": ["patient-records"], "metric": "error_rate", "observed": 14.2, "baseline": 2.1, "deviation": 6.2, "slope": 0.8, "top_signatures": [{"signature": "3f9a1c7b2e", "template": "Database connection timeout after <N>ms", "count": 41}], "new_signature": false, "explanation": "Error rate on patient-records is 14.2% vs learned baseline 2.1% (6.2 sigma), rising 0.8 pts/min since 10:15:32. Top cause: 'Database connection timeout after <N>ms' (83% of errors).", "started_at": "2026-09-28T10:15:32.412Z", "updated_at": "2026-09-28T10:15:32.412Z", "resolved_at": null}
```

### Example: updated
```json
{"id": "inc-a91f3c", "status": "updated", "severity": "CRITICAL", "services": ["patient-records", "appointments"], "metric": "error_rate", "observed": 27.9, "baseline": 2.1, "deviation": 13.2, "slope": 4.1, "top_signatures": [{"signature": "3f9a1c7b2e", "template": "Database connection timeout after <N>ms", "count": 96}, {"signature": "b07d44e1a9", "template": "Upstream service unavailable", "count": 22}], "new_signature": false, "explanation": "Error rate across patient-records and appointments is 27.9% vs learned baseline 2.1% (13.2 sigma), rising 4.1 pts/min since 10:15:32. Top cause: 'Database connection timeout after <N>ms' (81% of errors).", "started_at": "2026-09-28T10:15:32.412Z", "updated_at": "2026-09-28T10:17:02.118Z", "resolved_at": null}
```

### Example: resolved
```json
{"id": "inc-a91f3c", "status": "resolved", "severity": "CRITICAL", "services": ["patient-records", "appointments"], "metric": "error_rate", "observed": 2.3, "baseline": 2.1, "deviation": 0.1, "slope": -3.2, "top_signatures": [], "new_signature": false, "explanation": "Incident resolved: error rate is back to 2.3% (baseline 2.1%). Lasted 4m 15s, peaked at CRITICAL.", "started_at": "2026-09-28T10:15:32.412Z", "updated_at": "2026-09-28T10:19:47.905Z", "resolved_at": "2026-09-28T10:19:47.905Z"}
```

## LiveMetric
Sent about once a second as WebSocket `metric` messages and served by `GET /metrics`.

| Field | Type | Unit / values | Nullable | Meaning |
|---|---|---|---|---|
| `timestamp` | string | ISO-8601 UTC | no | Tick time |
| `error_rate` | number | percent, 0-100 | no | Error rate over the 1-minute window |
| `baseline` | number | percent | **yes** | **`null` while `state == "learning"`**, required otherwise |
| `band_low` | number | percent | **yes** | Same rule as `baseline` |
| `band_high` | number | percent | **yes** | Same rule as `baseline` |
| `state` | string | `learning` \| `normal` \| `anomaly` | no | Backend pipeline state |

The backend never invents a baseline before one is learned: while `learning`, the three fields are `null` and the chart should show a "Learning" overlay.

### Example: learning
```json
{"timestamp": "2026-09-28T10:14:05.000Z", "error_rate": 1.8, "baseline": null, "band_low": null, "band_high": null, "state": "learning"}
```

### Example: normal
```json
{"timestamp": "2026-09-28T10:15:01.000Z", "error_rate": 2.4, "baseline": 2.1, "band_low": 0.0, "band_high": 7.95, "state": "normal"}
```

### Example: anomaly
```json
{"timestamp": "2026-09-28T10:15:40.000Z", "error_rate": 14.2, "baseline": 2.1, "band_low": 0.0, "band_high": 7.95, "state": "anomaly"}
```

## WebSocket envelope
Each message on `ws://localhost:8000/ws` is `{"type": "metric" | "alert" | "signatures" | "heartbeat", "data": ...}`. `metric` carries a LiveMetric and `alert` carries an Alert. Full details in [interfaces.md](interfaces.md).