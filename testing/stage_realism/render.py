"""Render what the PTZ and the wide camera would see, from real imagery.

The background is our real stage (`assets/stage_plate.jpg`, built from the
calibration sweep photos). People are real people cut out of our streams
(`assets/sprites/`). Each rendered frame goes through the production Apple
Vision detector exactly as a live NDI frame would, so detector misses, box
shapes, and confusions come from real pixels rather than from a model.

A walking person cycles through that actor's real walking cut-outs; a person
standing still cycles slowly through their standing cut-outs, which carries the
real sway and gestures. Stage furniture (lectern, communion table, floor
monitors) is redrawn over anyone standing behind it. Camera pans and fast
walkers get horizontal motion blur for one 1/60 s exposure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from .world import SCALE_X, SCALE_Y, metres_to_reference_x

ASSETS = Path(__file__).resolve().parent / "assets"
PTZ_SIZE = (960, 540)  # the tracker analyses PTZ frames at <= 960 px wide
EXPOSURE_SECONDS = 1.0 / 60.0
# Fallback for a bottom-cut cut-out that body pose could not measure: most
# youth-meeting shots cut at the ankles.
CUT_BOTTOM_FRACTION = 0.95


class AssetsMissing(RuntimeError):
    pass


def assets_ready() -> bool:
    return (ASSETS / "stage_plate.jpg").exists() and (ASSETS / "sprites" / "index.json").exists()


@dataclass(frozen=True, slots=True)
class Plate:
    image: np.ndarray
    region: tuple[float, float, float, float]
    scale: float
    audience_size: tuple[int, int]
    occluders: dict

    def to_plate(self, x: float, y: float) -> tuple[float, float]:
        width, height = self.audience_size
        return (x - self.region[0]) * width * self.scale, (y - self.region[1]) * height * self.scale


@lru_cache(maxsize=1)
def load_plate() -> Plate:
    if not assets_ready():
        raise AssetsMissing(
            "Rendered tests need testing/stage_realism/assets/; run tools/build_stage_plate.py "
            "and tools/extract_person_sprites.py (see README)."
        )
    meta = json.loads((ASSETS / "stage_plate.json").read_text())
    image = cv2.imread(str(ASSETS / "stage_plate.jpg"))
    return Plate(image, tuple(meta["region"]), meta["scale"], tuple(meta["audience_size"]), meta["occluders"])


@lru_cache(maxsize=1)
def load_audience() -> np.ndarray:
    return cv2.imread(str(ASSETS / "audience.jpg"))


@lru_cache(maxsize=1)
def actors() -> dict:
    return json.loads((ASSETS / "sprites" / "index.json").read_text())["actors"]


@lru_cache(maxsize=4096)
def _sprite(file: str) -> np.ndarray:
    return cv2.imread(str(ASSETS / "sprites" / file), cv2.IMREAD_UNCHANGED)


def sprite_for(actor: str, t: float, speed: float, direction: int) -> tuple[np.ndarray, float]:
    """An RGBA cut-out for this moment, and the share of the person's height it shows."""

    frames = [item for item in actors()[actor]["frames"] if item.get("qa", "ok") == "ok"]
    walking = [item for item in frames if item["walking"]]
    standing = [item for item in frames if not item["walking"]] or frames
    if speed > 0.3 and walking:
        # About two steps per second at normal pace; stride frequency grows
        # with speed.
        rate = 6.0 * max(0.6, speed / 1.3)
        item = walking[int(t * rate) % len(walking)]
        source_direction = 1 if item["velocity_heights_per_s"] >= 0 else -1
        flip = source_direction != direction
    else:
        item = standing[int(t * 2.0) % len(standing)]
        flip = False
    image = _sprite(item["file"])
    cover = float(item.get("cover", CUT_BOTTOM_FRACTION if item.get("cut_bottom") else 1.0))
    if cover < 0.98:
        image = _fade_bottom(item["file"])
    if flip:
        image = image[:, ::-1]
    return image, cover


@lru_cache(maxsize=4096)
def _fade_bottom(file: str) -> np.ndarray:
    """A cut-out that stops at the frame edge fades out instead of ending in a hard line."""

    image = _sprite(file).copy()
    rows = max(2, image.shape[0] // 8)
    ramp = np.linspace(1.0, 0.0, rows, dtype=np.float32)[:, None]
    image[-rows:, :, 3] = (image[-rows:, :, 3] * ramp).astype(np.uint8)
    return image


def _to_the_floor(sprite: np.ndarray, cover: float) -> np.ndarray:
    """Continue a waist- or thigh-cut cut-out down to the feet (wide view only)."""

    height = sprite.shape[0]
    faded = max(2, height // 8)  # see _fade_bottom
    body = sprite[: height - faded]
    missing = int(round(height * (1.0 / max(0.3, cover) - 1.0))) + faded
    if missing <= 0:
        return sprite
    band = body[max(0, body.shape[0] - max(4, height // 16)) :]
    legs = cv2.resize(band, (sprite.shape[1], missing), interpolation=cv2.INTER_NEAREST)
    # Taper slightly toward the feet.
    columns = np.abs(np.linspace(-1.0, 1.0, sprite.shape[1]))[None, :]
    rows = np.linspace(0.0, 1.0, missing)[:, None]
    legs[:, :, 3] = (legs[:, :, 3] * (columns < 1.0 - 0.25 * rows)).astype(np.uint8)
    return np.vstack([body, legs])


def _blur(image: np.ndarray, pixels: float) -> np.ndarray:
    length = int(round(abs(pixels)))
    if length < 2:
        return image
    kernel = np.zeros((1, length), np.float32)
    kernel[0, :] = 1.0 / length
    return cv2.filter2D(image, -1, kernel)


def _paste(canvas: np.ndarray, sprite: np.ndarray, left: float, top: float, width: float, height: float,
           blur_px: float = 0.0) -> None:
    w, h = int(round(width)), int(round(height))
    if w < 2 or h < 2:
        return
    x0, y0 = int(round(left)), int(round(top))
    if x0 >= canvas.shape[1] or y0 >= canvas.shape[0] or x0 + w <= 0 or y0 + h <= 0:
        return
    interpolation = cv2.INTER_AREA if h < sprite.shape[0] else cv2.INTER_LINEAR
    scaled = cv2.resize(sprite, (w, h), interpolation=interpolation)
    if blur_px:
        scaled = _blur(scaled, blur_px)
    sx0, sy0 = max(0, -x0), max(0, -y0)
    sx1, sy1 = min(w, canvas.shape[1] - x0), min(h, canvas.shape[0] - y0)
    region = canvas[y0 + sy0 : y0 + sy1, x0 + sx0 : x0 + sx1]
    patch = scaled[sy0:sy1, sx0:sx1]
    alpha = patch[:, :, 3:4].astype(np.float32) / 255.0
    region[:] = (patch[:, :, :3] * alpha + region * (1.0 - alpha)).astype(np.uint8)


def _to_view(x: float, y: float, view, size) -> tuple[float, float]:
    vx, vy, vw, vh = view
    return (x - vx) / vw * size[0], (y - vy) / vh * size[1]


def render_ptz(view, people, t: float, *, camera_velocity: float = 0.0, rng=None) -> np.ndarray:
    """The PTZ image for a view rectangle in Audience-normalized coordinates."""

    plate = load_plate()
    width, height = PTZ_SIZE
    vx, vy, vw, vh = view
    left, top = plate.to_plate(vx, vy)
    right, bottom = plate.to_plate(vx + vw, vy + vh)
    sx = width / max(1e-6, right - left)
    sy = height / max(1e-6, bottom - top)
    matrix = np.array([[sx, 0, -left * sx], [0, sy, -top * sy]], np.float32)
    frame = cv2.warpAffine(plate.image, matrix, (width, height), flags=cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_REPLICATE)
    # camera_velocity: view centre motion in view-widths per second.
    frame = _blur(frame, camera_velocity * width * EXPOSURE_SECONDS)
    background = frame.copy()
    ordered = sorted((p for p in people if p.present(t) and p.actor), key=lambda p: p.state(t)[1])
    for person in ordered:
        x_m, feet_y, _posture, speed = person.state(t)
        direction = 1 if _heading(person, t) >= 0 else -1
        sprite, cover = sprite_for(person.actor, t, speed, direction)
        body_height = person.height_m * SCALE_Y * cover
        head_top = feet_y - person.height_m * SCALE_Y
        body_width = body_height * sprite.shape[1] / sprite.shape[0] * (1080 / 1920)
        centre = metres_to_reference_x(x_m)
        x0, y0 = _to_view(centre - body_width / 2.0, head_top, view, PTZ_SIZE)
        x1, y1 = _to_view(centre + body_width / 2.0, head_top + body_height, view, PTZ_SIZE)
        subject_px = speed * direction * SCALE_X / vw * width
        relative = subject_px - camera_velocity * width
        _paste(frame, sprite, x0, y0, x1 - x0, y1 - y0, blur_px=relative * EXPOSURE_SECONDS)
        _occlude(frame, background, plate, view, feet_y)
    if rng is not None:
        noise = rng.normal(0.0, 2.0, frame.shape).astype(np.int16)
        frame = np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    return frame


def _occlude(frame, background, plate: Plate, view, feet_y: float, size=PTZ_SIZE) -> None:
    """Redraw furniture standing in front of someone whose feet are at feet_y."""

    for item in plate.occluders.values():
        if feet_y >= item["base_y"]:
            continue
        polygon = np.array([_to_view(x, y, view, size) for x, y in item["polygon"]], np.int32)
        mask = np.zeros(frame.shape[:2], np.uint8)
        cv2.fillPoly(mask, [polygon], 255)
        frame[mask > 0] = background[mask > 0]


def _heading(person, t: float) -> float:
    before = person.state(max(0.0, t - 0.1))[0]
    after = person.state(t + 0.1)[0]
    return after - before


def render_audience(people, t: float) -> np.ndarray:
    """The wide camera's frame: the real empty church plus the subjects."""

    background = load_audience()
    frame = background.copy()
    plate = load_plate()
    size = (frame.shape[1], frame.shape[0])
    view = (0.0, 0.0, 1.0, 1.0)
    for person in sorted((p for p in people if p.present(t) and p.actor), key=lambda p: p.state(t)[1]):
        x_m, feet_y, _posture, speed = person.state(t)
        direction = 1 if _heading(person, t) >= 0 else -1
        sprite, cover = sprite_for(person.actor, t, speed, direction)
        if cover < 0.98:
            # The wide camera sees people head to foot. A cut-out the stream
            # framed at the waist would float legless here, which no detector
            # calls a person; continue its lowest band of clothing to the
            # floor. At ~150 px tall that is close to a skirt, sari, or
            # trousers, and the PTZ close-up never uses it.
            sprite, cover = _to_the_floor(sprite, cover), 1.0
        body_height = person.height_m * SCALE_Y * cover
        head_top = feet_y - person.height_m * SCALE_Y
        body_width = body_height * sprite.shape[1] / sprite.shape[0] * (size[1] / size[0])
        centre = metres_to_reference_x(x_m)
        x0, y0 = _to_view(centre - body_width / 2.0, head_top, view, size)
        x1, y1 = _to_view(centre + body_width / 2.0, head_top + body_height, view, size)
        _paste(frame, sprite, x0, y0, x1 - x0, y1 - y0)
        _occlude(frame, background, plate, view, feet_y, size)
    return frame


def to_qimage(frame: np.ndarray):
    from PySide6.QtGui import QImage

    height, width = frame.shape[:2]
    return QImage(frame.data, width, height, frame.strides[0], QImage.Format.Format_BGR888).copy()
