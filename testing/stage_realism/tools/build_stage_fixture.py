"""Rebuild `fixtures/icas_stage.json` from this Mac's Production Hub data.

Run after a recalibration or a tuning change on the production machine:

    PYTHONPATH=Production_Hub_App testing/.venv/bin/python \
        testing/stage_realism/tools/build_stage_fixture.py

Everything in the fixture is numeric. It records:

* the approved Audience-to-PTZ calibration anchors, exactly as the running app
  loads them (no camera addresses or images);
* the live automation/tracking settings from `default_profile.json`;
* the Panasonic pan/tilt response fitted from real Sunday logs, where every
  `ptz_automation_velocity` speed change is integrated between the motor poses
  that `ptz_tracking_source_changed` recorded.

The optics and walking speeds measured from the YouTube recordings are not
derived here; they live in `fixtures/icas_footage_measurements.json` with the
video IDs and timestamps they came from.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import glob
import json
import math
from pathlib import Path

from production_hub.tracking.ptz_geometry import PtzGeometryModel

DATA_ROOT = Path.home() / "Library/Application Support/Production Hub"
OUTPUT = Path(__file__).resolve().parents[1] / "fixtures" / "icas_stage.json"


def _events(log_dir: Path) -> list[dict]:
    wanted = ("ptz_automation_velocity", "ptz_tracking_source_changed", "ptz_", "visca")
    events = []
    for name in sorted(glob.glob(str(log_dir / "*.log*"))):
        with open(name, errors="ignore") as handle:
            for line in handle:
                if not any(key in line for key in wanted):
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event["t"] = dt.datetime.fromisoformat(event["timestamp"]).timestamp()
                events.append(event)
    events.sort(key=lambda item: item["t"])
    return events


def _segments(events: list[dict], axis: str) -> list[tuple[list[tuple[int, float]], int]]:
    """Speed histories between two logged poses with nothing else in between."""

    speed_key = f"{axis}_speed"
    poses = [
        item
        for item in events
        if item["event"] == "ptz_tracking_source_changed"
        and item.get("metadata", {}).get("actual")
    ]
    segments = []
    for start, end in zip(poses, poses[1:]):
        if not 0.5 < end["t"] - start["t"] < 20.0:
            continue
        between = [item for item in events if start["t"] < item["t"] < end["t"]]
        if any(
            item["event"] not in ("ptz_automation_velocity", "ptz_tracking_source_changed")
            for item in between
        ):
            continue
        before = [
            item
            for item in events
            if item["event"] == "ptz_automation_velocity" and item["t"] <= start["t"]
        ]
        if not before or start["t"] - before[-1]["t"] > 30.0:
            continue
        speed = before[-1]["metadata"][speed_key] - 50
        cursor = start["t"]
        pieces = []
        for item in between + [end]:
            pieces.append((speed, item["t"] - cursor))
            cursor = item["t"]
            if item["event"] == "ptz_automation_velocity":
                speed = item["metadata"][speed_key] - 50
        moved = end["metadata"]["actual"][axis] - start["metadata"]["actual"][axis]
        segments.append((pieces, moved))
    return segments


def _shape(offset: int, exponent: float) -> float:
    if offset == 0:
        return 0.0
    return math.copysign(math.exp(exponent * abs(offset)) - 1.0, offset)


def fit_axis(segments) -> dict[str, float]:
    """Least-squares `rate = -gain * sign(s) * (exp(b*|s|) - 1)` in motor units/s.

    The sign is negative because an AW speed above 50 decreases the absolute
    pan and tilt coordinates on this camera.
    """

    best = None
    for exponent in [value / 1000.0 for value in range(5, 300, 5)]:
        xs = [sum(-_shape(speed, exponent) * seconds for speed, seconds in pieces) for pieces, _ in segments]
        ys = [moved for _, moved in segments]
        denominator = sum(x * x for x in xs)
        if denominator <= 0:
            continue
        gain = sum(x * y for x, y in zip(xs, ys)) / denominator
        error = sum((y - gain * x) ** 2 for x, y in zip(xs, ys))
        if best is None or error < best[0]:
            residual = math.sqrt(error / max(1, len(xs)))
            best = (error, gain, exponent, residual)
    if best is None:
        raise ValueError("Not enough logged tracking motion to fit the motor.")
    _error, gain, exponent, residual = best
    return {
        "gain_units_per_second": round(gain, 3),
        "exponent": exponent,
        "segments": len(segments),
        "rms_residual_units": round(residual, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()

    geometry = PtzGeometryModel.load_active(args.data_root)
    anchors = [dataclasses.asdict(item) for item in geometry.anchors]
    profile = json.loads((args.data_root / "config/default_profile.json").read_text())
    tracking = profile["integrations"]["camera_tracking"]
    events = _events(args.data_root / "logs")
    pan = fit_axis(_segments(events, "pan"))
    tilt_segments = [item for item in _segments(events, "tilt") if any(s for s, _ in item[0])]
    try:
        tilt = fit_axis(tilt_segments)
    except ValueError:
        tilt = dict(pan)
    fixture = {
        "schema": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "calibration": {
            "created_at": geometry.created_at,
            "anchors": anchors,
        },
        "tracking_config": {key: value for key, value in tracking.items() if key != "automation"},
        "automation_config": tracking["automation"],
        "motor": {
            "source": "Production Hub logs: speed commands integrated between logged poses",
            "pan": pan,
            "tilt": tilt,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(fixture, indent=1) + "\n")
    print(f"Wrote {args.output}")
    print(json.dumps(fixture["motor"], indent=1))


if __name__ == "__main__":
    main()
