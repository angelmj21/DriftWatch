"""DriftWatch Synthetic Healthcare Log Generator CLI.

Usage Examples:
---------------
1. Run default demo generator with control server and timeline:
   python -m generator.run --timeline demo --seed 42

2. Fast offline benchmark replay (speed 30x, duration 1800s, no control server):
   python -m generator.run --speed 30 --duration 1800 --no-control --timeline demo

3. Clean production simulation writing to custom paths:
   python -m generator.run --log-path ./logs/hospital.log --labels-path ./logs/labels.jsonl

Flags:
------
--speed: Speed multiplier (simulated seconds per wall-clock second).
--seed: Random seed for deterministic replay.
--timeline: 'demo', 'none', or path to JSON file specifying timeline.
--log-path: Output destination for the raw log stream.
--labels-path: Output destination for ground-truth JSON lines incident labels.
--control-port: Port for HTTP control server (default 8001).
--no-control: Disable the HTTP control server.
--duration: Stop after N simulated seconds (or wall-clock seconds if specified).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

try:
    from generator.control import ScenarioManager, create_control_app
    from generator.labels import LabelWriter
    from generator.scenarios import (
        CascadeScenario,
        DriftScenario,
        NewErrorScenario,
        Scenario,
        SpikeScenario,
        build_demo_timeline,
        create_scenario,
    )
    from generator.traffic import TrafficModel, TrafficModifier
    from generator.writer import LogWriter
except ImportError:
    from control import ScenarioManager, create_control_app  # type: ignore
    from labels import LabelWriter  # type: ignore
    from scenarios import (  # type: ignore
        CascadeScenario,
        DriftScenario,
        NewErrorScenario,
        Scenario,
        SpikeScenario,
        build_demo_timeline,
        create_scenario,
    )
    from traffic import TrafficModel, TrafficModifier  # type: ignore
    from writer import LogWriter  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("driftwatch.generator")


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="DriftWatch Synthetic Healthcare Log Generator",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Speed multiplier: N makes simulated time advance N times faster than wall clock",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for deterministic log generation",
    )
    parser.add_argument(
        "--timeline",
        type=str,
        default="demo",
        help="Timeline: 'demo', 'none', or path to JSON timeline definition file",
    )
    parser.add_argument(
        "--log-path",
        type=str,
        default="./logs/hospital.log",
        help="Destination path for the growing log file",
    )
    parser.add_argument(
        "--labels-path",
        type=str,
        default="./logs/labels.jsonl",
        help="Destination path for ground-truth JSON lines incident labels",
    )
    parser.add_argument(
        "--control-port",
        type=int,
        default=8001,
        help="Port for HTTP control server",
    )
    parser.add_argument(
        "--no-control",
        action="store_true",
        default=False,
        help="Disable the HTTP control server",
    )
    parser.add_argument(
        "--rotate-bytes",
        type=int,
        default=None,
        help="Maximum file size in bytes before rotating (rename to .1)",
    )
    parser.add_argument(
        "--rotate-interval",
        type=float,
        default=None,
        help="Maximum interval in seconds before rotating (rename to .1)",
    )
    parser.add_argument(
        "--tick",
        type=float,
        default=0.25,
        help="Wall-clock sleep interval per simulation step in seconds",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Stop after N simulated seconds (useful for benchmarks)",
    )
    return parser.parse_args(argv)


class ControlServerThread:
    """Runs FastAPI uvicorn server in a background daemon thread."""

    def __init__(self, app: Any, host: str = "0.0.0.0", port: int = 8001):
        import uvicorn

        self.server = uvicorn.Server(
            uvicorn.Config(app=app, host=host, port=port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=2.0)


def load_timeline(
    timeline_arg: str, seed: int
) -> List[Tuple[float, Callable[[], Scenario]]]:
    """Parse timeline argument ('demo', 'none', or json file path)."""
    if timeline_arg.lower() == "none":
        return []
    elif timeline_arg.lower() == "demo":
        return build_demo_timeline(seed=seed)
    else:
        # Load custom JSON file
        p = Path(timeline_arg)
        if not p.exists():
            raise FileNotFoundError(f"Timeline definition file not found: {timeline_arg}")
        with open(p, "r", encoding="utf-8") as f:
            items = json.load(f)

        timeline = []
        for item in items:
            offset = float(item["offset_s"])
            stype = item["type"]
            svc = item.get("service")
            dur = item.get("duration_s", 120.0)
            intensity = item.get("intensity", 1.0)
            timeline.append(
                (
                    offset,
                    lambda st=stype, sv=svc, d=dur, inte=intensity: create_scenario(
                        st, service=sv, duration_s=d, intensity=inte
                    ),
                )
            )
        timeline.sort(key=lambda x: x[0])
        return timeline


def run_generator(args: argparse.Namespace) -> int:
    """Run the main log generation loop."""
    log_file = Path(args.log_path).resolve()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    labels_file = Path(args.labels_path).resolve()
    labels_file.parent.mkdir(parents=True, exist_ok=True)

    speed = max(0.001, args.speed)
    tick = max(0.01, args.tick)

    # Initialize components
    model = TrafficModel(seed=args.seed)
    writer = LogWriter(
        log_path=log_file,
        max_bytes=args.rotate_bytes,
        rotate_interval_s=args.rotate_interval,
        flush_every=1,
        seed=args.seed,
    )
    labels = LabelWriter(path=labels_file)
    scenario_mgr = ScenarioManager()

    # Callback when a scenario starts via control API
    def on_control_scenario_start(scenario: Scenario):
        scenario.start(sim_time)
        labels.start(
            incident_id=scenario.id,
            incident_type=scenario.scenario_type,
            services=scenario.services,
            ts=sim_time,
        )
        logger.info(
            f"On-demand scenario started: type={scenario.scenario_type} id={scenario.id} services={scenario.services}"
        )

    scenario_mgr.register_start_callback(on_control_scenario_start)

    # Start Control API if enabled
    control_server = None
    if not args.no_control:
        try:
            control_app = create_control_app(scenario_mgr)
            control_server = ControlServerThread(
                app=control_app, host="0.0.0.0", port=args.control_port
            )
            control_server.start()
            logger.info(f"Control API running on http://0.0.0.0:{args.control_port}")
        except Exception as e:
            logger.warning(f"Could not start control API server: {e}")

    # Load scheduled timeline
    scheduled_timeline = load_timeline(args.timeline, seed=args.seed)
    timeline_idx = 0

    running = True

    def handle_shutdown(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    sim_start_time = datetime.now(timezone.utc)
    sim_time = sim_start_time
    start_wall_time = time.monotonic()
    last_status_wall_time = start_wall_time
    last_status_lines = 0
    total_incidents_recorded = 0

    print(f"[generator] Starting DriftWatch log generator")
    print(f"[generator] Log output: {log_file}")
    print(f"[generator] Labels output: {labels_file}")
    print(f"[generator] Speed: {speed}x | Seed: {args.seed} | Timeline: {args.timeline}")

    try:
        while running:
            loop_start = time.monotonic()
            elapsed_sim_s = (sim_time - sim_start_time).total_seconds()

            if args.duration is not None and elapsed_sim_s >= args.duration:
                logger.info(
                    f"Simulation reached target duration of {args.duration}s simulated time."
                )
                break

            dt_sim = tick * speed

            # 1. Trigger scheduled timeline scenarios
            while (
                timeline_idx < len(scheduled_timeline)
                and elapsed_sim_s >= scheduled_timeline[timeline_idx][0]
            ):
                _, factory = scheduled_timeline[timeline_idx]
                sc = factory()
                sc.start(sim_time)
                scenario_mgr.active_scenarios[sc.id] = sc
                labels.start(
                    incident_id=sc.id,
                    incident_type=sc.scenario_type,
                    services=sc.services,
                    ts=sim_time,
                )
                total_incidents_recorded += 1
                logger.info(
                    f"Timeline scenario started: type={sc.scenario_type} id={sc.id} services={sc.services}"
                )
                timeline_idx += 1

            # 2. Reset modifiers and apply all active scenarios
            model.modifier.extra_injections.clear()
            model.modifier.error_share_override.clear()

            expired_ids = []
            for sc_id, sc in list(scenario_mgr.active_scenarios.items()):
                if sc.active(sim_time):
                    sc.apply_modifiers(sim_time, model.modifier)
                else:
                    expired_ids.append(sc_id)

            for exp_id in expired_ids:
                sc = scenario_mgr.active_scenarios.pop(exp_id)
                labels.end(sc.id, ts=sim_time)
                logger.info(
                    f"Scenario ended: type={sc.scenario_type} id={sc.id} at {sim_time.isoformat()}"
                )

            # 3. Generate traffic for this tick
            requests = model.next_requests(sim_time, dt_sim)
            for req in requests:
                writer.write(req)

            # Advance simulated time
            sim_time += timedelta(seconds=dt_sim)

            # Status reporting every 10 seconds of wall time
            now_wall = time.monotonic()
            status_elapsed = now_wall - last_status_wall_time
            if status_elapsed >= 10.0:
                lines_in_period = writer.total_lines_written - last_status_lines
                rate = lines_in_period / status_elapsed if status_elapsed > 0 else 0.0
                ts_iso = sim_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                print(
                    f"[generator] lines={writer.total_lines_written} "
                    f"rate={rate:.1f} lines/s sim_time={ts_iso} active_scenarios={len(scenario_mgr.active_scenarios)}"
                )
                last_status_wall_time = now_wall
                last_status_lines = writer.total_lines_written

            # Maintain tick pace
            loop_duration = time.monotonic() - loop_start
            sleep_time = max(0.0, tick - loop_duration)
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Cleaning up generator resources...")
        scenario_mgr.is_running = False

        if control_server:
            control_server.stop()

        # End all open incidents in labels cleanly with final sim_time
        labels.close(ts=sim_time)
        writer.close()

        print(
            f"[generator] Stopped cleanly.\n"
            f"  - Total lines written: {writer.total_lines_written} (rotations: {writer.total_rotations})\n"
            f"  - Total simulated time: {(sim_time - sim_start_time).total_seconds():.1f}s\n"
            f"  - Incidents triggered: {total_incidents_recorded}"
        )

    return 0


def main():
    args = parse_args()
    sys.exit(run_generator(args))


if __name__ == "__main__":
    main()
