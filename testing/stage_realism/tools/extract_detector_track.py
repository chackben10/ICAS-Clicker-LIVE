"""Record what the production person detector sees in a church-stream excerpt.

    PYTHONPATH=Production_Hub_App testing/.venv/bin/python \
        testing/stage_realism/tools/extract_detector_track.py \
        --video 22i-7y8CYc8 --start 7250 --end 7290 --fps 5 \
        --label walk_off_0920 --output testing/stage_realism/fixtures/tracks/

Downloads only the excerpt (yt-dlp + ffmpeg), runs the same
`AppleVisionPersonDetector` the app runs, and writes normalized boxes per frame.
No frames or images are kept. Frames where the program feed cuts to another
camera are flagged, so a scene cut is never mistaken for a person moving.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

WIDTH, HEIGHT = 640, 360
CUT_THRESHOLD = 40.0


def _download(video: str, start: float, end: float, folder: Path) -> Path:
    target = folder / "excerpt.mp4"
    subprocess.run(
        [
            "yt-dlp", "-q", "-f", "bv*[height<=720][ext=mp4]",
            "--download-sections", f"*{start}-{end}", "--force-keyframes-at-cuts",
            "-o", str(target), f"https://www.youtube.com/watch?v={video}",
        ],
        check=True,
    )
    return target


def detect(clip: Path, fps: float) -> list[dict]:
    from PySide6.QtGui import QImage

    from production_hub.tracking.apple_vision import AppleVisionPersonDetector

    detector = AppleVisionPersonDetector()
    decoder = subprocess.Popen(
        [
            "ffmpeg", "-loglevel", "error", "-i", str(clip),
            "-vf", f"fps={fps},scale={WIDTH}:{HEIGHT}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        stdout=subprocess.PIPE,
    )
    frames = []
    previous = None
    index = 0
    size = WIDTH * HEIGHT * 3
    assert decoder.stdout is not None
    while True:
        buffer = decoder.stdout.read(size)
        if len(buffer) < size:
            break
        sample = buffer[::97]
        cut = previous is not None and (
            sum(abs(a - b) for a, b in zip(sample, previous)) / len(sample) > CUT_THRESHOLD
        )
        previous = sample
        image = QImage(buffer, WIDTH, HEIGHT, WIDTH * 3, QImage.Format.Format_RGB888).copy()
        boxes = [
            [
                round(item.bounds.x, 4),
                round(item.bounds.y, 4),
                round(item.bounds.width, 4),
                round(item.bounds.height, 4),
                round(item.confidence, 3),
            ]
            for item in detector.detect(image)
        ]
        frames.append({"t": round(index / fps, 3), "cut": cut, "boxes": boxes})
        index += 1
    decoder.wait()
    return frames


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, help="YouTube video ID")
    parser.add_argument("--start", type=float, required=True, help="seconds into the stream")
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--fps", type=float, default=5.0)
    parser.add_argument("--label", required=True)
    parser.add_argument("--note", default="")
    parser.add_argument("--clip", type=Path, help="use an already-downloaded excerpt")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as folder:
        clip = args.clip or _download(args.video, args.start, args.end, Path(folder))
        frames = detect(clip, args.fps)
    payload = {
        "label": args.label,
        "source": {
            "youtube_id": args.video,
            "start_seconds": args.start,
            "end_seconds": args.end,
            "url": f"https://www.youtube.com/watch?v={args.video}&t={int(args.start)}s",
        },
        "detector": "production_hub AppleVisionPersonDetector (default settings)",
        "fps": args.fps,
        "note": args.note,
        "frames": frames,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / f"{args.label}.json"
    path.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    print(f"Wrote {path} ({len(frames)} frames)")


if __name__ == "__main__":
    main()
