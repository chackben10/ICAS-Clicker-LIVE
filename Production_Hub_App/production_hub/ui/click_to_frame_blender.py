"""Run the Click-to-Frame composite off the Qt main thread.

`cv2.remap` and `cv2.blendLinear` over a full Audience frame, driven by a 125 ms
timer, were running on the Qt main thread while the Click-to-Frame window was
open -- which is most of a service.  That is the one Production Hub hot path
worth moving; it is why the Camera page measured 145-160% CPU before tracking
was even armed.

Both OpenCV calls release the GIL, so a plain worker thread is a real gain and
needs no rewrite of the surrounding widget code.  The worker keeps only the
newest job: a preview that skips an intermediate frame under load is correct,
whereas a queue that grows behind a slow blend is not.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class BlendJob:
    # The geometry a blend was computed for. A result whose geometry no longer
    # matches the caller's is stale and must not be drawn.
    geometry: Any
    # The source frame the blend came from. The caller records this as the
    # sequence it actually drew, which is not necessarily the newest frame
    # available -- the blend runs a tick behind under load.
    sequence: int
    live_bgr: np.ndarray
    map_x: np.ndarray
    map_y: np.ndarray
    reference_bgr: np.ndarray
    alpha: np.ndarray


@dataclass(frozen=True)
class BlendResult:
    geometry: Any
    sequence: int
    image: np.ndarray


def blend_live_over_reference(job: BlendJob) -> np.ndarray:
    """Warp the live Audience frame onto the reference mosaic and blend."""

    warped = cv2.remap(
        job.live_bgr,
        job.map_x,
        job.map_y,
        cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
    )
    return cv2.blendLinear(warped, job.reference_bgr, job.alpha, 1.0 - job.alpha)


class ClickToFrameBlender:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._job: BlendJob | None = None
        self._result: BlendResult | None = None
        self._stopping = False
        self._failures = 0
        self._thread = threading.Thread(
            target=self._run,
            name="production-hub-click-to-frame-blend",
            daemon=True,
        )
        self._thread.start()

    def submit(self, job: BlendJob) -> None:
        """Queue a blend, replacing any job that has not started yet."""

        with self._condition:
            if self._stopping:
                return
            self._job = job
            self._condition.notify()

    def newest(self, geometry: Any) -> BlendResult | None:
        """Return the newest completed blend for `geometry`, or None.

        The result carries the frame it was built from so the caller can record
        what it drew rather than assuming it drew the current frame.
        """

        with self._condition:
            result = self._result
        if result is None or result.geometry != geometry:
            return None
        return result

    @property
    def failures(self) -> int:
        with self._condition:
            return self._failures

    def stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._job = None
            self._condition.notify_all()
        self._thread.join(timeout=3)

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._job is None and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                job = self._job
                self._job = None
            if job is None:
                continue
            try:
                blended = blend_live_over_reference(job)
            except (cv2.error, FloatingPointError, TypeError, ValueError):
                # The widget falls back to the unaligned reference mosaic, as it
                # did when this ran inline.
                with self._condition:
                    self._failures += 1
                continue
            with self._condition:
                self._result = BlendResult(
                    geometry=job.geometry, sequence=job.sequence, image=blended
                )
