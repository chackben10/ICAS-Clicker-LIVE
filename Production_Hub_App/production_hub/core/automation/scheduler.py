from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from production_hub.core.automation.models import AutomationDefinition

AutomationCallback = Callable[[AutomationDefinition], Awaitable[None]]


class AutomationScheduler:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []
        self._stopped = asyncio.Event()

    def schedule_interval(self, definition: AutomationDefinition, callback: AutomationCallback) -> None:
        if definition.interval_seconds <= 0:
            return
        self._tasks.append(asyncio.create_task(self._interval_loop(definition, callback)))

    async def _interval_loop(self, definition: AutomationDefinition, callback: AutomationCallback) -> None:
        while not self._stopped.is_set():
            await asyncio.sleep(definition.interval_seconds)
            if definition.enabled:
                await callback(definition)

    async def stop(self) -> None:
        self._stopped.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

