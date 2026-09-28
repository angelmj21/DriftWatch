"""Central settings. Every module imports `settings` from here: no hardcoded thresholds,
paths or ARNs anywhere else. Every field can be overridden by a DW_* env var (or .env).
Percent values are 0-100. AWS credentials are NOT stored here (boto3 default chain)."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND = Path(__file__).resolve().parents[1]
_ROOT = _BACKEND.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DW_",
        env_file=(_ROOT / ".env", _BACKEND / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",  # .env also holds VITE_* and AWS_* keys
    )

    # ---- paths ----
    log_path: str = "./logs/hospital.log"          # log file the tailer follows
    labels_path: str = "./logs/labels.jsonl"       # ground truth; only benchmarks/ reads it

    # ---- windows (seconds) ----
    window_short_s: int = 60                       # short window for the live error rate
    window_long_s: int = 300                       # long window (5 min)
    allowed_lateness_s: float = 5.0                # late-line tolerance vs watermark

    # ---- ingest / streaming ----
    tailer_poll_interval_s: float = 0.2            # tailer polling interval
    ws_metric_interval_s: float = 1.0              # LiveMetric broadcast period
    ws_heartbeat_s: float = 10.0                   # WebSocket heartbeat period
    api_port: int = 8000                           # backend API + WebSocket port
    control_port: int = 8001                       # generator control API port

    # ---- baseline ----
    sensitivity: float = 3.0                       # k: band = baseline +/- k * scale
    baseline_min_samples: int = 120                # ticks before leaving 'learning'
    baseline_history_len: int = 900                # history length in ticks (15 min at 1 Hz)
    ewma_alpha: float = 0.05                       # EWMA weight for slow drift following
    min_scale: float = 0.5                         # floor on scale (percent) to avoid div-by-zero
    min_events_per_window: int = 20                # ignore samples with less traffic than this
    freeze_max_s: int = 1800                       # max baseline freeze during an anomaly

    # ---- detector ----
    z_on: float = 4.0                              # robust z to start an anomaly (spike)
    z_soft: float = 2.5                            # softer z when slope also rising (drift)
    z_off: float = 1.5                             # robust z to end an anomaly
    slope_on: float = 0.5                          # pct-points per minute that counts as rising
    on_ticks: int = 3                              # consecutive ticks above z_on to trigger
    off_ticks: int = 10                            # consecutive ticks below z_off to clear
    slope_window_s: int = 120                      # window for the slope regression

    # ---- signature tracker ----
    sig_min_count: int = 3                         # min occurrences to report a new signature
    sig_shift_ratio: float = 5.0                   # short share / long-term share to flag a shift

    # ---- severity ----
    service_criticality: dict[str, float] = Field(default_factory=lambda: {
        "patient-records": 1.0, "pharmacy": 0.9, "lab-results": 0.8, "appointments": 0.5,
    })
    sev_w_deviation: float = 0.30                  # severity weights (sum ~1.0)
    sev_w_duration: float = 0.15
    sev_w_slope: float = 0.20
    sev_w_variety: float = 0.10
    sev_w_criticality: float = 0.25
    sev_warn_score: float = 0.35                   # score >= this is WARN
    sev_critical_score: float = 0.65               # score >= this is CRITICAL

    # ---- alert manager ----
    cooldown_s: int = 120                          # re-trigger within this merges into old incident
    update_interval_s: int = 15                    # min seconds between 'updated' alerts
    resolve_after_s: int = 30                      # inactive this long -> resolved

    # ---- AWS ----
    sns_min_severity: Literal["INFO", "WARN", "CRITICAL"] = "WARN"
    aws_region: str = "ap-south-1"
    sns_topic_arn: str = ""                        # empty -> SNS publisher no-ops with a warning
    cw_log_group: str = "/driftwatch/alerts"
    cw_metric_namespace: str = "DriftWatch"


settings = Settings()