"""Cut real people out of church-stream excerpts to use as rendered test subjects.

    testing/.venv/bin/python testing/stage_realism/tools/extract_person_sprites.py \
        --clip hd_6330-6350.mp4 --video 22i-7y8CYc8 --start 6330

Uses Apple Vision's person instance segmentation (separates people standing
together), keeps people who are whole apart from their feet (the stream's PTZ
framing routinely cuts at the ankles; those are flagged `cut_bottom`), and
follows each person from frame to frame. A cut-out is labelled `walking`
when the person moves relative to the background; the camera's own pan is
measured from the background and subtracted, so an operator's pan is not
mistaken for walking.

Output (git-ignored): testing/stage_realism/assets/sprites/<actor>/<n>.png
(RGBA) and assets/sprites/index.json with each actor's frames and source.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "sprites"
FPS = 10.0
MAX_SPRITE_HEIGHT = 520


def _masks(frame: np.ndarray) -> list[np.ndarray]:
    import Quartz
    import Vision
    from Foundation import NSData, NSIndexSet

    ok, encoded = cv2.imencode(".png", frame)
    data = NSData.dataWithBytes_length_(encoded.tobytes(), len(encoded))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, {})
    request = Vision.VNGeneratePersonInstanceMaskRequest.alloc().init()
    ok, _error = handler.performRequests_error_([request], None)
    if not ok or not request.results():
        return []
    observation = request.results()[0]
    masks = []
    for index in observation.allInstances():
        buffer, _error = observation.generateScaledMaskForImageForInstances_fromRequestHandler_error_(
            NSIndexSet.indexSetWithIndex_(index), handler, None
        )
        Quartz.CVPixelBufferLockBaseAddress(buffer, 1)
        try:
            height = Quartz.CVPixelBufferGetHeight(buffer)
            width = Quartz.CVPixelBufferGetWidth(buffer)
            row = Quartz.CVPixelBufferGetBytesPerRow(buffer)
            base = Quartz.CVPixelBufferGetBaseAddress(buffer)
            values = np.frombuffer(base.as_buffer(height * row), dtype=np.float32)
            masks.append(values.reshape(height, row // 4)[:, :width].copy())
        finally:
            Quartz.CVPixelBufferUnlockBaseAddress(buffer, 1)
    return masks


def _camera_shift(previous: np.ndarray, current: np.ndarray, people: np.ndarray) -> tuple[float, float]:
    """Background translation between frames (pixels), people masked out."""

    window = cv2.createHanningWindow(previous.shape[::-1], cv2.CV_32F)
    keep = (people < 0.5).astype(np.float32)
    shift, _response = cv2.phaseCorrelate(previous * keep, current * keep, window)
    return shift


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clip", type=Path, required=True)
    parser.add_argument("--video", required=True)
    parser.add_argument("--start", type=float, required=True, help="clip start, seconds into the stream")
    parser.add_argument("--label", default="")
    parser.add_argument("--mask", default="", help="x,y,w,h (normalized) stream graphic to ignore, e.g. a slide inset")
    args = parser.parse_args()
    mask_box = [float(v) for v in args.mask.split(",")] if args.mask else None

    ASSETS.mkdir(parents=True, exist_ok=True)
    index_path = ASSETS / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {"actors": {}}
    prefix = args.label or f"{args.video}-{int(args.start)}"
    capture = cv2.VideoCapture(str(args.clip))
    source_fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, round(source_fps / FPS))
    tracks: list[dict] = []
    previous_gray = None
    frame_number = -1
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        frame_number += 1
        if frame_number % step:
            continue
        t = frame_number / source_fps
        height, width = frame.shape[:2]
        if mask_box is not None:
            mx, my, mw, mh = mask_box
            frame[int(my * height) : int((my + mh) * height), int(mx * width) : int((mx + mw) * width)] = 110
        masks = _masks(frame)
        union = np.maximum.reduce(masks) if masks else np.zeros((height, width), np.float32)
        gray = cv2.cvtColor(cv2.resize(frame, (480, 270)), cv2.COLOR_BGR2GRAY).astype(np.float32)
        camera = (0.0, 0.0)
        if previous_gray is not None:
            small_union = cv2.resize(union, (480, 270))
            dx, dy = _camera_shift(previous_gray, gray, small_union)
            camera = (dx * width / 480.0, dy * height / 270.0)
        previous_gray = gray
        for mask in masks:
            ys, xs = np.nonzero(mask > 0.5)
            if len(xs) < 500:
                continue
            left, right, top, bottom = xs.min(), xs.max(), ys.min(), ys.max()
            cut_bottom = bottom >= height - 6
            whole = left > 6 and right < width - 6 and top > 2
            box = (int(left), int(top), int(right), int(bottom))
            centre = ((left + right) / 2.0, (top + bottom) / 2.0)
            track = None
            for candidate in tracks:
                last_t, last_centre, last_box = candidate["last"]
                if t - last_t > 0.35:
                    continue
                expected = (last_centre[0] + camera[0], last_centre[1] + camera[1])
                if abs(centre[0] - expected[0]) < 0.12 * width and abs(centre[1] - expected[1]) < 0.12 * height:
                    track = candidate
                    break
            if track is None:
                track = {"id": f"{prefix}-p{len(tracks) + 1}", "frames": [], "last": None}
                tracks.append(track)
            velocity = 0.0
            if track["last"] is not None:
                last_t, last_centre, _ = track["last"]
                moved = (centre[0] - last_centre[0]) - camera[0]
                velocity = moved / max(1e-6, t - last_t) / (bottom - top)  # body heights per second
            track["last"] = (t, centre, box)
            if not whole or (bottom - top) < 0.45 * height:
                continue
            alpha = cv2.GaussianBlur((mask[top : bottom + 1, left : right + 1] * 255).astype(np.uint8), (5, 5), 0)
            rgba = np.dstack([frame[top : bottom + 1, left : right + 1], alpha])
            scale = min(1.0, MAX_SPRITE_HEIGHT / rgba.shape[0])
            if scale < 1.0:
                rgba = cv2.resize(rgba, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            folder = ASSETS / track["id"]
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{len(track['frames']):03d}.png"
            cv2.imwrite(str(folder / name), rgba)
            track["frames"].append(
                {
                    "file": f"{track['id']}/{name}",
                    "t": round(args.start + t, 2),
                    "velocity_heights_per_s": round(float(velocity), 3),
                    "walking": bool(abs(velocity) > 0.25),
                    "cut_bottom": bool(cut_bottom),
                }
            )
    for track in tracks:
        if len(track["frames"]) < 3:
            continue
        walking = int(sum(item["walking"] for item in track["frames"]))
        index["actors"][track["id"]] = {
            "source": f"https://www.youtube.com/watch?v={args.video}&t={int(args.start)}s",
            "frames": track["frames"],
            "walking_frames": walking,
            "standing_frames": len(track["frames"]) - walking,
        }
        print(f"{track['id']:28s} {len(track['frames']):3d} frames ({walking} walking)")
    index_path.write_text(json.dumps(index, indent=1))


if __name__ == "__main__":
    main()
