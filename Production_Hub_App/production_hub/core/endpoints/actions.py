from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from production_hub.core.endpoints.models import ActionDefinition, ActionResult

ActionHandler = Callable[[ActionDefinition, dict[str, Any]], Awaitable[ActionResult]]


class ActionRouter:
    def __init__(self) -> None:
        self._handlers: dict[str, ActionHandler] = {}

    def register(self, action_type: str, handler: ActionHandler) -> None:
        self._handlers[action_type] = handler

    async def execute(self, action: ActionDefinition, context: dict[str, Any]) -> ActionResult:
        if action.action_type == "delay":
            import asyncio

            await asyncio.sleep(float(action.params.get("seconds", action.delay_seconds)))
            return ActionResult(action.action_type, True, "delay complete")
        handler = self._handlers.get(action.action_type)
        if handler is None:
            return ActionResult(action.action_type, False, f"No handler registered for {action.action_type}")
        return await handler(action, context)

