"""Build the high-resolution stage background the rendered tests draw from.

    testing/.venv/bin/python testing/stage_realism/tools/build_stage_plate.py

Every calibration-sweep PTZ photo is warped into Audience-camera coordinates
with the homography the approved calibration measured for it, at 4x the
Audience resolution. The sharpest photo wins wherever photos overlap: the
zoom-2238 reference pose covers the stage deck, the wide poses cover the side
exits and floor, and the Audience image fills whatever is left. The result is
our real, empty stage as the PTZ sees it.

Output (git-ignored, rebuilt from this Mac's calibration):
    testing/stage_realism/assets/stage_plate.jpg
    testing/stage_realism/assets/stage_plate.json   region and scale
    testing/stage_realism/assets/audience.jpg        the wide camera's view
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import cv2
import numpy as np

DATA_ROOT = Path.home() / "Library/Application Support/Production Hub"
ASSETS = Path(__file__).resolve().parents[1] / "assets"
SCALE = 4.0
# Stage furniture that hides people standing behind it, outlined by hand on the
# calibration's reference PTZ photo (pixels). `base` is the image row where the
# object meets the floor: anyone whose feet are higher in the image is behind it.
OCCLUDERS = {
    "lectern": {"polygon": [[783, 362], [1037, 362], [1037, 657], [783, 657]], "base": 657},
    "communion_table": {"polygon": [[690, 676], [1120, 676], [1120, 965], [690, 965]], "base": 965},
    "monitor_left": {"polygon": [[386, 602], [548, 602], [548, 694], [386, 694]], "base": 694},
    "monitor_right": {"polygon": [[1325, 572], [1520, 572], [1520, 690], [1325, 690]], "base": 690},
}
# Audience-normalized region worth rendering: the whole stage, both side
# exits, and the floor in front of the stage.
REGION = (0.12, -0.10, 0.92, 0.55)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    args = parser.parse_args()

    active = json.loads((args.data_root / "calibration/active-map.json").read_text())["active_map"]
    sweep_root = Path(active).parent
    sync = json.loads(Path(active).read_text())
    width, height = sync["poses"][0]["audience_size"]
    left, top, right, bottom = REGION
    plate_w = round((right - left) * width * SCALE)
    plate_h = round((bottom - top) * height * SCALE)
    # Audience pixel -> plate pixel.
    to_plate = np.array(
        [[SCALE, 0, -left * width * SCALE], [0, SCALE, -top * height * SCALE], [0, 0, 1]],
        dtype=np.float64,
    )

    plate = np.zeros((plate_h, plate_w, 3), np.uint8)
    # Resolution each source contributes, per plate pixel; the best one wins.
    best = np.zeros((plate_h, plate_w), np.float32)

    audience_image = None
    folders = sorted(path for path in (sweep_root / "sweep").iterdir() if path.is_dir())
    by_index = {int(folder.name.split("-", 1)[0]): folder for folder in folders}
    for pose in sync["poses"]:
        folder = by_index.get(int(pose["index"]))
        if folder is None or pose.get("status") != "accepted":
            continue
        image = cv2.imread(str(folder / "ptz.jpg"))
        if audience_image is None and (folder / "audience.jpg").exists():
            audience_image = folder / "audience.jpg"
        if image is None:
            continue
        homography = to_plate @ np.array(pose["ptz_to_audience"], dtype=np.float64)
        warped = cv2.warpPerspective(image, homography, (plate_w, plate_h), flags=cv2.INTER_CUBIC)
        mask = cv2.warpPerspective(
            np.full(image.shape[:2], 255, np.uint8), homography, (plate_w, plate_h), flags=cv2.INTER_NEAREST
        )
        # Keep a margin away from each photo's border, where lens falloff and
        # homography error are worst.
        mask = cv2.erode(mask, np.ones((25, 25), np.uint8))
        detail = 1.0 / max(1e-6, pose["motor_position"]["zoom"] ** -1)  # tighter zoom = sharper
        take = (mask > 0) & (detail > best)
        plate[take] = warped[take]
        best[take] = detail
        print(f"{folder.name:24s} zoom {pose['motor_position']['zoom']:5d} covers {take.mean():6.1%} of the plate")

    if audience_image is not None:
        wide = cv2.imread(str(audience_image))
        warped = cv2.warpPerspective(wide, to_plate, (plate_w, plate_h), flags=cv2.INTER_CUBIC)
        hole = best == 0
        plate[hole] = warped[hole]
        ASSETS.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(audience_image, ASSETS / "audience.jpg")

    reference = next(pose for pose in sync["poses"] if pose["name"] == "reference")
    reference_to_audience = np.array(reference["ptz_to_audience"], dtype=np.float64)
    occluders = {}
    for name, item in OCCLUDERS.items():
        polygon = np.array(item["polygon"], np.float64).reshape(-1, 1, 2)
        audience = cv2.perspectiveTransform(polygon, reference_to_audience).reshape(-1, 2)
        base = cv2.perspectiveTransform(
            np.array([[[np.mean(polygon[:, 0, 0]), item["base"]]]], np.float64), reference_to_audience
        )[0, 0]
        occluders[name] = {
            "polygon": [[round(x / width, 5), round(y / height, 5)] for x, y in audience],
            "base_y": round(base[1] / height, 5),
        }

    ASSETS.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(ASSETS / "stage_plate.jpg"), plate, [cv2.IMWRITE_JPEG_QUALITY, 92])
    (ASSETS / "stage_plate.json").write_text(
        json.dumps(
            {
                "calibration": active,
                "audience_size": [width, height],
                "region": list(REGION),
                "scale": SCALE,
                "size": [plate_w, plate_h],
                "occluders": occluders,
            },
            indent=1,
        )
    )
    print(f"Wrote {ASSETS / 'stage_plate.jpg'} ({plate_w}x{plate_h})")


if __name__ == "__main__":
    main()
