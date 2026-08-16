from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass

from production_hub.integrations.panasonic_awp.service import PanasonicAwpService


@dataclass(frozen=True)
class PtzAbsolutePose:
    name: str
    pan: int
    tilt: int
    zoom: int

    def to_dict(self) -> dict[str, int | str]:
        return asdict(self)


def build_bounded_stage_sweep(start: PtzAbsolutePose) -> tuple[PtzAbsolutePose, ...]:
    """Build a conservative sweep around an operator-confirmed stage view."""

    pan_offset = 0x0500
    tilt_up_offset = 0x0180
    tilt_down_offset = 0x0100
    wide_zoom = max(0x555, start.zoom - 0x180)
    tight_zoom = min(0xFFF, start.zoom + 0x140)
    return (
        PtzAbsolutePose("reference", start.pan, start.tilt, start.zoom),
        PtzAbsolutePose("wide-center", start.pan, start.tilt, wide_zoom),
        PtzAbsolutePose("wide-left", start.pan - pan_offset, start.tilt, wide_zoom),
        PtzAbsolutePose("wide-right", start.pan + pan_offset, start.tilt, wide_zoom),
        PtzAbsolutePose("medium-left", start.pan - pan_offset, start.tilt, start.zoom),
        PtzAbsolutePose("medium-right", start.pan + pan_offset, start.tilt, start.zoom),
        PtzAbsolutePose(
            "medium-upper",
            start.pan,
            start.tilt - tilt_up_offset,
            start.zoom,
        ),
        PtzAbsolutePose(
            "medium-lower",
            start.pan,
            start.tilt + tilt_down_offset,
            start.zoom,
        ),
        PtzAbsolutePose("tight-center", start.pan, start.tilt, tight_zoom),
    )


def build_structural_landmark_sweep(start: PtzAbsolutePose) -> tuple[PtzAbsolutePose, ...]:
    """Cover the full Audience view with overlapping, wide structural poses."""

    pan_offset = 0x0800
    middle_tilt = start.tilt + 0x0400
    lower_tilt = start.tilt + 0x0800
    # The installed Panasonic reaches its downward mechanical stop slightly
    # before +0x0A00 from the normal stage position. +0x0900 preserves margin
    # while still covering the foreground edge of the Audience frame.
    foreground_tilt = start.tilt + 0x0900
    wide_zoom = max(0x555, start.zoom - 0x0380)

    def pose(name: str, pan: int, tilt: int, zoom: int = wide_zoom) -> PtzAbsolutePose:
        return PtzAbsolutePose(
            name,
            max(0x1000, min(0xF000, pan)),
            max(0x1000, min(0xF000, tilt)),
            max(0x555, min(0xFFF, zoom)),
        )

    return (
        pose("reference", start.pan, start.tilt, start.zoom),
        pose("structural-wide", start.pan, start.tilt),
        pose("upper-left", start.pan - pan_offset, start.tilt),
        pose("upper-right", start.pan + pan_offset, start.tilt),
        pose("middle-right", start.pan + pan_offset, middle_tilt),
        pose("middle-center", start.pan, middle_tilt),
        pose("middle-left", start.pan - pan_offset, middle_tilt),
        pose("lower-left", start.pan - pan_offset, lower_tilt),
        pose("lower-center", start.pan, lower_tilt),
        pose("lower-right", start.pan + pan_offset, lower_tilt),
        pose("foreground-center", start.pan, foreground_tilt),
    )


async def read_pose(service: PanasonicAwpService, name: str = "actual") -> PtzAbsolutePose:
    pan, tilt = await service.query_pan_tilt_position()
    zoom = await service.query_zoom_position()
    return PtzAbsolutePose(name, pan, tilt, zoom)


async def move_to_pose(
    service: PanasonicAwpService,
    target: PtzAbsolutePose,
    *,
    timeout_seconds: float = 15.0,
) -> PtzAbsolutePose:
    """Move to an absolute pose and verify the camera actually arrived."""

    current = await read_pose(service)
    if target.zoom < current.zoom:
        await service.absolute_zoom(target.zoom)
    if abs(target.pan - current.pan) > 0x20 or abs(target.tilt - current.tilt) > 0x20:
        await service.absolute_pan_tilt(target.pan, target.tilt)
    if target.zoom >= current.zoom:
        await service.absolute_zoom(target.zoom)

    deadline = time.monotonic() + max(2.0, timeout_seconds)
    latest = current
    while time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        latest = await read_pose(service)
        if (
            abs(latest.pan - target.pan) <= 0x30
            and abs(latest.tilt - target.tilt) <= 0x30
            and abs(latest.zoom - target.zoom) <= 0x10
        ):
            return PtzAbsolutePose(target.name, latest.pan, latest.tilt, latest.zoom)
    raise TimeoutError(
        f"PTZ did not reach {target.name}; target={target.to_dict()}, "
        f"latest={latest.to_dict()}"
    )
