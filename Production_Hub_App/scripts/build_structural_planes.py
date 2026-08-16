#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import cv2
import numpy as np

from production_hub.calibration.review import load_active_calibration_review
from production_hub.calibration.structural_planes import (
    StructuralPlaneInput,
    StructuralPlaneSettings,
    extract_structural_planes,
    render_structural_plane_overlay,
)


def parse_args(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate reviewable structural plane polygons from the simultaneous "
            "Audience and PTZ images in an approved calibration sweep."
        )
    )
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--map", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--minimum-pose-confirmations", type=int, default=2)
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if arguments is None else arguments))
    data_root = args.data_dir.expanduser().resolve()
    if args.map is not None:
        map_path = args.map.expanduser().resolve()
    else:
        review = load_active_calibration_review(data_root)
        if review is None:
            raise RuntimeError("No approved camera calibration map is available.")
        map_path = review.map_path
    payload = _load_json(map_path)
    if payload.get("status") != "accepted":
        raise RuntimeError("Structural planes require an accepted camera synchronization map.")
    reference_path = _resolve(map_path.parent, payload["reference_calibration"])
    reference_payload = _load_json(reference_path)
    artifacts = reference_payload.get("artifacts", {})
    reference_audience_path = reference_path.parent / str(
        artifacts.get("audience_image", "audience.jpg")
    )
    reference_ptz_path = reference_path.parent / str(
        artifacts.get("ptz_image", "ptz.jpg")
    )
    reference_audience = _read_image(reference_audience_path)
    reference_ptz = _read_image(reference_ptz_path)

    identity = np.eye(3, dtype=np.float64)
    inputs: list[StructuralPlaneInput] = []
    skipped: list[dict[str, object]] = []
    for pose in payload.get("poses", ()):
        if pose.get("status") != "accepted":
            continue
        pose_index = int(pose.get("index", len(inputs) + 1))
        pose_name = str(pose.get("name", f"pose-{pose_index}"))
        if pose_index == 1 or pose_name == "reference":
            inputs.append(
                StructuralPlaneInput(
                    pose_index=pose_index,
                    pose_name=pose_name,
                    audience_bgr=reference_audience,
                    ptz_bgr=reference_ptz,
                    audience_to_reference=identity,
                )
            )
            continue
        audience_value = pose.get("pose_audience_image")
        ptz_value = pose.get("pose_ptz_image")
        reference_to_observation = pose.get("reference_audience_to_observation")
        if not audience_value or not ptz_value or reference_to_observation is None:
            skipped.append(
                {
                    "pose_index": pose_index,
                    "pose_name": pose_name,
                    "reason": "simultaneous camera images or Audience drift transform are missing",
                }
            )
            continue
        try:
            audience_to_reference = np.linalg.inv(
                np.asarray(reference_to_observation, dtype=np.float64)
            )
            audience_to_reference /= audience_to_reference[2, 2]
            inputs.append(
                StructuralPlaneInput(
                    pose_index=pose_index,
                    pose_name=pose_name,
                    audience_bgr=_read_image(_resolve(map_path.parent, audience_value)),
                    ptz_bgr=_read_image(_resolve(map_path.parent, ptz_value)),
                    audience_to_reference=audience_to_reference,
                )
            )
        except (OSError, ValueError, np.linalg.LinAlgError) as exc:
            skipped.append(
                {
                    "pose_index": pose_index,
                    "pose_name": pose_name,
                    "reason": str(exc),
                }
            )
    if len(inputs) < max(2, int(args.minimum_pose_confirmations)):
        raise RuntimeError("The calibration map has too few usable simultaneous camera poses.")

    output_path = (
        args.output.expanduser().resolve()
        if args.output is not None
        else map_path.parent / "structural-planes" / "planes.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"Generating structural planes from {len(inputs)} simultaneous Audience/PTZ pose(s)…",
        flush=True,
    )
    settings = StructuralPlaneSettings(
        minimum_pose_confirmations=args.minimum_pose_confirmations,
    )
    result = extract_structural_planes(
        reference_audience,
        inputs,
        settings,
        progress=lambda message: print(message, flush=True),
    )
    overlay_name = "planes-overlay.jpg"
    overlay = render_structural_plane_overlay(reference_audience, result.planes)
    if not cv2.imwrite(str(output_path.parent / overlay_name), overlay):
        raise OSError("OpenCV could not write the structural-plane review overlay.")
    now = datetime.now(UTC).isoformat()
    output = {
        "schema_version": 1,
        "created_at": now,
        "status": "accepted" if result.planes else "no_candidates",
        "method": (
            "SIFT reciprocal matching + iterative USAC_MAGSAC plane models + "
            "spatial support clustering + cross-pose recurrence"
        ),
        "source_map": str(map_path),
        "calibration_reference": str(payload.get("created_at", "")),
        "reference_audience_image": str(reference_audience_path),
        "overlay_image": overlay_name,
        "settings": asdict(settings),
        "pose_summaries": [*result.pose_summaries, *skipped],
        "total_observations": result.total_observations,
        "planes": [plane.to_dict() for plane in result.planes],
    }
    _atomic_json(output_path, output)
    print(f"Structural-plane artifact: {output_path}", flush=True)
    print(f"Review overlay: {output_path.parent / overlay_name}", flush=True)
    print(
        f"Result: {len(result.planes)} plane candidate(s); no Production Hub settings were changed.",
        flush=True,
    )
    return 0 if result.planes else 2


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return payload


def _resolve(parent: Path, value: object) -> Path:
    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (parent / path).resolve()


def _read_image(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise OSError(f"Could not read camera image: {path}")
    return image


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
