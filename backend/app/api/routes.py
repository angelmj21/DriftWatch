"""REST polling fallback endpoints for DriftWatch.

Endpoints:
- GET /health: Status, pipeline state, lines processed, uptime
- GET /metrics?limit=300: Recent LiveMetric items, oldest first
- GET /alerts?limit=50&status=: Alert messages, newest first with optional status filter
- GET /signatures: Top and new signatures payload
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Query

from backend.app.publishers.ws import ws_publisher

router = APIRouter()


@router.get("/health")
async def get_health() -> Dict[str, Any]:
    """Health status and general telemetry."""
    uptime_s = (datetime.now(timezone.utc) - ws_publisher.start_time).total_seconds()
    return {
        "status": "ok",
        "state": ws_publisher.state,
        "lines_processed": ws_publisher.lines_processed,
        "uptime_s": round(uptime_s, 2),
    }


@router.get("/metrics")
async def get_metrics(
    limit: int = Query(default=300, ge=1, le=1800, description="Max metrics to return (oldest first)")
) -> List[Dict[str, Any]]:
    """Return the last N metrics, ordered oldest first."""
    all_metrics = list(ws_publisher.metrics)
    if not all_metrics:
        return []
    # Deque stores oldest first. Slice the tail of size `limit`.
    return all_metrics[-limit:]


@router.get("/alerts")
async def get_alerts(
    limit: int = Query(default=50, ge=1, le=500, description="Max alerts to return (newest first)"),
    status: Optional[Literal["open", "updated", "resolved"]] = Query(
        default=None, description="Optional status filter"
    ),
) -> List[Dict[str, Any]]:
    """Return alert messages, ordered newest first with optional status filter."""
    all_alerts = list(ws_publisher.alerts)
    if not all_alerts:
        return []

    # Filter by status if provided
    if status is not None:
        filtered = [a for a in all_alerts if a.get("status") == status]
    else:
        filtered = all_alerts

    # Newest first means reverse order of insertion
    reversed_alerts = list(reversed(filtered))
    return reversed_alerts[:limit]


@router.get("/signatures")
async def get_signatures() -> Dict[str, Any]:
    """Return latest top and new signatures payload."""
    return ws_publisher.latest_signatures
