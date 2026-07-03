from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable

from production_hub.core.events.event_models import Event

EventHandler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, name: str, handler: EventHandler) -> None:
        self._subscribers[name].append(handler)

    async def publish(self, event: Event) -> None:
        handlers = [*self._subscribers.get(event.name, []), *self._subscribers.get("*", [])]
        if not handlers:
            return
        await asyncio.gather(*(handler(event) for handler in handlers), return_exceptions=True)

