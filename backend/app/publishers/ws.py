"""WebSocket broadcaster and in-memory store for DriftWatch.

Exposes /ws APIRouter, manages client connections, handles heartbeats,
and stores recent metrics, alerts, signatures, and system state for REST fallback.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Literal, Optional, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

# Import settings with graceful fallback
try:
    from backend.app.config import settings
except ImportError:
    try:
        from app.config import settings  # type: ignore
    except ImportError:
        settings = None

logger = logging.getLogger(__name__)

# Configurable constants / defaults
_WS_HEARTBEAT_S: float = getattr(settings, "ws_heartbeat_s", 10.0) if settings else 10.0
_SEND_TIMEOUT_S: float = 2.0
_MAX_METRICS: int = 1800
_MAX_ALERTS: int = 500
_INITIAL_METRICS_ON_CONNECT: int = 60


def _utc_now_iso() -> str:
    """Return current UTC time in ISO-8601 format with milliseconds and Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class WSPublisher:
    """Broadcaster for live WebSocket clients with in-memory store for REST."""

    def __init__(
        self,
        heartbeat_interval_s: float = _WS_HEARTBEAT_S,
        send_timeout_s: float = _SEND_TIMEOUT_S,
        max_metrics: int = _MAX_METRICS,
        max_alerts: int = _MAX_ALERTS,
        initial_metrics_on_connect: int = _INITIAL_METRICS_ON_CONNECT,
    ):
        self.heartbeat_interval_s = heartbeat_interval_s
        self.send_timeout_s = send_timeout_s
        self.initial_metrics_on_connect = initial_metrics_on_connect

        # Connected WebSocket clients
        self.active_connections: Set[WebSocket] = set()

        # In-memory stores
        # Bounded store of last ~1800 metrics (oldest first in deque)
        self.metrics: deque[Dict[str, Any]] = deque(maxlen=max_metrics)
        # Bounded store of last ~500 alerts (oldest first in deque)
        self.alerts: deque[Dict[str, Any]] = deque(maxlen=max_alerts)
        # Latest signatures payload: {"top": [...], "new": [...]}
        self.latest_signatures: Dict[str, Any] = {"top": [], "new": []}

        # System state & counters
        self.state: Literal["learning", "normal", "anomaly"] = "learning"
        self.lines_processed: int = 0
        self.start_time: datetime = datetime.now(timezone.utc)

        # Background heartbeat task
        self._heartbeat_task: Optional[asyncio.Task] = None

        # Router
        self.router = APIRouter()
        self._register_routes()

    def _register_routes(self) -> None:
        """Register the /ws WebSocket endpoint on the router."""

        @self.router.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await self.connect(websocket)
            try:
                while True:
                    # Keep connection open; receive text / ping / close frames
                    # Client may send ping or ignore incoming text
                    data = await websocket.receive_text()
                    # If client sends a ping or message, we ignore or log debug
            except WebSocketDisconnect:
                self.disconnect(websocket)
            except Exception as e:
                logger.debug(f"WebSocket client error: {e}")
                self.disconnect(websocket)

    async def start(self) -> None:
        """Start the background heartbeat task if not already running."""
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        """Cancel heartbeat task and disconnect active clients."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        # Close all active connections
        for ws in list(self.active_connections):
            try:
                await ws.close()
            except Exception:
                pass
        self.active_connections.clear()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept a new WebSocket connection and send initial metrics."""
        await websocket.accept()
        self.active_connections.add(websocket)

        # Start heartbeat loop if not started
        await self.start()

        # Step 4: Send the last few metrics to the client right after it connects
        # so charts are not empty on refresh
        if self.metrics:
            recent_metrics = list(self.metrics)[-self.initial_metrics_on_connect :]
            for m in recent_metrics:
                envelope = {"type": "metric", "data": m}
                try:
                    await asyncio.wait_for(
                        websocket.send_text(json.dumps(envelope)),
                        timeout=self.send_timeout_s,
                    )
                except Exception as e:
                    logger.debug(f"Failed to send initial metrics to new client: {e}")
                    self.disconnect(websocket)
                    break

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a disconnected client from the active set."""
        self.active_connections.discard(websocket)

    def _normalize_payload(self, data: Any) -> Dict[str, Any]:
        """Convert Pydantic models or dicts to json-compatible dict."""
        if hasattr(data, "to_dict") and callable(data.to_dict):
            return data.to_dict()
        elif hasattr(data, "model_dump") and callable(data.model_dump):
            return data.model_dump(mode="json")
        elif isinstance(data, dict):
            return data
        else:
            return dict(data)

    def update_state(self, state: Literal["learning", "normal", "anomaly"]) -> None:
        """Update current pipeline state."""
        self.state = state

    def record_lines(self, count: int = 1) -> None:
        """Increment processed line counter."""
        self.lines_processed += count

    async def broadcast(self, msg_type: str, data: Any) -> None:
        """Broadcast message envelope to all active clients and update in-memory stores.

        Envelope format: {"type": msg_type, "data": payload}
        Drops dead clients without breaking others.
        """
        payload = self._normalize_payload(data)

        # Update in-memory stores based on message type
        if msg_type == "metric":
            self.metrics.append(payload)
            if "state" in payload:
                self.state = payload["state"]
        elif msg_type == "alert":
            self.alerts.append(payload)
        elif msg_type == "signatures":
            self.latest_signatures = payload

        if not self.active_connections:
            return

        envelope = {"type": msg_type, "data": payload}
        msg_str = json.dumps(envelope)

        # Broadcast to all connected clients with timeout
        dead_clients: List[WebSocket] = []

        async def _send(ws: WebSocket):
            try:
                await asyncio.wait_for(
                    ws.send_text(msg_str),
                    timeout=self.send_timeout_s,
                )
            except Exception as e:
                logger.debug(f"Dropping client during broadcast: {e}")
                dead_clients.append(ws)

        await asyncio.gather(*(_send(ws) for ws in list(self.active_connections)))

        for dead in dead_clients:
            self.disconnect(dead)

    async def _heartbeat_loop(self) -> None:
        """Periodic heartbeat broadcast every settings.ws_heartbeat_s."""
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval_s)
                heartbeat_payload = {"ts": _utc_now_iso()}
                envelope = {"type": "heartbeat", "data": heartbeat_payload}
                msg_str = json.dumps(envelope)

                dead_clients: List[WebSocket] = []

                async def _send(ws: WebSocket):
                    try:
                        await asyncio.wait_for(
                            ws.send_text(msg_str),
                            timeout=self.send_timeout_s,
                        )
                    except Exception:
                        dead_clients.append(ws)

                if self.active_connections:
                    await asyncio.gather(*(_send(ws) for ws in list(self.active_connections)))
                    for dead in dead_clients:
                        self.disconnect(dead)
        except asyncio.CancelledError:
            pass


# Singleton instance for application-wide use
ws_publisher = WSPublisher()
router = ws_publisher.router
