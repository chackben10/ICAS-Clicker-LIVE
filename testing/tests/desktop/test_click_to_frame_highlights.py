"""Yellow subject highlights in the Click to Frame window.

The window is the operator's at-a-glance check that Subject Tracking is on the
right people. It draws the supervisor's *locked* subjects -- not every person
the detector found -- because a box around somebody in the congregation is the
signal that the lock is wrong, and that is worth seeing before going to air.
"""

from __future__ import annotations

import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from production_hub.app.bootstrap import build_context
from production_hub.calibration.relocalization import (
    RelocalizationSnapshot,
    RelocalizationState,
)
from production_hub.tracking.models import NormalizedRect, SubjectReferenceTarget, TrackedSubject
from production_hub.tracking.ptz_automation import PtzAutomationSnapshot
from production_hub.ui.pages.camera_control_page import ClickToFrameWindow


def _locked_relocalization(offset_x: float = 0.0) -> RelocalizationSnapshot:
    return RelocalizationSnapshot(
        state=RelocalizationState.LOCKED,
        approval_status="approved",
        reference_size=(1000, 1000),
        live_size=(1000, 1000),
        reference_to_live=(
            (1.0, 0.0, offset_x * 1000.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0),
        ),
    )


def _targets(*centres: float) -> tuple[SubjectReferenceTarget, ...]:
    return tuple(
        SubjectReferenceTarget(
            lock_id=index + 1,
            bounds=NormalizedRect(centre - 0.02, 0.40, 0.04, 0.20),
            confidence=0.9,
            source="ptz",
        )
        for index, centre in enumerate(centres)
    )


class _Automation:
    def __init__(self, snapshot: PtzAutomationSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> PtzAutomationSnapshot:
        return self._snapshot.copy()


def _window(qtbot, context, snapshot, relocalization):
    context.ptz_automation = _Automation(snapshot)
    context.video = SimpleNamespace(
        relocalization=SimpleNamespace(snapshot=lambda: relocalization.copy()),
    )
    window = ClickToFrameWindow(context)
    qtbot.addWidget(window)
    return window


@pytest.mark.desktop
def test_locked_subjects_are_highlighted_while_armed(qtbot) -> None:
    with TemporaryDirectory() as temp_dir:
        context = build_context(Path(temp_dir))
        snapshot = _snapshot(0.30, 0.55)
        window = _window(qtbot, context, snapshot, _locked_relocalization())

        window.refresh_tracked_highlights()

        highlights = window.preview._tracked_highlights
        assert len(highlights) == 2
        assert [round(item.center[0], 3) for item in highlights] == [0.30, 0.55]


@pytest.mark.desktop
def test_highlights_follow_the_live_audience_image(qtbot) -> None:
    """Live detections must not receive the motor map or drift transform."""

    with TemporaryDirectory() as temp_dir:
        context = build_context(Path(temp_dir))
        snapshot = _snapshot(0.30)
        window = _window(qtbot, context, snapshot, _locked_relocalization(offset_x=0.08))

        window.refresh_tracked_highlights()

        highlights = window.preview._tracked_highlights
        assert len(highlights) == 1
        assert round(highlights[0].center[0], 3) == 0.30


@pytest.mark.desktop
def test_nothing_is_highlighted_when_tracking_is_off(qtbot) -> None:
    with TemporaryDirectory() as temp_dir:
        context = build_context(Path(temp_dir))
        snapshot = PtzAutomationSnapshot(armed=False, locked_targets=_targets(0.30))
        window = _window(qtbot, context, snapshot, _locked_relocalization())

        window.refresh_tracked_highlights()

        assert window.preview._tracked_highlights == ()


@pytest.mark.desktop
def test_live_observation_needs_no_second_calibration_transform(qtbot) -> None:
    """Matched observations already use the displayed image coordinates."""

    with TemporaryDirectory() as temp_dir:
        context = build_context(Path(temp_dir))
        snapshot = _snapshot(0.30)
        unlocked = RelocalizationSnapshot(
            state=RelocalizationState.DISABLED,
            reference_size=(1000, 1000),
            live_size=(1000, 1000),
        )
        window = _window(qtbot, context, snapshot, unlocked)

        window.refresh_tracked_highlights()

        assert len(window.preview._tracked_highlights) == 1


def _snapshot(*centres):
    return PtzAutomationSnapshot(
        armed=True,
        locked_targets=_targets(*(x + .12 for x in centres)),
        locked_audience_subjects=tuple(
            TrackedSubject(i + 1, NormalizedRect(x - .02, .40, .04, .20),
                           .9, True, 5, time.monotonic())
            for i, x in enumerate(centres)
        ),
    )


@pytest.mark.desktop
@pytest.mark.parametrize("stale", [False, True])
def test_no_estimated_highlight_when_audience_observation_is_missing(qtbot, stale):
    with TemporaryDirectory() as directory:
        context = build_context(Path(directory))
        snapshot = _snapshot(.3)
        if stale:
            from dataclasses import replace
            snapshot.locked_audience_subjects = tuple(
                replace(s, last_seen_monotonic=time.monotonic() - 3)
                for s in snapshot.locked_audience_subjects
            )
        else:
            snapshot.locked_audience_subjects = ()
        window = _window(qtbot, context, snapshot, _locked_relocalization())
        window.refresh_tracked_highlights()
        assert window.preview._tracked_highlights == ()


@pytest.mark.desktop
def test_highlight_preserves_feed_detail_and_has_a_thin_outline(qtbot):
    from PySide6.QtGui import QColor, QPainter, QPixmap
    from production_hub.ui.pages.camera_control_page import VideoPreview

    preview = VideoPreview("Test")
    qtbot.addWidget(preview)
    preview.set_tracked_highlights((NormalizedRect(.2, .2, .4, .4),))
    pixmap = QPixmap(200, 200)
    pixmap.fill(QColor(80, 80, 80))
    painter = QPainter(pixmap)
    preview._paint_tracked_highlights(painter, pixmap)
    painter.end()
    image = pixmap.toImage()
    center = image.pixelColor(80, 80)
    assert max(abs(center.red()-80), abs(center.green()-80), abs(center.blue()-80)) <= 10
    assert image.pixelColor(38, 80) == QColor(80, 80, 80)
    assert image.pixelColor(42, 80) == center


@pytest.mark.desktop
@pytest.mark.parametrize("width,height", [(960, 540), (601, 403)])
def test_highlight_follows_warped_and_cropped_subject_pixels(qtbot, width, height):
    import cv2
    import numpy as np
    from PySide6.QtGui import QImage
    from production_hub.ui.pages.camera_control_page import (
        click_live_rect_to_viewport, crop_click_to_frame_image,
    )
    # A synthetic subject in the camera image. The panorama both translates
    # and shears it, then the operator viewport removes the sides and bottom.
    live = np.zeros((360, 640), dtype=np.uint8)
    live[60:180, 200:280] = 255
    yy, xx = np.mgrid[:height, :width].astype(np.float32)
    map_x = xx * 640/width + yy * .15 - 65
    map_y = yy * 360/height + 18
    alpha = np.ones_like(map_x)
    warped = cv2.remap(live, map_x, map_y, cv2.INTER_NEAREST)
    image = QImage(warped.data, width, height, warped.strides[0], QImage.Format.Format_Grayscale8).copy()
    cropped = crop_click_to_frame_image(image)
    pixels = np.frombuffer(cropped.constBits(), dtype=np.uint8).reshape(cropped.height(), cropped.bytesPerLine())[:, :cropped.width()]
    ys, xs = np.nonzero(pixels)
    bounds = NormalizedRect(200/640, 60/360, 80/640, 120/360)
    mapped = click_live_rect_to_viewport(bounds, map_x, map_y, alpha, (640,360))
    assert mapped is not None
    # Independent rendered pixels must be enclosed to within sampling of one
    # pixel; asserting a copied coordinate formula would miss the real bug.
    assert abs(mapped.x*cropped.width() - xs.min()) <= 2
    assert abs(mapped.y*cropped.height() - ys.min()) <= 2
    assert abs((mapped.x+mapped.width)*cropped.width() - xs.max()-1) <= 2
    assert abs((mapped.y+mapped.height)*cropped.height() - ys.max()-1) <= 2
    assert abs(mapped.x-bounds.x)*cropped.width() > 5  # raw-camera placement would fail
    # No box on panorama-only pixels or outside the cropped viewport.
    assert click_live_rect_to_viewport(bounds, map_x, map_y, alpha*0, (640,360)) is None
    assert click_live_rect_to_viewport(NormalizedRect(.3,.95,.1,.04), map_x,map_y,alpha,(640,360)) is None


@pytest.mark.desktop
def test_floating_window_uses_the_displayed_composite_mapping(qtbot):
    import numpy as np
    from production_hub.ui.pages.camera_control_page import CameraControlPage
    with TemporaryDirectory() as directory:
        context = build_context(Path(directory))
        page = CameraControlPage(context)
        qtbot.addWidget(page)
        snapshot = _snapshot(.30)
        page.context.ptz_automation = _Automation(snapshot)
        yy, xx = np.mgrid[:360, :640].astype(np.float32)
        # Simulate the exact maps retained with the completed blend, not a
        # newly calculated map which could belong to a still-pending frame.
        page._click_mosaic = object()
        page._click_composite_reference_aligned = True
        page._click_composite_highlight_mapping = (xx-80, yy, np.ones_like(xx), (640,360))
        window = page.click_to_frame_window
        window.refresh_tracked_highlights()
        highlights = window.preview._tracked_highlights
        assert len(highlights) == 1
        assert highlights[0].center[0] > .40
        page._click_composite_reference_aligned = False
        window.refresh_tracked_highlights()
        assert window.preview._tracked_highlights == ()
        page._click_mosaic = None
        window.refresh_tracked_highlights()
        assert window.preview._tracked_highlights[0].center[0] == pytest.approx(.30)
        page.close()
        context.video.shutdown()
