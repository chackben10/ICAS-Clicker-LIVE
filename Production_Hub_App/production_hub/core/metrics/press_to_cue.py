"""Press-to-cue measurement: the only latency number the booth can feel.

A clicker press is timestamped inside the CGEventTap callback, before any queue
or HTTP work, and the clock stops when ProPresenter's status stream pushes the
slide that resulted.  The span therefore contains Production Hub's dispatch, the
HTTP round-trip, *and* ProPresenter's own processing -- which is where the
multi-second stalls actually live.

Hub-to-ProPresenter HTTP latency is a different, much smaller number.  It was
what the previous soak reported, and it is why a 11.5 ms median coexisted with
five-second slides.  Keep the two apart.

The recorder is deliberately cheap: a press costs one monotonic read, a match
costs one subtraction, and the retained history is bounded.  It is safe to leave
on in production, which is the point -- a distribution across a real service is
the evidence, not a synthetic run.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any


# A press that has produced no cue within this long did not cause one: the
# presentation ended, the cue was refused, or ProPresenter dropped it.  Recorded
# as unmatched rather than attributed to whichever slide happens to arrive next.
DEFAULT_MATCH_TIMEOUT_SECONDS = 12.0
DEFAULT_HISTORY = 512
# One cue can arrive as two pushes -- presentation/slide_index and status/slide
# describe the same change and land milliseconds apart. Without this guard a
# single press would close the clock on two, mis-attributing the second. Well
# below any human press cadence, well above the gap between the two pushes.
DEFAULT_MIN_CUE_INTERVAL_SECONDS = 0.25


@dataclass(frozen=True)
class PendingPress:
    direction: str
    pressed_at: float
    source: str


@dataclass(frozen=True)
class PressToCueSample:
    direction: str
    source: str
    latency_ms: float
    signature: str
    recorded_at: float


def _percentile(ordered: list[float], fraction: float) -> float:
    """Nearest-rank percentile. Small samples are the normal case here."""

    if not ordered:
        return 0.0
    rank = max(1, min(len(ordered), int(round(fraction * len(ordered) + 0.5))))
    return ordered[rank - 1]


class PressToCueRecorder:
    def __init__(
        self,
        logger: Any | None = None,
        *,
        history: int = DEFAULT_HISTORY,
        match_timeout_seconds: float = DEFAULT_MATCH_TIMEOUT_SECONDS,
        min_cue_interval_seconds: float = DEFAULT_MIN_CUE_INTERVAL_SECONDS,
    ) -> None:
        self.logger = logger
        self.match_timeout_seconds = max(1.0, float(match_timeout_seconds))
        self.min_cue_interval_seconds = max(0.0, float(min_cue_interval_seconds))
        self._lock = threading.Lock()
        self._pending: deque[PendingPress] = deque()
        self._samples: deque[PressToCueSample] = deque(maxlen=max(16, int(history)))
        self._unmatched = 0
        self._unsolicited = 0
        self._last_signature = ""
        self._last_matched_at = 0.0

    # -- press side -------------------------------------------------------

    def record_press(self, direction: str, pressed_at: float, *, source: str = "clicker") -> None:
        """Register a press. `pressed_at` must come from `time.monotonic()`."""

        with self._lock:
            # Expire against this press, not the wall clock: every timestamp the
            # recorder compares must come from the same clock the caller used.
            self._expire_locked(float(pressed_at))
            self._pending.append(
                PendingPress(direction=str(direction), pressed_at=float(pressed_at), source=str(source))
            )

    # -- cue side ---------------------------------------------------------

    def record_cue(self, signature: str, *, observed_at: float | None = None) -> PressToCueSample | None:
        """Close the clock on the oldest press still waiting for a cue.

        Returns the sample, or None when the cue was not caused by a press --
        an operator advancing from ProPresenter's own UI, or a repeat push of a
        signature already seen.
        """

        signature = str(signature or "")
        now = float(observed_at if observed_at is not None else time.monotonic())
        with self._lock:
            if not signature or signature == self._last_signature:
                return None
            self._last_signature = signature
            if self._last_matched_at and now - self._last_matched_at < self.min_cue_interval_seconds:
                # The same cue arriving by its other route.
                return None
            self._expire_locked(now)
            if not self._pending:
                self._unsolicited += 1
                return None
            press = self._pending.popleft()
            self._last_matched_at = now
            sample = PressToCueSample(
                direction=press.direction,
                source=press.source,
                latency_ms=round((now - press.pressed_at) * 1000, 1),
                signature=signature,
                recorded_at=now,
            )
            self._samples.append(sample)
        if self.logger is not None:
            self.logger.info(
                "press_to_cue",
                "Clicker press reached ProPresenter",
                direction=sample.direction,
                source=sample.source,
                latency_ms=sample.latency_ms,
            )
        return sample

    def _expire_locked(self, now: float) -> None:
        while self._pending and now - self._pending[0].pressed_at > self.match_timeout_seconds:
            expired = self._pending.popleft()
            self._unmatched += 1
            if self.logger is not None:
                self.logger.warning(
                    "press_to_cue_unmatched",
                    "Clicker press produced no ProPresenter slide change",
                    direction=expired.direction,
                    source=expired.source,
                    waited_ms=round((now - expired.pressed_at) * 1000, 1),
                )

    # -- reporting --------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        with self._lock:
            latencies = sorted(sample.latency_ms for sample in self._samples)
            pending = len(self._pending)
            unmatched = self._unmatched
            unsolicited = self._unsolicited
        return {
            "samples": len(latencies),
            "pending": pending,
            "unmatched": unmatched,
            "unsolicited_cues": unsolicited,
            "p50_ms": _percentile(latencies, 0.50),
            "p95_ms": _percentile(latencies, 0.95),
            "max_ms": latencies[-1] if latencies else 0.0,
        }

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            samples = list(self._samples)[-max(1, int(limit)) :]
        return [
            {
                "direction": sample.direction,
                "source": sample.source,
                "latency_ms": sample.latency_ms,
            }
            for sample in samples
        ]

    def reset(self) -> None:
        with self._lock:
            self._pending.clear()
            self._samples.clear()
            self._unmatched = 0
            self._unsolicited = 0
            self._last_signature = ""
            self._last_matched_at = 0.0
