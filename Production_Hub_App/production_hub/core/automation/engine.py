from __future__ import annotations

from collections.abc import Awaitable, Callable

from production_hub.core.automation.models import AutomationDefinition, AutomationRunState

AutomationHandler = Callable[[AutomationDefinition, AutomationRunState], Awaitable[str]]


class AutomationEngine:
    def __init__(self, automations: list[AutomationDefinition]) -> None:
        self.definitions = {item.key: item for item in automations}
        self.states = {item.key: AutomationRunState(item.key, item.enabled) for item in automations}
        self._handlers: dict[str, AutomationHandler] = {}
        self.paused = False

    def register_handler(self, key: str, handler: AutomationHandler) -> None:
        self._handlers[key] = handler

    def has_handler(self, key: str) -> bool:
        return key in self._handlers

    def pause_all(self) -> None:
        self.paused = True

    def resume_all(self) -> None:
        self.paused = False

    async def run_once(self, key: str) -> AutomationRunState:
        definition = self.definitions[key]
        state = self.states[key]
        if self.paused or not definition.enabled:
            state.last_condition_result = "paused_or_disabled"
            return state
        handler = self._handlers.get(key)
        if handler is None:
            state.last_condition_result = "no_handler"
            return state
        try:
            message = await handler(definition, state)
            state.mark_success(message)
        except Exception as exc:
            state.mark_error(str(exc))
            if state.consecutive_errors >= definition.repeated_error_disable_threshold:
                definition.enabled = False
                state.enabled = False
        return state

    def inspector_rows(self) -> list[AutomationRunState]:
        return list(self.states.values())
