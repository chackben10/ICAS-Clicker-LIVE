"""How fast the AW-HE120 actually moves for each continuous speed command.

Measured on the ICAS AW-HE120 on 2026-09-22 by reading the camera's own
position while it ran each speed (testing/stage_realism/tools/
measure_motor_speed.py). The camera's "speed with zoom position" feature slows
pan and tilt as it zooms in, so the same command covers far less ground at
sermon zoom than at a wide shot: speed offset 26 is ~2800 pan units/s at zoom
2238 but ~390 at zoom 3500, and the same at 3600 (the slowdown levels off).
Values between measured zooms are interpolated in log space; beyond them the
nearest measurement holds.

Offsets are |AW speed - 50|. Rates are absolute motor units per second.
"""

from __future__ import annotations

import math

MEASURED_ZOOMS = (2238, 3500, 3600)
_PAN = {
    2238: ((2, 12.0), (4, 25.5), (7, 66.3), (10, 169.6), (14, 431.9), (18, 1017.5), (22, 1889.5),
           (26, 2812.4), (30, 3660.7), (34, 4417.4), (38, 5304.0), (42, 6168.3), (46, 7075.9), (49, 7518.3)),
    3500: ((2, 11.3), (4, 16.7), (7, 36.6), (10, 65.3), (14, 124.3), (18, 198.0), (22, 284.8),
           (26, 386.9), (30, 541.5), (34, 760.8), (38, 1012.2), (42, 1304.5), (46, 1621.9), (49, 1856.3)),
    3600: ((2, 7.8), (4, 15.3), (7, 40.1), (10, 65.7), (14, 122.1), (18, 198.0), (22, 285.6),
           (26, 388.0), (30, 543.8), (34, 760.6), (38, 1016.9), (42, 1299.4), (46, 1635.1), (49, 1863.7)),
}
_TILT = {
    2238: ((4, 21.9), (8, 93.5), (14, 387.0), (28, 3263.7), (36, 4156.4)),
    3500: ((4, 11.9), (8, 47.2), (14, 125.5), (20, 246.8), (28, 385.9), (36, 908.5)),
    3600: ((4, 13.1), (8, 46.8), (14, 127.2), (20, 246.8), (28, 388.7), (36, 913.4)),
}
_TABLES = {"pan": _PAN, "tilt": _TILT}


def _curve(points, offset: float) -> float:
    offset = max(0.0, min(float(offset), points[-1][0]))
    previous = (0.0, 0.0)
    for point in points:
        if offset <= point[0]:
            (o0, r0), (o1, r1) = previous, point
            if r0 <= 0.0:
                return r1 * (offset - o0) / max(1e-9, o1 - o0)
            return math.exp(math.log(r0) + (math.log(r1) - math.log(r0)) * (offset - o0) / (o1 - o0))
        previous = point
    return points[-1][1]


def rate(axis: str, offset: float, zoom: float) -> float:
    """Absolute motor units/s for a speed offset at this zoom position.

    Log-linear between measured zooms, flat beyond them: the slowdown levels
    off by zoom 3500 (3600 measured within 1% of 3500).
    """

    tables = _TABLES[axis]
    zooms = MEASURED_ZOOMS
    zoom = max(zooms[0], min(zooms[-1], float(zoom)))
    for low, high in zip(zooms, zooms[1:]):
        if low <= zoom <= high:
            a = _curve(tables[low], abs(offset))
            b = _curve(tables[high], abs(offset))
            if a <= 0.0 or b <= 0.0:
                return max(a, b)
            fraction = (zoom - low) / (high - low)
            return math.exp(math.log(a) + (math.log(b) - math.log(a)) * fraction)
    return _curve(tables[zooms[-1]], abs(offset))


def offset_for_rate(axis: str, units_per_second: float, zoom: float, *, maximum: float = 49.0) -> float:
    """The (fractional) speed offset that moves at `units_per_second`, capped."""

    wanted = abs(units_per_second)
    if wanted <= 0.0:
        return 0.0
    low, high = 0.0, float(maximum)
    if rate(axis, high, zoom) <= wanted:
        return high
    for _ in range(30):
        middle = (low + high) / 2.0
        if rate(axis, middle, zoom) < wanted:
            low = middle
        else:
            high = middle
    return high


# How many motor units one frame spans at each zoom: the ICAS calibration's
# pose-to-image mapping combined with the PTZ footprint measured from the
# calibration sweep (zoom 1365, 2238) and 2026-09-20 stream shots (3642, +/-25%).
# Tilt spans 0.75 of the pan figure (16:9 frame, square motor units).
_FRAME_PAN_UNITS = ((1365, 9037.0), (2238, 4948.0), (2900, 2791.0), (3500, 1661.0), (4095, 992.0))


def frame_units(axis: str, zoom: float) -> float:
    """Motor units across one frame width (pan) or height (tilt) at this zoom."""

    points = _FRAME_PAN_UNITS
    zoom = max(points[0][0], min(points[-1][0], float(zoom)))
    for (z0, u0), (z1, u1) in zip(points, points[1:]):
        if z0 <= zoom <= z1:
            units = math.exp(math.log(u0) + (math.log(u1) - math.log(u0)) * (zoom - z0) / (z1 - z0))
            break
    return units if axis == "pan" else units * 0.75


def picture_speed(axis: str, offset: float, zoom: float) -> float:
    """How fast the picture moves for a speed offset, in frames per second (signed)."""

    return math.copysign(rate(axis, abs(offset), zoom) / frame_units(axis, zoom), offset) if offset else 0.0


def offset_for_picture_speed(axis: str, frames_per_second: float, zoom: float, *, maximum: float = 49.0) -> float:
    """The signed speed offset that moves the picture at `frames_per_second`."""

    offset = offset_for_rate(axis, abs(frames_per_second) * frame_units(axis, zoom), zoom, maximum=maximum)
    return math.copysign(offset, frames_per_second) if frames_per_second else 0.0
