from __future__ import annotations

import asyncio
import threading
import time
import urllib.error
from dataclasses import dataclass
from typing import Any


# Directions that move a slide, and so belong in the press-to-cue distribution.
SLIDE_DIRECTIONS = frozenset({"next", "previous"})


@dataclass(frozen=True)
class ClickerAction:
    direction: str
    # Taken inside the CGEventTap callback, before any queue or HTTP work, so
    # press-to-cue covers the whole path the operator actually waits through.
    received_at: float


class ClickerActionDispatcher:
    """Run clicker actions independently from automation and health polling.

    The dispatcher owns one long-lived event loop on its own thread.  Building
    a fresh loop per keypress -- `asyncio.run()` in the old worker -- paid loop
    setup and teardown on the operator's critical path for no benefit, since
    the ProPresenter client holds no loop-bound state.
    """

    def __init__(
        self,
        propresenter: Any,
        logger: Any,
        *,
        queue_size: int = 32,
        recorder: Any | None = None,
    ) -> None:
        self.propresenter = propresenter
        self.logger = logger
        self.recorder = recorder
        self.queue_size = max(1, int(queue_size))
        self._depth_lock = threading.Lock()
        self._depth = 0
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[ClickerAction | None] | None = None
        self._thread = threading.Thread(
            target=self._run_loop,
            name="production-hub-clicker-actions",
            daemon=True,
        )
        self._thread.start()
        # A press must never race the loop's construction.
        self._ready.wait(timeout=5)

    # -- public API -------------------------------------------------------

    def submit(self, direction: str, pressed_at: float | None = None) -> bool:
        action = ClickerAction(
            direction=str(direction),
            received_at=float(pressed_at if pressed_at is not None else time.monotonic()),
        )
        loop = self._loop
        queue = self._queue
        if loop is None or queue is None or loop.is_closed():
            self.logger.warning(
                "clicker_action_unavailable",
                "Clicker dispatcher is not running; input could not be accepted",
                direction=action.direction,
            )
            return False
        with self._depth_lock:
            if self._depth >= self.queue_size:
                self.logger.warning(
                    "clicker_action_queue_full",
                    "Clicker action queue is full; input could not be accepted",
                    direction=action.direction,
                )
                return False
            self._depth += 1
        if self.recorder is not None and action.direction in SLIDE_DIRECTIONS:
            # Bible mode is not a slide move, so it never enters the press-to-cue
            # distribution; timing it as one would flatter or spoil the number.
            self.recorder.record_press(action.direction, action.received_at, source="clicker")
        loop.call_soon_threadsafe(queue.put_nowait, action)
        return True

    def stop(self) -> None:
        loop = self._loop
        queue = self._queue
        if loop is not None and queue is not None and not loop.is_closed():
            loop.call_soon_threadsafe(queue.put_nowait, None)
        self._thread.join(timeout=5)

    # -- worker -----------------------------------------------------------

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._queue = asyncio.Queue()
        self._ready.set()
        try:
            loop.run_until_complete(self._consume())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                asyncio.set_event_loop(None)
                loop.close()

    async def _consume(self) -> None:
        assert self._queue is not None
        while True:
            action = await self._queue.get()
            if action is None:
                await self._close_connections()
                return
            try:
                await self._perform(action)
            finally:
                with self._depth_lock:
                    self._depth = max(0, self._depth - 1)

    async def _perform(self, action: ClickerAction) -> None:
        queued_ms = round((time.monotonic() - action.received_at) * 1000, 1)
        self.logger.info(
            "clicker_action_received",
            "Clicker action accepted",
            direction=action.direction,
            queued_ms=queued_ms,
        )
        try:
            await self._operation(action.direction)
        except Exception as exc:
            if self._is_absent_previous_cue(action.direction, exc):
                # ProPresenter's dynamic Bible tab has no earlier cue to return
                # to, so it refuses the trigger.  That is the operator reaching
                # the start of the verse list, not a failure -- the previous
                # soak logged sixteen of these as errors.
                self.logger.info(
                    "clicker_action_no_previous_cue",
                    "ProPresenter has no previous cue to return to",
                    direction=action.direction,
                    queued_ms=queued_ms,
                )
                return
            self.logger.warning(
                "clicker_action_failed",
                "Clicker slide action failed",
                direction=action.direction,
                error=str(exc),
            )
            return
        self.logger.info(
            "clicker_action_complete",
            "Clicker slide action complete",
            direction=action.direction,
            queued_ms=queued_ms,
        )

    async def _close_connections(self) -> None:
        """Release this loop's HTTP pool before the loop is torn down."""

        client = getattr(self.propresenter, "client", None)
        aclose = getattr(client, "aclose", None)
        if aclose is None:
            return
        try:
            await aclose()
        except Exception:
            pass

    async def _operation(self, direction: str) -> None:
        if direction == "bible":
            await self.propresenter.trigger_macro(
                self.propresenter.config.bible_macro_trigger_uuid
            )
            return
        if direction == "next":
            await self.propresenter.next_slide()
            return
        await self.propresenter.previous_slide()

    @staticmethod
    def _is_absent_previous_cue(direction: str, error: Exception) -> bool:
        if direction != "previous":
            return False
        status = getattr(error, "code", None)
        if status is None and isinstance(error, urllib.error.HTTPError):
            status = error.code
        return status in {400, 404, 405}
