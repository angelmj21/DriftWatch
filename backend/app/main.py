"""Main FastAPI application and pipeline orchestrator for DriftWatch (v1 wiring).

Wires together:
LogTailer -> LogParser -> make_signature -> SlidingWindow -> WSPublisher.
Broadcasts LiveMetric every settings.ws_metric_interval_s.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.alerts.schema import LiveMetric
from backend.app.config import settings
from backend.app.engine.window import SlidingWindow, WindowSnapshot
from backend.app.ingest.parser import LogEvent, LogParser
from backend.app.ingest.signature import make_signature
from backend.app.ingest.tailer import LogTailer
from backend.app.api.routes import router as api_router
from backend.app.publishers.ws import router as ws_router, ws_publisher

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

# Pipeline tasks
_tailer_task: Optional[asyncio.Task] = None
_metric_broadcast_task: Optional[asyncio.Task] = None


async def run_pipeline():
    """Streaming pipeline: LogTailer -> LogParser -> make_signature -> SlidingWindow."""
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
        logger.info("Pipeline task cancelled.")
    except Exception as e:
        logger.critical(f"Unexpected pipeline error: {e}", exc_info=True)
    finally:
        # Final flush on shutdown
        try:
            for flushed_ev in parser.flush():
                _process_event(flushed_ev)
        except Exception:
            pass


def _process_event(ev: LogEvent):
    """Process a single parsed LogEvent through signatures and window."""
    try:
        ws_publisher.record_lines(1)
        # Compute signature (useful for tracker in v2, and verifies error signature logic)
        if ev.level == "ERROR":
            sig_id, template = make_signature(ev)
            # In v2, signature tracker is updated here

        # Feed to sliding window
        window.add(ev)
    except Exception as e:
        logger.error(f"Error processing event {ev.req_id}: {e}")


async def run_metric_broadcaster():
    """Periodic task: snapshot sliding window -> LiveMetric -> WebSocket broadcast."""
    logger.info(
        f"Starting LiveMetric broadcaster every {settings.ws_metric_interval_s}s"
    )
    try:
        while True:
            await asyncio.sleep(settings.ws_metric_interval_s)
            try:
                # Use pipeline watermark if available, else current UTC time
                now_utc = datetime.now(timezone.utc)
                snap: WindowSnapshot = window.snapshot(now_utc)

                # Build LiveMetric wire contract:
                # State is learning until Angel's baseline lands.
                # baseline, band_low, band_high must be None when state == "learning".
                metric = LiveMetric(
                    timestamp=snap.ts,
                    error_rate=snap.error_rate_1m,
                    baseline=None,
                    band_low=None,
                    band_high=None,
                    state="learning",
                )

                # Broadcast metric to all WebSocket clients (also updates ws_publisher in-memory store)
                await ws_publisher.broadcast("metric", metric)
            except Exception as e:
                logger.error(f"Error generating or broadcasting metric: {e}")
    except asyncio.CancelledError:
        logger.info("Metric broadcaster task cancelled.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager to start and stop background tasks cleanly."""
    global _tailer_task, _metric_broadcast_task

    # Ensure log file directory exists so tailer can poll cleanly
    log_dir = Path(settings.log_path).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    # Start WebSocket publisher internal tasks (e.g. heartbeat)
    await ws_publisher.start()

    # Start background ingestion and broadcasting tasks
    _tailer_task = asyncio.create_task(run_pipeline())
    _metric_broadcast_task = asyncio.create_task(run_metric_broadcaster())

    yield

    # Shutdown: cancel and await tasks
    logger.info("Shutting down DriftWatch pipeline...")
    tailer.stop()

    for task in (_tailer_task, _metric_broadcast_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    await ws_publisher.stop()
    logger.info("DriftWatch pipeline stopped cleanly.")


app = FastAPI(
    title="DriftWatch Backend API",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware for frontend (http://localhost:5173)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
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
