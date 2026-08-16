#!/usr/bin/env python3
from __future__ import annotations

import argparse
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

import cv2
import numpy as np

from production_hub.calibration import (
    AlignmentSettings,
    estimate_alignment,
    render_alignment_diagnostics,
)
from production_hub.calibration.sync_map import (
    compose_audience_to_pose,
    invert_homography,
)
from production_hub.calibration.structural_markers import (
    assign_global_marker_ids,
    build_structural_feature_index,
    guided_structural_markers,
    marker_atlas,
    merge_structural_markers,
    prune_global_marker_atlas,
    select_structural_markers,
)


def parse_args(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a multi-pose Audience-to-PTZ map by anchoring one direct camera "
            "alignment and composing robust same-camera PTZ pose links."
        )
    )
    parser.add_argument("--reference-calibration", type=Path, required=True)
    parser.add_argument("--sweep", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if arguments is None else arguments))
    reference_path = args.reference_calibration.expanduser().resolve()
    sweep_path = args.sweep.expanduser().resolve()
    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else sweep_path.parent / "full_sync.json"
    )
    reference_payload = _load_json(reference_path)
    sweep_payload = _load_json(sweep_path)
    reference_alignment = reference_payload.get("alignment", {})
    if reference_alignment.get("status") != "accepted":
        raise RuntimeError("The reference Audience-to-PTZ calibration is not accepted.")
    reference_ptz_path = reference_path.parent / str(
        reference_payload.get("artifacts", {}).get("ptz_image", "ptz.jpg")
    )
    reference_ptz = cv2.imread(str(reference_ptz_path), cv2.IMREAD_COLOR)
    if reference_ptz is None:
        raise RuntimeError(f"Could not read reference PTZ image: {reference_ptz_path}")
    reference_audience_path = reference_path.parent / str(
        reference_payload.get("artifacts", {}).get("audience_image", "audience.jpg")
    )
    reference_audience = cv2.imread(str(reference_audience_path), cv2.IMREAD_COLOR)
    if reference_audience is None:
        raise RuntimeError(
            f"Could not read reference Audience image: {reference_audience_path}"
        )

    identity = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    base_matrix = reference_alignment["audience_to_ptz"]
    structural_feature_index = build_structural_feature_index(reference_audience)
    reference_direct_markers = select_structural_markers(
        reference_alignment.get("correspondences", ()),
        reference_audience,
    )
    reference_guided_markers = guided_structural_markers(
        structural_feature_index,
        reference_ptz,
        base_matrix,
    )
    reference_markers = merge_structural_markers(
        reference_direct_markers,
        reference_guided_markers,
        (reference_audience.shape[1], reference_audience.shape[0]),
    )
    poses: list[dict[str, Any]] = []
    return_pose = sweep_payload["return_position"]
    poses.append(
        {
            "index": 1,
            "name": "reference",
            "motor_position": return_pose,
            "status": "accepted",
            "confidence_score": reference_alignment.get("confidence_score"),
            "reference_ptz_to_pose": identity,
            "audience_to_ptz": base_matrix,
            "ptz_to_audience": reference_alignment["ptz_to_audience"],
            "audience_size": reference_alignment.get("audience_size"),
            "ptz_size": reference_alignment.get("ptz_size"),
            "source": "direct Audience-to-PTZ reference",
            "structural_markers": reference_markers,
        }
    )

    link_settings = AlignmentSettings(
        maximum_width=1920,
        maximum_features=12000,
        ratio_threshold=0.72,
        minimum_audience_coverage=0.02,
        minimum_ptz_coverage=0.05,
    )
    audience_link_settings = AlignmentSettings(
        maximum_width=1920,
        maximum_features=12000,
        ratio_threshold=0.72,
        minimum_audience_coverage=0.15,
        minimum_ptz_coverage=0.15,
    )
    for sweep_pose in sweep_payload.get("poses", []):
        if int(sweep_pose.get("index", 0)) <= 1:
            continue
        pose_directory = Path(sweep_pose["calibration_directory"])
        pose_ptz_path = pose_directory / "ptz.jpg"
        pose_ptz = cv2.imread(str(pose_ptz_path), cv2.IMREAD_COLOR)
        pose_calibration_path = pose_directory / "calibration.json"
        pose_payload = _load_json(pose_calibration_path) if pose_calibration_path.is_file() else {}
        pose_alignment = pose_payload.get("alignment", {})
        pose_artifacts = pose_payload.get("artifacts", {})
        pose_audience_path = pose_directory / str(
            pose_artifacts.get("audience_image", "audience.jpg")
        )
        pose_audience = cv2.imread(str(pose_audience_path), cv2.IMREAD_COLOR)
        pose_diagnostic_directory = (
            output_path.parent
            / "pose-diagnostics"
            / f"{int(sweep_pose.get('index', 0)):02d}-{sweep_pose.get('name', 'pose')}"
        )
        entry: dict[str, Any] = {
            "index": sweep_pose.get("index"),
            "name": sweep_pose.get("name"),
            "motor_position": sweep_pose.get("after_capture", sweep_pose.get("reached")),
            "direct_alignment": sweep_pose.get("alignment", {}),
            "pose_ptz_image": str(pose_ptz_path),
            "pose_audience_image": str(pose_audience_path),
            "audience_size": pose_alignment.get("audience_size"),
            "ptz_size": pose_alignment.get("ptz_size"),
        }
        if pose_ptz is None:
            entry.update({"status": "failed", "reason": "Pose PTZ image is missing."})
            poses.append(entry)
            continue
        link = None
        try:
            link = estimate_alignment(reference_ptz, pose_ptz, link_settings)
            diagnostic_directory = pose_diagnostic_directory / "ptz-link"
            artifacts = render_alignment_diagnostics(
                reference_ptz,
                pose_ptz,
                link,
                diagnostic_directory,
            )
            entry["ptz_link"] = link.to_dict()
            entry["ptz_link_artifacts"] = {
                key: str(diagnostic_directory / value)
                for key, value in artifacts.items()
            }
        except Exception as exc:
            entry["ptz_link_error"] = f"{type(exc).__name__}: {exc}"

        direct_used = False
        if pose_audience is not None and _alignment_is_geometrically_usable(pose_alignment):
            try:
                audience_link = estimate_alignment(
                    reference_audience,
                    pose_audience,
                    audience_link_settings,
                )
                entry["audience_observation_link"] = _alignment_summary(audience_link.to_dict())
                if audience_link.accepted:
                    observation_to_reference = invert_homography(
                        audience_link.audience_to_ptz
                    )
                    direct_composed = compose_audience_to_pose(
                        audience_link.audience_to_ptz,
                        pose_alignment["audience_to_ptz"],
                    )
                    structural_markers = select_structural_markers(
                        pose_alignment.get("correspondences", ()),
                        reference_audience,
                        audience_to_reference=observation_to_reference,
                    )
                    guided_markers = guided_structural_markers(
                        structural_feature_index,
                        pose_ptz,
                        direct_composed,
                    )
                    structural_markers = merge_structural_markers(
                        structural_markers,
                        guided_markers,
                        (reference_audience.shape[1], reference_audience.shape[0]),
                    )
                    if len(structural_markers) >= 4:
                        composed_overlay_path = (
                            pose_diagnostic_directory
                            / "direct_structural_alignment_overlay.jpg"
                        )
                        _write_composed_overlay(
                            reference_audience,
                            pose_ptz,
                            direct_composed,
                            composed_overlay_path,
                        )
                        entry.update(
                            {
                                "status": "accepted",
                                "confidence_score": min(
                                    float(pose_alignment.get("confidence_score", 0.0)),
                                    audience_link.confidence_score,
                                ),
                                "reference_audience_to_observation": audience_link.audience_to_ptz,
                                "audience_to_ptz": direct_composed,
                                "ptz_to_audience": invert_homography(direct_composed),
                                "source": "direct per-pose structural Audience alignment",
                                "structural_markers": structural_markers,
                                "composed_alignment_overlay": str(composed_overlay_path),
                            }
                        )
                        direct_used = True
            except Exception as exc:
                entry["audience_observation_link_error"] = f"{type(exc).__name__}: {exc}"

        if not direct_used and link is not None and link.accepted:
            composed = compose_audience_to_pose(base_matrix, link.audience_to_ptz)
            fallback_markers = _project_reference_markers(
                reference_markers,
                link.audience_to_ptz,
            )
            guided_markers = guided_structural_markers(
                structural_feature_index,
                pose_ptz,
                composed,
            )
            fallback_markers = merge_structural_markers(
                fallback_markers,
                guided_markers,
                (reference_audience.shape[1], reference_audience.shape[0]),
            )
            composed_overlay_path = (
                pose_diagnostic_directory / "composed_alignment_overlay.jpg"
            )
            _write_composed_overlay(
                reference_audience,
                pose_ptz,
                composed,
                composed_overlay_path,
            )
            entry.update(
                {
                    "status": "accepted",
                    "confidence_score": min(
                        float(reference_alignment.get("confidence_score", 0.0)),
                        link.confidence_score,
                    ),
                    "reference_ptz_to_pose": link.audience_to_ptz,
                    "audience_to_ptz": composed,
                    "ptz_to_audience": invert_homography(composed),
                    "source": "fallback composed Audience→reference PTZ→moved PTZ",
                    "structural_markers": fallback_markers,
                    "composed_alignment_overlay": str(composed_overlay_path),
                }
            )
        elif not direct_used:
            reasons = [
                str(item)
                for item in (
                    entry.get("audience_observation_link_error"),
                    entry.get("ptz_link_error"),
                    "; ".join(link.reasons) if link is not None else "",
                )
                if item
            ]
            entry.update(
                {
                    "status": "failed",
                    "reason": "; ".join(reasons) or "No defensible direct or linked alignment.",
                }
            )
        poses.append(entry)

    assign_global_marker_ids(poses)
    structural_marker_count = prune_global_marker_atlas(
        poses,
        (reference_audience.shape[1], reference_audience.shape[0]),
    )
    atlas = marker_atlas(poses)
    atlas_path = output_path.parent / "structural_marker_atlas.jpg"
    _write_marker_atlas(reference_audience, atlas, atlas_path)
    accepted = sum(item.get("status") == "accepted" for item in poses)
    payload = {
        "schema_version": 2,
        "created_at": datetime.now(UTC).isoformat(),
        "purpose": "full multi-pose Audience-to-PTZ synchronization map",
        "motion_authority": False,
        "status": "accepted" if accepted == len(poses) and len(poses) >= 6 else "partial",
        "approval_status": "pending_review",
        "accepted_poses": accepted,
        "total_poses": len(poses),
        "reference_calibration": str(reference_path),
        "sweep_manifest": str(sweep_path),
        "method": (
            "temporally repeatable per-pose structural landmarks with spatial balancing; "
            "same-camera homography fallback"
        ),
        "reference_audience_image": str(reference_audience_path),
        "structural_marker_atlas": str(atlas_path),
        "structural_marker_count": structural_marker_count,
        "structural_markers": atlas,
        "audience_grid_coverage": _grid_coverage(atlas, reference_audience.shape[1], reference_audience.shape[0]),
        "poses": poses,
    }
    _atomic_json(output_path, payload)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "accepted_poses": accepted,
                "total_poses": len(poses),
                "structural_markers": structural_marker_count,
                "audience_grid_coverage": payload["audience_grid_coverage"],
                "output": str(output_path),
                "pose_statuses": {
                    str(item.get("name")): item.get("status") for item in poses
                },
            },
            indent=2,
        )
    )
    return 0 if payload["status"] == "accepted" else 2


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"JSON input does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected a JSON object: {path}")
    return payload


def _alignment_is_geometrically_usable(alignment: dict[str, Any]) -> bool:
    """Allow coverage-only low confidence while retaining strict geometry gates."""

    return (
        alignment.get("status") in {"accepted", "low_confidence"}
        and int(alignment.get("inliers", 0)) >= 24
        and float(alignment.get("inlier_ratio", 0.0)) >= 0.28
        and float(alignment.get("median_error_pixels", float("inf"))) <= 3.0
        and isinstance(alignment.get("audience_to_ptz"), list)
    )


def _alignment_summary(alignment: dict[str, Any]) -> dict[str, Any]:
    return {
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


def _project_reference_markers(
    markers: list[dict[str, Any]],
    matrix: Any,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for marker in markers:
        projected = _project_point(matrix, marker["ptz_x"], marker["ptz_y"])
        if projected is None:
            continue
        selected.append(
            {
                **marker,
                "ptz_x": projected[0],
                "ptz_y": projected[1],
                "stability": "reference_projection",
            }
        )
    return selected


def _project_point(matrix: Any, x: float, y: float) -> tuple[float, float] | None:
    selected = np.asarray(matrix, dtype=np.float64)
    projected = selected @ np.asarray([float(x), float(y), 1.0], dtype=np.float64)
    if abs(float(projected[2])) < 1e-9:
        return None
    return float(projected[0] / projected[2]), float(projected[1] / projected[2])


def _grid_coverage(
    markers: list[dict[str, Any]],
    width: int,
    height: int,
    columns: int = 8,
    rows: int = 6,
) -> dict[str, Any]:
    occupied = {
        (
            min(columns - 1, max(0, int(float(item["audience_x"]) / width * columns))),
            min(rows - 1, max(0, int(float(item["audience_y"]) / height * rows))),
        )
        for item in markers
    }
    total = columns * rows
    return {
        "columns": columns,
        "rows": rows,
        "occupied_cells": len(occupied),
        "total_cells": total,
        "ratio": round(len(occupied) / total, 4),
    }


def _write_marker_atlas(
    audience: np.ndarray,
    markers: list[dict[str, Any]],
    path: Path,
) -> None:
    image = audience.copy()
    for marker in markers:
        marker_id = int(marker["marker_id"])
        point = (round(float(marker["audience_x"])), round(float(marker["audience_y"])))
        cv2.circle(image, point, 7, (20, 25, 30), 2, cv2.LINE_AA)
        cv2.circle(image, point, 4, (70, 235, 235), -1, cv2.LINE_AA)
        cv2.putText(
            image,
            f"M{marker_id:03d}",
            (point[0] + 7, point[1] - 7),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.34,
            (10, 20, 25),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            image,
            f"M{marker_id:03d}",
            (point[0] + 7, point[1] - 7),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.34,
            (70, 235, 235),
            1,
            cv2.LINE_AA,
        )
    cv2.rectangle(image, (0, 0), (min(image.shape[1], 760), 38), (10, 12, 16), -1)
    cv2.putText(
        image,
        f"Structural marker atlas | {len(markers)} verified landmarks",
        (12, 26),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.68,
        (240, 245, 250),
        2,
        cv2.LINE_AA,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 94]):
        raise RuntimeError(f"Could not write structural marker atlas: {path}")


def _write_composed_overlay(
    audience: np.ndarray,
    ptz: np.ndarray,
    matrix,
    path: Path,
) -> None:
    size = (ptz.shape[1], ptz.shape[0])
    homography = np.asarray(matrix, dtype=np.float64)
    warped = cv2.warpPerspective(audience, homography, size)
    source_mask = np.full(audience.shape[:2], 255, dtype=np.uint8)
    valid = cv2.warpPerspective(source_mask, homography, size) > 0
    overlay = ptz.copy()
    overlay[valid] = cv2.addWeighted(ptz[valid], 0.5, warped[valid], 0.5, 0.0)
    cv2.rectangle(overlay, (0, 0), (900, 38), (10, 12, 16), -1)
    cv2.putText(
        overlay,
        "Moved PTZ + 50% composed Audience alignment",
        (12, 26),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.68,
        (240, 245, 250),
        2,
        cv2.LINE_AA,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), overlay, [cv2.IMWRITE_JPEG_QUALITY, 94]):
        raise RuntimeError(f"Could not write composed alignment overlay: {path}")


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
