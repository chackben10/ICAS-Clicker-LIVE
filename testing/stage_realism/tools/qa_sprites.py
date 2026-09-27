"""Reject cut-outs where segmentation merged two people into one.

    PYTHONPATH=Production_Hub_App testing/.venv/bin/python testing/stage_realism/tools/qa_sprites.py

Each cut-out is placed alone on a grey frame and run through the production
person detector. A frame the detector sees as two or more people is marked
`qa: "merged"` and never rendered. A frame it cannot see at all is kept: a real
person the detector misses is exactly the kind of case the suite wants.
Frames carrying pieces of a blanked stream graphic are marked `qa: "graphic"`.

For cut-outs that lose their lower body at the frame edge, Apple Vision body
pose estimates how much of the person is shown (`cover`), from the lowest
visible joint (ankle, knee, hip, then the neck as a last resort), and each
person uses the median over all of their cut-outs.
The renderer scales the cut-out by that fraction so nobody is stretched.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

SPRITES = Path(__file__).resolve().parents[1] / "assets" / "sprites"


# Fraction of standing height from the top of the head down to each Vision
# joint (foot = ankle, leg = knee, upLeg = hip, neck = base of the neck).
_JOINT_DEPTH = (("_foot_", 0.96), ("_leg_", 0.72), ("_upLeg_", 0.52), ("root", 0.52), ("neck_1", 0.20))


def _cover(sprite: np.ndarray) -> float:
    """Share of the person's height a bottom-cut cut-out shows (body pose)."""

    import Vision
    from Foundation import NSData

    ok, encoded = cv2.imencode(".png", sprite[:, :, :3])
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
        NSData.dataWithBytes_length_(encoded.tobytes(), len(encoded)), {}
    )
    request = Vision.VNDetectHumanBodyPoseRequest.alloc().init()
    handler.performRequests_error_([request], None)
    if not request.results():
        return 0.60
    observation = request.results()[0]
    points, _error = observation.recognizedPointsForGroupKey_error_(
        Vision.VNHumanBodyPoseObservationJointsGroupNameAll, None
    )
    seen = {}
    for name, point in (points or {}).items():
        if point.confidence() < 0.3:
            continue
        # Vision's y axis points up; convert to distance from the top.
        seen[str(name)] = 1.0 - point.location().y
    for fragment, depth in _JOINT_DEPTH:
        rows = [value for key, value in seen.items() if fragment in key]
        if rows:
            return float(min(1.0, max(0.30, depth / max(0.05, max(rows)))))
    return 0.60


def main() -> None:
    from PySide6.QtGui import QImage

    from production_hub.tracking.apple_vision import AppleVisionPersonDetector

    detector = AppleVisionPersonDetector()
    index = json.loads((SPRITES / "index.json").read_text())
    for actor, entry in index["actors"].items():
        merged = 0
        for item in entry["frames"]:
            sprite = cv2.imread(str(SPRITES / item["file"]), cv2.IMREAD_UNCHANGED)
            canvas = np.full((540, 960, 3), 128, np.uint8)
            height = 480
            width = max(2, int(sprite.shape[1] * height / sprite.shape[0]))
            if width > 900:
                width, height = 900, int(sprite.shape[0] * 900 / sprite.shape[1])
            scaled = cv2.resize(sprite, (width, height), interpolation=cv2.INTER_AREA)
            x0, y0 = (960 - width) // 2, 540 - height
            alpha = scaled[:, :, 3:4] / 255.0
            region = canvas[y0 : y0 + height, x0 : x0 + width]
            region[:] = (scaled[:, :, :3] * alpha + region * (1 - alpha)).astype(np.uint8)
            image = QImage(canvas.data, 960, 540, canvas.strides[0], QImage.Format.Format_BGR888).copy()
            people = [c for c in detector.detect(image) if c.bounds.height > 0.25]
            item["qa"] = "merged" if len(people) >= 2 else "ok"
            opaque = sprite[:, :, 3] > 128
            blanked = np.all(np.abs(sprite[:, :, :3].astype(int) - 110) <= 2, axis=2) & opaque
            if opaque.any() and blanked.sum() / opaque.sum() > 0.02:
                item["qa"] = "graphic"
            item["cover"] = _cover(sprite) if item.get("cut_bottom") else 1.0
            merged += item["qa"] == "merged"
        # One scale per person: per-frame pose estimates are noisy and would
        # make the person visibly grow and shrink.
        covers = sorted(item["cover"] for item in entry["frames"] if item["qa"] == "ok")
        if covers:
            median = covers[len(covers) // 2]
            for item in entry["frames"]:
                item["cover"] = median
        print(f"{actor:24s} {len(entry['frames']):4d} frames, {merged} merged, shows {median if covers else 0:.0%} of height")
    (SPRITES / "index.json").write_text(json.dumps(index, indent=1))


if __name__ == "__main__":
    main()
