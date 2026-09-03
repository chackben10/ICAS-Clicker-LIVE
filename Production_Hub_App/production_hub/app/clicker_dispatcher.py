from __future__ import annotations

import asyncio
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ClickerAction:
    direction: str
    received_at: float


class ClickerActionDispatcher:
    """Run clicker actions independently from automation and health polling."""

    def __init__(self, propresenter: Any, logger: Any, *, queue_size: int = 32) -> None:
        self.propresenter = propresenter
        self.logger = logger
        self._queue: queue.Queue[ClickerAction | None] = queue.Queue(maxsize=queue_size)
        self._thread = threading.Thread(
            target=self._run,
            name="production-hub-clicker-actions",
            daemon=True,
        )
        self._thread.start()

    def submit(self, direction: str) -> bool:
        action = ClickerAction(direction=direction, received_at=time.monotonic())
        try:
            self._queue.put_nowait(action)
            return True
        except queue.Full:
            self.logger.warning(
                "clicker_action_queue_full",
                "Clicker action queue is full; input could not be accepted",
                direction=direction,
            )
            return False

    def _run(self) -> None:
        while True:
            action = self._queue.get()
            try:
                if action is None:
                    return
                queued_ms = round((time.monotonic() - action.received_at) * 1000, 1)
                self.logger.info(
                    "clicker_action_received",
                    "Clicker action accepted",
                    direction=action.direction,
                    queued_ms=queued_ms,
                )
                operation = (
                    self.propresenter.next_slide
                    if action.direction == "next"
                    else self.propresenter.previous_slide
                )
                asyncio.run(operation())
                self.logger.info(
                    "clicker_action_complete",
                    "Clicker slide action complete",
                    direction=action.direction,
                    queued_ms=queued_ms,
                )
            except Exception as exc:
                self.logger.warning(
                    "clicker_action_failed",
                    "Clicker slide action failed",
                    direction=getattr(action, "direction", "unknown"),
                    error=str(exc),
                )
            finally:
                self._queue.task_done()

    def stop(self) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # The listener is already stopped; let accepted inputs finish.
            self._queue.put(None, timeout=2)
        self._thread.join(timeout=5)
