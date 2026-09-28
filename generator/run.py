"""CLI entry point for the DriftWatch synthetic healthcare log generator.

Streams realistic logs to disk continuously with configurable speed and seed.
Supports graceful shutdown on SIGINT/SIGTERM and status reporting every 10 seconds.
"""

import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import signal
import sys
import time
from typing import Optional

try:
    from generator.traffic import TrafficModel
    from generator.writer import LogWriter
except ImportError:
    from traffic import TrafficModel  # type: ignore
    from writer import LogWriter  # type: ignore


def wire(model: TrafficModel, writer: LogWriter, args: argparse.Namespace) -> None:
    """Extension hook for future issues (e.g. #21 scenarios/timeline, #22 labels, #23 control API).

    Kept clean and simple so later issues can plug in scenario engines or control servers.
    """
    pass


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
        "--log-path",
        type=str,
        default="./logs/hospital.log",
        help="Destination path for the growing log file",
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
        help="Optional maximum duration in wall-clock seconds before stopping (useful for tests)",
    )
    return parser.parse_args(argv)


def run_generator(args: argparse.Namespace) -> int:
    """Run the main log generation loop."""
    log_file = Path(args.log_path).resolve()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    speed = max(0.001, args.speed)
    tick = max(0.01, args.tick)

    model = TrafficModel(seed=args.seed)
    writer = LogWriter(
        log_path=log_file,
        max_bytes=args.rotate_bytes,
        rotate_interval_s=args.rotate_interval,
        flush_every=1,
        seed=args.seed,
    )

    # Allow extension components (scenarios, labels, control API) to wire into the pipeline
    wire(model, writer, args)

    running = True

    def handle_shutdown(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    sim_time = datetime.now(timezone.utc)
    start_wall_time = time.monotonic()
    last_status_wall_time = start_wall_time
    last_status_lines = 0

    print(f"[generator] Starting DriftWatch log generator")
    print(f"[generator] Output: {log_file}")
    print(f"[generator] Speed: {speed}x | Seed: {args.seed}")

    try:
        while running:
            loop_start = time.monotonic()

            if args.duration is not None and (loop_start - start_wall_time) >= args.duration:
                break

            # Simulated time elapsed in this tick
            dt_sim = tick * speed

            # Generate requests in [sim_time, sim_time + dt_sim)
            requests = model.next_requests(sim_time, dt_sim)

            # Write requests and flush
            for req in requests:
                writer.write(req)

            # Advance simulated time
            sim_time += timedelta(seconds=dt_sim)

            # Status report every 10 seconds of wall time
            now_wall = time.monotonic()
            status_elapsed = now_wall - last_status_wall_time
            if status_elapsed >= 10.0:
                lines_in_period = writer.total_lines_written - last_status_lines
                rate = lines_in_period / status_elapsed if status_elapsed > 0 else 0.0
                ts_iso = sim_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                print(
                    f"[generator] lines={writer.total_lines_written} "
                    f"rate={rate:.1f} lines/s sim_time={ts_iso}"
                )
                last_status_wall_time = now_wall
                last_status_lines = writer.total_lines_written

            # Maintain steady wall tick pace
            loop_duration = time.monotonic() - loop_start
            sleep_time = max(0.0, tick - loop_duration)
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        pass
    finally:
        writer.close()
        print(
            f"[generator] Stopped cleanly. Total lines written: {writer.total_lines_written} "
            f"(rotations: {writer.total_rotations})"
        )

    return 0


def main():
    args = parse_args()
    sys.exit(run_generator(args))


if __name__ == "__main__":
    main()
