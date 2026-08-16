#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from production_hub.calibration.ptz_sweep import (
    PtzAbsolutePose,
    build_bounded_stage_sweep,
    build_structural_landmark_sweep,
    move_to_pose,
    read_pose,
)
from production_hub.app.dev_launcher import DEV_CHILD_ARGUMENT
from production_hub.core.config.models import AppPaths
from production_hub.core.config.repository import ConfigRepository, default_app_root
from production_hub.integrations.panasonic_awp.service import PanasonicAwpService


def parse_args(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Move the configured Panasonic PTZ through a conservative stage sweep and "
            "build reviewable Audience-to-PTZ marker sets for every accepted pose."
        )
    )
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pose-samples", type=int, default=6)
    parser.add_argument("--settle-seconds", type=float, default=1.25)
    parser.add_argument(
        "--profile",
        choices=("structural", "stage"),
        default="structural",
        help="Structural covers the full Audience view; stage retains the legacy bounded sweep.",
    )
    parser.add_argument(
        "--confirm-movement",
        action="store_true",
        help="Required acknowledgement that this routine will physically move the PTZ.",
    )
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if arguments is None else arguments))
    if not args.confirm_movement:
        print("Refusing to move PTZ without --confirm-movement.", file=sys.stderr)
        return 2
    return asyncio.run(run_sweep(args))


async def run_sweep(args: argparse.Namespace) -> int:
    data_root = (args.data_dir or default_app_root()).expanduser().resolve()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_root = (
        args.output.expanduser().resolve()
        if args.output
        else data_root / "calibration-sweeps" / stamp
    )
    repository = ConfigRepository(AppPaths(data_root))
    config = repository.load_app_config().integrations.panasonic
    config.request_timeout_seconds = max(3.0, config.request_timeout_seconds)
    service = PanasonicAwpService(config)
    start = await read_pose(service, "return-position")
    poses = (
        build_structural_landmark_sweep(start)
        if args.profile == "structural"
        else build_bounded_stage_sweep(start)
    )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "purpose": "multi-pose Audience-to-PTZ geometric synchronization",
        "profile": args.profile,
        "motion_authority": True,
        "return_position": start.to_dict(),
        "poses": [],
        "restoration": {"status": "pending"},
    }
    _atomic_json(output_root / "sweep.json", manifest)
    interrupted: BaseException | None = None

    try:
        for index, target in enumerate(poses, start=1):
            print(
                f"[{index}/{len(poses)}] Moving to {target.name}: "
                f"pan={target.pan:04X} tilt={target.tilt:04X} zoom={target.zoom:03X}",
                flush=True,
            )
            reached = await move_to_pose(service, target)
            await asyncio.sleep(max(0.5, float(args.settle_seconds)))
            pose_output = output_root / f"{index:02d}-{target.name}"
            from scripts.calibrate_camera_views import main as calibrate_view

            calibration_arguments = [
                DEV_CHILD_ARGUMENT,
                "--data-dir",
                str(data_root),
                "--output",
                str(pose_output),
                "--samples",
                str(max(3, min(12, int(args.pose_samples)))),
                "--sample-interval",
                "0.35",
            ]
            calibration_exit_code = 1
            capture_attempts = 0
            for capture_attempt in range(1, 4):
                capture_attempts = capture_attempt
                calibration_exit_code = calibrate_view(calibration_arguments)
                if calibration_exit_code != 1:
                    break
                if capture_attempt < 3:
                    delay = 1.5 * capture_attempt
                    print(
                        f"Capture connection failed at {target.name}; retrying in "
                        f"{delay:.1f}s ({capture_attempt + 1}/3)…",
                        flush=True,
                    )
                    await asyncio.sleep(delay)
            actual = await read_pose(service, target.name)
            entry: dict[str, Any] = {
                "index": index,
                "name": target.name,
                "target": target.to_dict(),
                "reached": reached.to_dict(),
                "after_capture": actual.to_dict(),
                "calibration_exit_code": calibration_exit_code,
                "capture_attempts": capture_attempts,
                "calibration_directory": str(pose_output),
            }
            calibration_path = pose_output / "calibration.json"
            if calibration_path.exists():
                payload = json.loads(calibration_path.read_text(encoding="utf-8"))
                alignment = payload.get("alignment", {})
                entry["alignment"] = {
                    key: alignment.get(key)
                    for key in (
                        "status",
                        "confidence_score",
                        "candidate_matches",
                        "inliers",
                        "inlier_ratio",
                        "median_error_pixels",
                        "audience_coverage",
                        "ptz_coverage",
                    )
                }
            manifest["poses"].append(entry)
            _atomic_json(output_root / "sweep.json", manifest)
    except BaseException as exc:
        interrupted = exc
        manifest["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        print("Returning PTZ to its recorded starting position…", flush=True)
        try:
            restored = await move_to_pose(service, start, timeout_seconds=20.0)
            manifest["restoration"] = {
                "status": "restored",
                "actual": restored.to_dict(),
                "completed_at": datetime.now(UTC).isoformat(),
            }
        except BaseException as restore_error:
            manifest["restoration"] = {
                "status": "failed",
                "error": f"{type(restore_error).__name__}: {restore_error}",
            }
            if interrupted is None:
                interrupted = restore_error
        _atomic_json(output_root / "sweep.json", manifest)

    accepted = sum(
        1
        for pose in manifest["poses"]
        if pose.get("alignment", {}).get("status") == "accepted"
    )
    print(
        json.dumps(
            {
                "status": "complete" if interrupted is None else "failed",
                "accepted_poses": accepted,
                "evaluated_poses": len(manifest["poses"]),
                "restoration": manifest["restoration"],
                "output": str(output_root),
            },
            indent=2,
        ),
        flush=True,
    )
    return 0 if interrupted is None and accepted >= 4 else 1


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


if __name__ == "__main__":
    raise SystemExit(main())
