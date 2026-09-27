"""Annotate a real stream moment for replay through the tracker.

    testing/.venv/bin/python testing/stage_realism/tools/annotate_stream_moment.py \
        --video 22i-7y8CYc8 --start 7935 --end 7968 --name youth0920_speaker_walks_off_b \
        --target-x 0.5 --note "..."

Downloads the excerpt (kept in git-ignored assets/moments/), then at 5 frames/s
records every person Apple Vision segments, following each one across frames
with the camera's own pan subtracted (phase correlation on the background) and
their clothing colours compared, so someone walking in where another person
just left is recognised as a different person.
The person nearest `--target-x` in the first frame is the subject the operator
armed on; everyone else is someone the camera must not switch to.

Automatic identity is not trusted blindly: people in white headscarves can
look alike to a colour histogram. Check the overlay sheet this tool writes
(assets/moments/<name>-check.jpg) and pass `--target-until` with the time the
subject has fully left, so a later look-alike is never counted as them, or
`--also-target` with track numbers that are really the subject (e.g. after a
fast turn the tracker lost them).

The committed fixture (fixtures/moments/<name>.json) holds only normalized
boxes, camera motion, and provenance; no images.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parents[1]
FIXTURES = HERE / "fixtures" / "moments"
ASSETS = HERE / "assets" / "moments"
FPS = 5.0
SAME_PERSON_COLOUR_DISTANCE = 0.45


def _colours(frame: np.ndarray, mask: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    histogram = cv2.calcHist([hsv], [0, 1], (mask > 0.5).astype(np.uint8), [18, 8], [0, 180, 0, 256])
    return cv2.normalize(histogram, None).flatten()


def main() -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from extract_person_sprites import _camera_shift, _masks

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--target-x", type=float, default=0.5)
    parser.add_argument("--note", default="")
    parser.add_argument("--log", default="", help="what the Production Hub log shows at this moment")
    parser.add_argument("--clip", type=Path)
    parser.add_argument("--target-until", type=float, default=None,
                        help="seconds into the clip after which the subject is out of shot (human-verified)")
    parser.add_argument("--mask", default="",
                        help="x,y,w,h (normalized) of a stream graphic, e.g. a slide picture-in-picture, "
                        "that the real PTZ feed never had; blanked before detection")
    parser.add_argument("--also-target", default="", help="comma-separated track numbers that are the subject too")
    args = parser.parse_args()

    ASSETS.mkdir(parents=True, exist_ok=True)
    clip = ASSETS / f"{args.name}.mp4"
    if args.clip:
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(args.clip), "-c", "copy", str(clip)], check=True)
    elif not clip.exists():
        subprocess.run(
            [
                "yt-dlp", "-q", "-f", "bv*[height<=720][ext=mp4]", "--download-sections",
                f"*{args.start}-{args.end}", "--force-keyframes-at-cuts", "-o", str(clip),
                f"https://www.youtube.com/watch?v={args.video}",
            ],
            check=True,
        )

    mask_box = [float(v) for v in args.mask.split(",")] if args.mask else None
    capture = cv2.VideoCapture(str(clip))
    source_fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, round(source_fps / FPS))
    frames = []
    tracks: list[dict] = []
    previous = None
    number = -1
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        number += 1
        if number % step:
            continue
        t = number / source_fps
        height, width = frame.shape[:2]
        if mask_box is not None:
            mx, my, mw, mh = mask_box
            frame[int(my * frame.shape[0]) : int((my + mh) * frame.shape[0]),
                  int(mx * frame.shape[1]) : int((mx + mw) * frame.shape[1])] = 110
        masks = _masks(frame)
        union = np.maximum.reduce(masks) if masks else np.zeros((height, width), np.float32)
        gray = cv2.cvtColor(cv2.resize(frame, (480, 270)), cv2.COLOR_BGR2GRAY).astype(np.float32)
        shift = (0.0, 0.0)
        if previous is not None:
            dx, dy = _camera_shift(previous, gray, cv2.resize(union, (480, 270)))
            shift = (dx / 480.0, dy / 270.0)
        previous = gray
        people = []
        for mask in masks:
            ys, xs = np.nonzero(mask > 0.5)
            if len(xs) < 400:
                continue
            box = [xs.min() / width, ys.min() / height, (xs.max() - xs.min()) / width, (ys.max() - ys.min()) / height]
            if box[3] < 0.15:
                continue
            centre = box[0] + box[2] / 2.0
            colours = _colours(frame, mask)
            track = None
            best = None
            for candidate in tracks:
                if t - candidate["t"] > 1.2:
                    continue
                expected = candidate["x"] + shift[0]
                distance = cv2.compareHist(candidate["colours"], colours, cv2.HISTCMP_BHATTACHARYYA)
                if abs(centre - expected) < 0.15 and distance < SAME_PERSON_COLOUR_DISTANCE:
                    cost = abs(centre - expected) + distance
                    if best is None or cost < best:
                        best, track = cost, candidate
            if track is None:
                track = {"id": len(tracks) + 1, "colours": colours}
                tracks.append(track)
            track["t"], track["x"] = t, centre
            track["colours"] = 0.8 * track["colours"] + 0.2 * colours
            people.append({"track": track["id"], "box": [round(v, 4) for v in box]})
        frames.append({"t": round(t, 2), "camera_shift": [round(shift[0], 4), round(shift[1], 4)], "people": people})

    first = next(item for item in frames if item["people"])
    target = min(first["people"], key=lambda p: abs(p["box"][0] + p["box"][2] / 2 - args.target_x))["track"]
    for extra in filter(None, args.also_target.split(",")):
        for item in frames:
            for person in item["people"]:
                if person["track"] == int(extra):
                    person["track"] = target
    if args.target_until is not None:
        for item in frames:
            if item["t"] > args.target_until:
                for person in item["people"]:
                    if person["track"] == target:
                        person["track"] = 1000 + target  # somebody else, whoever the colours matched
    _check_sheet(clip, frames, target, ASSETS / f"{args.name}-check.jpg")
    FIXTURES.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": args.name,
        "source": {
            "youtube_id": args.video,
            "start_seconds": args.start,
            "end_seconds": args.end,
            "url": f"https://www.youtube.com/watch?v={args.video}&t={int(args.start)}s",
        },
        "note": args.note,
        "log": args.log,
        "fps": FPS,
        "mask": mask_box,
        "target_track": target,
        "frames": frames,
    }
    (FIXTURES / f"{args.name}.json").write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    tracks_seen = sorted({p["track"] for f in frames for p in f["people"]})
    print(f"{args.name}: {len(frames)} frames, target track {target}, tracks {tracks_seen}")
    for item in frames[:: int(FPS)]:
        print(item["t"], [(p["track"], round(p["box"][0] + p["box"][2] / 2, 2)) for p in item["people"]])


def _check_sheet(clip: Path, frames: list[dict], target: int, path: Path) -> None:
    """Overlay sheet for a human to confirm who is who (subject in yellow)."""

    capture = cv2.VideoCapture(str(clip))
    tiles = []
    for item in frames[:: max(1, len(frames) // 40)]:
        capture.set(cv2.CAP_PROP_POS_MSEC, item["t"] * 1000)
        ok, image = capture.read()
        if not ok:
            continue
        image = cv2.resize(image, (320, 180))
        for person in item["people"]:
            x, y, w, h = person["box"]
            colour = (0, 255, 255) if person["track"] == target else (0, 0, 255)
            cv2.rectangle(image, (int(x * 320), int(y * 180)), (int((x + w) * 320), int((y + h) * 180)), colour, 2)
        cv2.putText(image, f"{item['t']:.1f}", (4, 174), 0, 0.45, (255, 255, 255), 1)
        tiles.append(image)
    while len(tiles) % 8:
        tiles.append(np.zeros((180, 320, 3), np.uint8))
    cv2.imwrite(str(path), np.vstack([np.hstack(tiles[i : i + 8]) for i in range(0, len(tiles), 8)]))


if __name__ == "__main__":
    main()
