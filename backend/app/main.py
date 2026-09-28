"""Main FastAPI application and full pipeline orchestrator for DriftWatch (v2 wiring).

Complete live pipeline:
Ingest: LogTailer -> LogParser -> make_signature -> tracker.observe -> SlidingWindow
Tick Loop (every settings.ws_metric_interval_s):
  snap = window.snapshot(now)
  base = baseline.update(snap, anomaly_active)
  det = detector.evaluate(snap, base)
  sigs = tracker.check(now)
  sev = severity.score(det, duration_s, sig_variety, services)
  alerts = manager.process(now, det, sev, sigs, top)
  publishers: ws.broadcast('alert', ...), SNS, CloudWatch
  ws.broadcast('metric', LiveMetric)
  ws.broadcast('signatures', ...) every ~5s
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.alerts.schema import Alert, LiveMetric
from backend.app.api.routes import router as api_router
from backend.app.config import settings
from backend.app.engine.baseline import AdaptiveBaseline, BaselineState
from backend.app.engine.window import SlidingWindow, WindowSnapshot
from backend.app.ingest.parser import LogEvent, LogParser
from backend.app.ingest.signature import make_signature
from backend.app.ingest.tailer import LogTailer
from backend.app.publishers.ws import router as ws_router, ws_publisher

# Dynamic imports for components that may be merged concurrently by Angel
try:
    from backend.app.engine.detector import Detector, Detection
except ImportError:
    Detector = None  # type: ignore
    Detection = None  # type: ignore

try:
    from backend.app.engine.signature_tracker import SignatureTracker, SigCount, SigFinding
except ImportError:
    SignatureTracker = None  # type: ignore
    SigCount = None  # type: ignore
    SigFinding = None  # type: ignore

try:
    from backend.app.engine.severity import Severity, score as score_severity
except ImportError:
    Severity = None  # type: ignore
    score_severity = None  # type: ignore

try:
    from backend.app.alerts.manager import AlertManager
except ImportError:
    AlertManager = None  # type: ignore

try:
    from backend.app.publishers.sns import SNSPublisher
except ImportError:
    SNSPublisher = None  # type: ignore

try:
    from backend.app.publishers.cloudwatch import CloudWatchPublisher
except ImportError:
    CloudWatchPublisher = None  # type: ignore

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("driftwatch.backend")

# Global pipeline instances
window = SlidingWindow(
    window_short_s=settings.window_short_s,
    window_long_s=settings.window_long_s,
    allowed_lateness_s=settings.allowed_lateness_s,
)
parser = LogParser()
tailer = LogTailer(
    path=settings.log_path,
    poll_interval_s=settings.tailer_poll_interval_s,
)
baseline = AdaptiveBaseline()

# Optional stages instantiated if class is available
detector = Detector() if Detector is not None else None
sig_tracker = SignatureTracker() if SignatureTracker is not None else None
alert_manager = AlertManager() if AlertManager is not None else None
sns_publisher = SNSPublisher() if SNSPublisher is not None else None
cloudwatch_publisher = CloudWatchPublisher() if CloudWatchPublisher is not None else None

# Pipeline state tracking
_tailer_task: Optional[asyncio.Task] = None
_tick_task: Optional[asyncio.Task] = None
_anomaly_active: bool = False
_anomaly_started_at: Optional[datetime] = None
_last_sig_broadcast: float = 0.0


async def run_pipeline():
    """Streaming ingestion pipeline: LogTailer -> LogParser -> make_signature -> tracker.observe -> window."""
    logger.info(f"Starting log tailer pipeline following {settings.log_path}")
    last_line_time = asyncio.get_event_loop().time()

    try:
        async for line in tailer.lines():
            now_loop = asyncio.get_event_loop().time()
            # If no line arrived for ~1s, flush any pending multiline event
            if (now_loop - last_line_time) > 1.0:
                try:
                    for flushed_ev in parser.flush():
                        _process_event(flushed_ev)
                except Exception as e:
                    logger.error(f"Error flushing parser on idle: {e}")

            last_line_time = now_loop

            try:
                events = parser.feed(line)
                for ev in events:
                    _process_event(ev)
            except Exception as e:
                logger.error(f"Error parsing line: {e}")
    except asyncio.CancelledError:
        logger.info("Pipeline ingest task cancelled.")
    except Exception as e:
        logger.critical(f"Unexpected pipeline ingest error: {e}", exc_info=True)
    finally:
        try:
            for flushed_ev in parser.flush():
                _process_event(flushed_ev)
        except Exception:
            pass


def _process_event(ev: LogEvent):
    """Process a single parsed LogEvent through signatures, tracker, and window."""
    try:
        ws_publisher.record_lines(1)
        # Compute signature
        if ev.level == "ERROR":
            sig_id, template = make_signature(ev)
            # Feed tracker if available
            if sig_tracker is not None:
                try:
                    sig_tracker.observe(
                        ts=ev.ts,
                        signature=sig_id,
                        template=template,
                        service=ev.service,
                    )
                except Exception as e:
                    logger.debug(f"Error in signature tracker observe: {e}")

        # Feed to sliding window
        window.add(ev)
    except Exception as e:
        logger.error(f"Error processing event {ev.req_id}: {e}")


async def _publish_alert(alert: Alert) -> None:
    """Publish alert to WebSocket immediately and dispatch SNS/CloudWatch concurrently with timeouts."""
    # 1. Immediate broadcast to connected WebSocket clients and local REST store
    try:
        await ws_publisher.broadcast("alert", alert)
    except Exception as e:
        logger.error(f"Error broadcasting alert to WebSocket: {e}")

    # 2. Fire and forget SNS / CloudWatch publishing (never block the tick loop)
    async def _safe_publish(pub: Any, name: str):
        if pub is None:
            return
        try:
            # Per contract: publisher timeout to avoid blocking or hanging
            await asyncio.wait_for(pub.publish(alert), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(f"{name} publisher timed out")
        except Exception as e:
            logger.warning(f"{name} publisher error: {e}")

    asyncio.create_task(_safe_publish(sns_publisher, "SNS"))
    asyncio.create_task(_safe_publish(cloudwatch_publisher, "CloudWatch"))


async def run_tick_loop():
    """Main tick loop: executes full detection and publishing pipeline every ws_metric_interval_s."""
    global _anomaly_active, _anomaly_started_at, _last_sig_broadcast
    logger.info(f"Starting main tick loop (interval={settings.ws_metric_interval_s}s)")

    try:
        while True:
            await asyncio.sleep(settings.ws_metric_interval_s)
            now_utc = datetime.now(timezone.utc)
            loop_time = asyncio.get_event_loop().time()

            try:
                # Stage 1: Window snapshot
                snap: WindowSnapshot = window.snapshot(now_utc)

                # Stage 2: Adaptive baseline update
                base_state: BaselineState = baseline.update(snap, anomaly_active=_anomaly_active)

                # Stage 3: Detector evaluation
                det = None
                if detector is not None:
                    try:
                        det = detector.evaluate(snap, base_state)
                        _anomaly_active = det.active
                    except Exception as e:
                        logger.error(f"Detector evaluation error: {e}")
                else:
                    _anomaly_active = False

                # Track anomaly duration
                if _anomaly_active:
                    if _anomaly_started_at is None:
                        _anomaly_started_at = now_utc
                    duration_s = (now_utc - _anomaly_started_at).total_seconds()
                else:
                    _anomaly_started_at = None
                    duration_s = 0.0

                # Stage 4: Signature tracker check
                sigs = None
                top_sigs = []
                if sig_tracker is not None:
                    try:
                        top_sigs = sig_tracker.top(n=5)
                        sigs = sig_tracker.check(now_utc)
                    except Exception as e:
                        logger.error(f"Signature tracker check error: {e}")

                # Stage 5: Severity scoring
                sev = None
                if det is not None and score_severity is not None:
                    try:
                        sig_variety = len(top_sigs)
                        sev = score_severity(
                            det=det,
                            duration_s=duration_s,
                            sig_variety=sig_variety,
                            services=det.services,
                        )
                    except Exception as e:
                        logger.error(f"Severity scoring error: {e}")

                # Stage 6: Alert manager processing
                if alert_manager is not None and det is not None and sev is not None:
                    try:
                        generated_alerts = alert_manager.process(
                            now=now_utc,
                            det=det,
                            sev=sev,
                            sigs=sigs,
                            top=top_sigs,
                        )
                        for alert in generated_alerts:
                            await _publish_alert(alert)
                    except Exception as e:
                        logger.error(f"Alert manager processing error: {e}")

                # Stage 7: Determine pipeline state for LiveMetric
                if base_state.learning:
                    cur_state = "learning"
                    b_val = None
                    low_val = None
                    high_val = None
                elif _anomaly_active:
                    cur_state = "anomaly"
                    b_val = base_state.baseline
                    low_val = base_state.band_low
                    high_val = base_state.band_high
                else:
                    cur_state = "normal"
                    b_val = base_state.baseline
                    low_val = base_state.band_low
                    high_val = base_state.band_high

                ws_publisher.update_state(cur_state)

                # Stage 8: Broadcast LiveMetric
                metric = LiveMetric(
                    timestamp=snap.ts,
                    error_rate=snap.error_rate_1m,
                    baseline=b_val,
                    band_low=low_val,
                    band_high=high_val,
                    state=cur_state,
                )
                await ws_publisher.broadcast("metric", metric)

                # Stage 9: Periodic signatures broadcast (~every 5s)
                if (loop_time - _last_sig_broadcast) >= 5.0:
                    _last_sig_broadcast = loop_time
                    top_list = (
                        [
                            {"signature": s.signature, "template": s.template, "count": s.count}
                            for s in top_sigs
                        ]
                        if top_sigs
                        else []
                    )
                    new_list = [s.signature for s in sigs.new_signatures] if sigs and getattr(sigs, "new_signatures", None) else []
                    sig_payload = {"top": top_list, "new": new_list}
                    await ws_publisher.broadcast("signatures", sig_payload)

            except Exception as e:
                logger.error(f"Error in pipeline tick: {e}", exc_info=True)

    except asyncio.CancelledError:
        logger.info("Main tick loop cancelled.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager to start and stop background tasks cleanly."""
    global _tailer_task, _tick_task

    # Ensure log file directory exists
    log_dir = Path(settings.log_path).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    # Start WebSocket publisher
    await ws_publisher.start()

    # Start background ingestion and tick loops
    _tailer_task = asyncio.create_task(run_pipeline())
    _tick_task = asyncio.create_task(run_tick_loop())

    yield

    # Shutdown: cancel and await tasks
    logger.info("Shutting down DriftWatch pipeline...")
    tailer.stop()

    for task in (_tailer_task, _tick_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # Flush CloudWatch publisher queue on shutdown if available
    if cloudwatch_publisher is not None and hasattr(cloudwatch_publisher, "flush"):
        try:
            if asyncio.iscoroutinefunction(cloudwatch_publisher.flush):
                await cloudwatch_publisher.flush()
            else:
                cloudwatch_publisher.flush()
        except Exception as e:
            logger.warning(f"Error flushing CloudWatch publisher on shutdown: {e}")

    await ws_publisher.stop()
    logger.info("DriftWatch pipeline stopped cleanly.")


app = FastAPI(
    title="DriftWatch Backend API",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS middleware for frontend (http://localhost:5173)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount WebSocket and REST routers
app.include_router(ws_router)
app.include_router(api_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.app.main:app", host="0.0.0.0", port=settings.api_port, reload=False)
