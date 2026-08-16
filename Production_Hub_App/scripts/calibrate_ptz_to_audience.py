#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from production_hub.app.dev_launcher import DEV_CHILD_ARGUMENT
from production_hub.calibration.store import CalibrationRegistry
from production_hub.core.config.repository import default_app_root
from scripts.build_camera_sync_map import main as build_sync_map
from scripts.calibrate_camera_sweep import main as calibrate_sweep
from scripts.calibrate_camera_views import main as calibrate_reference


def parse_args(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the complete guarded Audience-to-PTZ calibration workflow: direct "
            "reference, physical PTZ sweep, restoration, and composed sync map."
        )
    )
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pose-samples", type=int, default=6)
    parser.add_argument("--reference-samples", type=int, default=12)
    parser.add_argument(
        "--sweep-profile",
        choices=("structural", "stage"),
        default="structural",
    )
    parser.add_argument("--confirm-movement", action="store_true")
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if arguments is None else arguments))
    if not args.confirm_movement:
        print("Refusing complete calibration without --confirm-movement.", file=sys.stderr)
        return 2
    data_root = (args.data_dir or default_app_root()).expanduser().resolve()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_root = (
        args.output.expanduser().resolve()
        if args.output
        else data_root / "calibration-sweeps" / stamp
    )
    reference_directory = output_root / "reference"
    sweep_directory = output_root / "sweep"
    print("Step 1/4 · Capturing direct Audience-to-PTZ reference…", flush=True)
    reference_code = calibrate_reference(
        [
            DEV_CHILD_ARGUMENT,
            "--data-dir",
            str(data_root),
            "--output",
            str(reference_directory),
            "--samples",
            str(max(3, min(30, int(args.reference_samples)))),
        ]
    )
    reference_path = reference_directory / "calibration.json"
    if reference_code != 0 or not reference_path.is_file():
        print("Reference calibration was not accepted; PTZ movement was not started.", file=sys.stderr)
        return 1

    print("Step 2/4 · Running guarded PTZ movement sweep…", flush=True)
    sweep_code = calibrate_sweep(
        [
            "--data-dir",
            str(data_root),
            "--output",
            str(sweep_directory),
            "--pose-samples",
            str(max(3, min(12, int(args.pose_samples)))),
            "--profile",
            args.sweep_profile,
            "--confirm-movement",
        ]
    )
    sweep_path = sweep_directory / "sweep.json"
    if not sweep_path.is_file():
        print("PTZ sweep did not produce a manifest.", file=sys.stderr)
        return 1
    if sweep_code != 0:
        print(
            "Direct moved-pose matches were sparse; attempting the robust same-camera "
            "composition from the retained pose images.",
            flush=True,
        )

    print("Step 3/4 · Building full multi-pose synchronization map…", flush=True)
    map_code = build_sync_map(
        [
            "--reference-calibration",
            str(reference_path),
            "--sweep",
            str(sweep_path),
            "--output",
            str(output_root / "full_sync.json"),
        ]
    )
    if map_code == 0:
        map_path = output_root / "full_sync.json"
        print("Step 4/4 · Activating Camera Sync for live use…", flush=True)
        try:
            CalibrationRegistry(data_root).approve_and_activate(map_path)
        except (OSError, TypeError, ValueError) as exc:
            print(f"Calibration activation failed: {exc}", file=sys.stderr, flush=True)
            return 1
        print(f"Calibration complete and active: {map_path}", flush=True)
    return map_code


if __name__ == "__main__":
    raise SystemExit(main())
