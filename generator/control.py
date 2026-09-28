"""Generator control API for DriftWatch on-demand incident triggering.

Exposes endpoints on port 8001 (or configured port):
- GET /scenarios: ["spike", "drift", "new_error", "cascade"]
- POST /scenario: {"type":"spike", "service":"patient-records", "duration_s":120}
- GET /status: {"running": bool, "active": [{"id", "type", "service", "started_at"}]}

Enables dashboard "Simulate incident" buttons to inject real scenarios into the running generator.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Callable, Dict, List, Optional
import uuid

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    from generator.catalog import SERVICES
    from generator.scenarios import (
        SCENARIO_CLASSES,
        Scenario,
        create_scenario,
    )
except ImportError:
    from catalog import SERVICES  # type: ignore
    from scenarios import (  # type: ignore
        SCENARIO_CLASSES,
        Scenario,
        create_scenario,
    )

logger = logging.getLogger("driftwatch.generator.control")


class ScenarioRequest(BaseModel):
    type: str = Field(description="Scenario type: spike | drift | new_error | cascade")
    service: Optional[str] = Field(default=None, description="Target service (optional)")
    duration_s: Optional[float] = Field(default=120.0, ge=5.0, le=3600.0, description="Duration in seconds")


class ActiveScenarioInfo(BaseModel):
    id: str
    type: str
    service: Optional[str] = None
    started_at: str


class ControlStatusResponse(BaseModel):
    running: bool
    active: List[ActiveScenarioInfo]


class ScenarioManager:
    """Manages active scenarios triggered either via control API or timeline."""

    def __init__(self):
        self.active_scenarios: Dict[str, Scenario] = {}
        self.is_running: bool = True
        self._on_start_cb: Optional[Callable[[Scenario], None]] = None

    def register_start_callback(self, cb: Callable[[Scenario], None]) -> None:
        self._on_start_cb = cb

    def start_scenario(
        self,
        scenario_type: str,
        service: Optional[str] = None,
        duration_s: float = 120.0,
    ) -> Scenario:
        stype = scenario_type.lower()
        if stype not in SCENARIO_CLASSES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown scenario type '{scenario_type}'. Allowed: {list(SCENARIO_CLASSES.keys())}",
            )

        if service is not None and service not in SERVICES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown service '{service}'. Allowed: {list(SERVICES.keys())}",
            )

        # Reject duplicate active scenario of same type + service with 409 Conflict
        for active in self.active_scenarios.values():
            active_svc = getattr(active, "service", None)
            if active.scenario_type == stype and active_svc == service:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"A scenario of type '{stype}' for service '{service}' is already active.",
                )

        scenario = create_scenario(
            scenario_type=stype,
            service=service,
            duration_s=duration_s,
        )

        self.active_scenarios[scenario.id] = scenario
        if self._on_start_cb:
            self._on_start_cb(scenario)

        return scenario

    def remove_scenario(self, scenario_id: str) -> None:
        self.active_scenarios.pop(scenario_id, None)


def create_control_app(manager: ScenarioManager) -> FastAPI:
    """Create and configure the FastAPI control application."""
    app = FastAPI(
        title="DriftWatch Generator Control API",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/scenarios", response_model=List[str])
    def get_scenarios():
        """List available scenario types."""
        return ["spike", "drift", "new_error", "cascade"]

    @app.post("/scenario")
    def post_scenario(req: ScenarioRequest):
        """Trigger an on-demand incident scenario."""
        scenario = manager.start_scenario(
            scenario_type=req.type,
            service=req.service,
            duration_s=req.duration_s if req.duration_s is not None else 120.0,
        )
        return {
            "started": True,
            "id": scenario.id,
            "type": scenario.scenario_type,
            "service": getattr(scenario, "service", None),
            "duration_s": scenario.duration_s,
        }

    @app.get("/status", response_model=ControlStatusResponse)
    def get_status():
        """Return running state and currently active scenarios."""
        active_list: List[ActiveScenarioInfo] = []
        for s in manager.active_scenarios.values():
            start_iso = (
                s.start_time.isoformat(timespec="milliseconds").replace("+00:00", "Z")
                if s.start_time
                else datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            )
            active_list.append(
                ActiveScenarioInfo(
                    id=s.id,
                    type=s.scenario_type,
                    service=getattr(s, "service", None),
                    started_at=start_iso,
                )
            )
        return ControlStatusResponse(running=manager.is_running, active=active_list)

    return app
