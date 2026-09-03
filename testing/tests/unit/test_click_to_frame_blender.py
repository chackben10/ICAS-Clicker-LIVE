"""Click-to-Frame compositing must not run on the Qt main thread."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from production_hub.ui.click_to_frame_blender import (
    BlendJob,
    ClickToFrameBlender,
    blend_live_over_reference,
)


def _job(key: object = "k", size: int = 32, alpha_value: float = 1.0, sequence: int = 1) -> BlendJob:
    live = np.full((size, size, 3), 200, dtype=np.uint8)
    reference = np.zeros((size, size, 3), dtype=np.uint8)
    grid_x, grid_y = np.meshgrid(
        np.arange(size, dtype=np.float32), np.arange(size, dtype=np.float32)
    )
    return BlendJob(
        geometry=key,
        sequence=sequence,
        live_bgr=live,
        map_x=grid_x,
        map_y=grid_y,
        reference_bgr=reference,
        alpha=np.full((size, size), alpha_value, dtype=np.float32),
    )


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_identity_map_with_full_alpha_returns_the_live_frame() -> None:
    blended = blend_live_over_reference(_job())
    assert blended.shape == (32, 32, 3)
    assert int(blended.min()) == 200 and int(blended.max()) == 200


def test_zero_alpha_returns_the_reference_mosaic() -> None:
    blended = blend_live_over_reference(_job(alpha_value=0.0))
    assert int(blended.max()) == 0


def test_the_blend_runs_off_the_calling_thread() -> None:
    blender = ClickToFrameBlender()
    caller = threading.get_ident()
    try:
        blender.submit(_job(key="geometry-1"))
        assert _wait(lambda: blender.newest("geometry-1") is not None)
        assert blender._thread.ident != caller
    finally:
        blender.stop()


def test_the_result_reports_the_frame_it_was_built_from() -> None:
    """The page records the frame it drew, not the newest frame available."""

    blender = ClickToFrameBlender()
    try:
        blender.submit(_job(key="geometry-1", sequence=41))
        assert _wait(lambda: blender.newest("geometry-1") is not None)
        assert blender.newest("geometry-1").sequence == 41
    finally:
        blender.stop()


def test_a_result_for_different_geometry_is_never_drawn() -> None:
    """A stale blend would put the live frame in the wrong place."""

    blender = ClickToFrameBlender()
    try:
        blender.submit(_job(key="geometry-1"))
        assert _wait(lambda: blender.newest("geometry-1") is not None)
        # The operator moved the camera: the maps, and so the key, changed.
        assert blender.newest("geometry-2") is None
    finally:
        blender.stop()


def test_only_the_newest_job_is_kept() -> None:
    """Under load a preview skips frames; it must not build a backlog."""

    release = threading.Event()
    blender = ClickToFrameBlender()
    try:
        # Occupy the worker so the following submissions queue behind it.
        slow = _job(key="slow", size=8)
        blender.submit(slow)
        assert _wait(lambda: blender.newest("slow") is not None)

        with blender._condition:
            blender._job = None
        for index in range(20):
            blender.submit(_job(key=f"burst-{index}", size=8))
        release.set()

        assert _wait(lambda: blender.newest("burst-19") is not None)
        # Every earlier burst job was replaced rather than queued.
        assert all(blender.newest(f"burst-{index}") is None for index in range(19))
    finally:
        blender.stop()


def test_a_malformed_job_falls_back_instead_of_raising() -> None:
    blender = ClickToFrameBlender()
    try:
        broken = BlendJob(
            geometry="broken",
            sequence=1,
            live_bgr=np.zeros((4, 4, 3), dtype=np.uint8),
            map_x=np.zeros((2, 2), dtype=np.float32),
            map_y=np.zeros((3, 3), dtype=np.float32),  # mismatched map
            reference_bgr=np.zeros((4, 4, 3), dtype=np.uint8),
            alpha=np.zeros((4, 4), dtype=np.float32),
        )
        blender.submit(broken)
        assert _wait(lambda: blender.failures >= 1)
        assert blender.newest("broken") is None

        # The worker survives and still serves later jobs.
        blender.submit(_job(key="after"))
        assert _wait(lambda: blender.newest("after") is not None)
    finally:
        blender.stop()


def test_stop_is_idempotent_and_ends_the_worker() -> None:
    blender = ClickToFrameBlender()
    blender.stop()
    blender.stop()
    assert not blender._thread.is_alive()


def test_a_settled_geometry_still_advances_frames() -> None:
    """Regression: the preview must not freeze once geometry stops changing.

    The page uses the drawn frame's sequence for its composite cache. If a
    stale result reported the newest sequence instead of its own, the cache
    would believe it was current and never recomposite again.
    """

    blender = ClickToFrameBlender()
    try:
        for sequence in (1, 2, 3):
            blender.submit(_job(key="steady", sequence=sequence))
            assert _wait(
                lambda sequence=sequence: (
                    (result := blender.newest("steady")) is not None
                    and result.sequence == sequence
                )
            )
    finally:
        blender.stop()


def test_a_pending_blend_does_not_force_the_caller_to_fall_back() -> None:
    """Regression: the Click-to-Frame window flickered.

    While the worker had no blend for the current geometry, the page drew the
    unaligned panorama for that frame, so the preview alternated between the
    blended live view and the static mosaic. The blender's contract is only to
    report honestly whether a matching blend exists; the page holds its last
    good composite when one does not. This pins the reporting half.
    """

    blender = ClickToFrameBlender()
    try:
        blender.submit(_job(key="geometry-1", sequence=1))
        assert _wait(lambda: blender.newest("geometry-1") is not None)

        # Geometry changed (camera moved, or the frame size changed): there is
        # no blend for it yet, and saying otherwise would draw a misaligned one.
        assert blender.newest("geometry-2") is None

        # The previous geometry's result is still intact and still reports the
        # frame it came from, so a caller holding it knows what it is showing.
        held = blender.newest("geometry-1")
        assert held is not None and held.sequence == 1
    finally:
        blender.stop()
