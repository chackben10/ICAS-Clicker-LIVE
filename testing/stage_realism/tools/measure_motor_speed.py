"""Measure the real AW-HE120 pan/tilt/zoom speed curve and command latency.

    PYTHONPATH=Production_Hub_App testing/.venv/bin/python \
        testing/stage_realism/tools/measure_motor_speed.py

Needs no light: it only reads the camera's own position (#APC, #GZ). The
camera pans on its own for about two minutes in short pulses that alternate
direction, then returns exactly to where it started. Do not run while Subject
Tracking is armed or during a service.

For each speed it records position samples as fast as the camera answers and
derives: the steady rate (motor units/s), how long after the command motion
starts, and how far the head coasts after the stop command. Results go to
`fixtures/icas_motor_measured.json`; the simulator prefers them to the rate it
fitted from Sunday logs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from pathlib import Path

from production_hub.core.config.models import PanasonicConfig
from production_hub.integrations.panasonic_awp.service import PanasonicAwpService

DATA_ROOT = Path.home() / "Library/Application Support/Production Hub"
OUTPUT = Path(__file__).resolve().parents[1] / "fixtures" / "icas_motor_measured.json"
PAN_OFFSETS = (2, 4, 7, 10, 14, 18, 22, 26, 30, 34, 38, 42, 46, 49)
TILT_OFFSETS = (4, 8, 14, 20, 28, 36)
ZOOM_OFFSETS = (4, 8, 16, 28, 49)
# The AW-HE120 can slow pan/tilt when zoomed in ("speed with zoom position"),
# so the curve is measured at the calibration zoom and at sermon zoom.
ZOOM_LEVELS = (2238, 3500)
# Keep each pulse short enough that the head stays near its start.
PAN_TRAVEL, TILT_TRAVEL, ZOOM_TRAVEL = 2200, 900, 600
MIN_PULSE, MAX_PULSE = 0.8, 3.0


def _guess_rate(offset: int, full: float) -> float:
    # Exponential guess from the spec (0.08-60 deg/s); only sizes the pulse.
    return max(8.0, full * (0.0011 ** ((49 - offset) / 48)))


async def _sample(camera: PanasonicAwpService, axis: str) -> tuple[float, int]:
    if axis == "zoom":
        value = await camera.query_zoom_position()
    else:
        pan, tilt = await camera.query_pan_tilt_position()
        value = pan if axis == "pan" else tilt
    return time.monotonic(), value


async def _wait_still(camera, done) -> tuple[int, int, int]:
    """Poll until the head stops moving and `done(pose)` holds."""

    deadline = time.monotonic() + 15.0
    previous = None
    while time.monotonic() < deadline:
        pan, tilt = await camera.query_pan_tilt_position()
        pose = (pan, tilt, await camera.query_zoom_position())
        if pose == previous and done(pose):
            return pose
        previous = pose
        await asyncio.sleep(0.2)
    raise RuntimeError("The camera did not settle at the measurement pose.")


async def _settle(camera, pan: int, tilt: int, zoom: int | None = None) -> None:
    """Return to the start and wait: speed commands are ignored during a move.

    Zoom goes first and pan/tilt only after it finishes, because a zoom
    command sent right after #APC cancels the pan/tilt move.
    """

    if zoom is not None:
        await camera.absolute_zoom(zoom)
        await _wait_still(camera, lambda pose: abs(pose[2] - zoom) <= 8)
    await camera.absolute_pan_tilt(pan, tilt)
    await _wait_still(camera, lambda pose: abs(pose[0] - pan) <= 60 and abs(pose[1] - tilt) <= 60)
    await asyncio.sleep(0.2)


async def _pulse(camera, axis: str, offset: int, direction: int, seconds: float) -> dict:
    speed = 50 + direction * offset
    samples = [await _sample(camera, axis)]
    commanded = time.monotonic()
    if axis == "pan":
        await camera.pan_tilt(speed, 50)
    elif axis == "tilt":
        await camera.pan_tilt(50, speed)
    else:
        await camera.zoom(speed)
    while time.monotonic() - commanded < seconds:
        samples.append(await _sample(camera, axis))
    stopped = time.monotonic()
    if axis == "zoom":
        await camera.stop_zoom()
    else:
        await camera.stop_pan_tilt()
    while time.monotonic() - stopped < 0.8:
        samples.append(await _sample(camera, axis))
    start_value = samples[0][1]
    moving = [(t, v) for t, v in samples if t <= stopped]
    started = next((t for t, v in moving if abs(v - start_value) > 3), None)
    # Steady rate: least squares over the last 60% of the commanded window.
    window = [(t, v) for t, v in moving if t >= commanded + 0.4 * seconds]
    rate = 0.0
    if len(window) >= 3:
        mean_t = sum(t for t, _ in window) / len(window)
        mean_v = sum(v for _, v in window) / len(window)
        spread = sum((t - mean_t) ** 2 for t, _ in window)
        rate = sum((t - mean_t) * (v - mean_v) for t, v in window) / spread if spread else 0.0
    at_stop = min(samples, key=lambda item: abs(item[0] - stopped))[1]
    final = samples[-1][1]
    return {
        "axis": axis,
        "offset": offset,
        "direction": direction,
        "command": speed,
        "seconds": round(seconds, 3),
        "rate_units_per_second": round(rate, 1),
        "start_latency_s": None if started is None else round(started - commanded, 3),
        "coast_units": final - at_stop,
        "samples": [[round(t - commanded, 3), v] for t, v in samples],
    }


async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zooms", default=",".join(str(z) for z in ZOOM_LEVELS),
                        help="comma-separated zoom positions to measure at")
    parser.add_argument("--merge", action="store_true",
                        help="add these runs to the existing fixture instead of replacing it")
    args = parser.parse_args()
    zoom_levels = tuple(int(z) for z in args.zooms.split(","))
    profile = json.loads((DATA_ROOT / "config/default_profile.json").read_text())
    camera = PanasonicAwpService(PanasonicConfig.from_dict(profile["integrations"]["panasonic"]))
    origin_pan, origin_tilt = await camera.query_pan_tilt_position()
    origin_zoom = await camera.query_zoom_position()
    round_trips = []
    for _ in range(8):
        began = time.monotonic()
        await camera.query_pan_tilt_position()
        round_trips.append(time.monotonic() - began)
    print(f"Start pose pan {origin_pan} tilt {origin_tilt} zoom {origin_zoom}; "
          f"position query round trip {1000 * sum(round_trips) / len(round_trips):.0f} ms")
    runs = []
    try:
        for zoom_level in zoom_levels:
            await _settle(camera, origin_pan, origin_tilt, zoom_level)
            for axis, offsets, travel, full in (
                ("pan", PAN_OFFSETS, PAN_TRAVEL, 7260.0),
                ("tilt", TILT_OFFSETS, TILT_TRAVEL, 7260.0),
                ("zoom", ZOOM_OFFSETS if zoom_level == ZOOM_LEVELS[0] else (), ZOOM_TRAVEL, 1100.0),
            ):
                direction = 1
                for offset in offsets:
                    seconds = max(MIN_PULSE, min(MAX_PULSE, travel / _guess_rate(offset, full)))
                    run = await _pulse(camera, axis, offset, direction, seconds)
                    run["zoom"] = zoom_level
                    runs.append(run)
                    print(f"zoom {zoom_level} {axis:4s} {run['command']:3d}  rate {run['rate_units_per_second']:8.1f}/s  "
                          f"starts {run['start_latency_s']} s  coasts {run['coast_units']}")
                    direction = -direction
                    # Back to the start (and to this zoom) before the next pulse.
                    await _settle(camera, origin_pan, origin_tilt, zoom_level)
    finally:
        await camera.stop_pan_tilt()
        await camera.stop_zoom()
        await _settle(camera, origin_pan, origin_tilt, origin_zoom)
    if args.merge and OUTPUT.exists():
        previous = json.loads(OUTPUT.read_text())
        runs = [run for run in previous["runs"] if run.get("zoom") not in zoom_levels] + runs
    OUTPUT.write_text(
        json.dumps(
            {
                "camera": "Panasonic AW-HE120",
                "measured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "position_query_round_trip_s": round(sum(round_trips) / len(round_trips), 4),
                "zooms": sorted({run["zoom"] for run in runs}),
                "runs": runs,
            },
            indent=1,
        )
        + "\n"
    )
    print(f"Wrote {OUTPUT}; camera returned to pan {origin_pan} tilt {origin_tilt} zoom {origin_zoom}")


if __name__ == "__main__":
    asyncio.run(main())
