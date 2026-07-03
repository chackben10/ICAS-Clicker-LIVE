from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from production_hub.core.endpoints.actions import ActionRouter
from production_hub.core.endpoints.models import ActionDefinition, ActionResult, EndpointDefinition, EndpointExecutionResult


class EndpointExecutor:
    def __init__(self, router: ActionRouter) -> None:
        self.router = router

    async def execute(self, endpoint: EndpointDefinition, context: dict[str, Any] | None = None) -> EndpointExecutionResult:
        context = context or {}
        result = EndpointExecutionResult(endpoint.key, ok=True)
        if not endpoint.enabled:
            result.ok = False
            result.error = "endpoint_disabled"
            result.finished_at = datetime.now(UTC).isoformat()
            return result

        for action in endpoint.actions:
            action_result = await self._execute_action_with_retries(action, context)
            result.action_results.append(action_result)
            if not action_result.ok:
                result.ok = False
                result.error = action_result.message
                break
        result.finished_at = datetime.now(UTC).isoformat()
        return result

    async def _execute_action_with_retries(self, action: ActionDefinition, context: dict[str, Any]) -> ActionResult:
        if action.delay_seconds:
            await asyncio.sleep(action.delay_seconds)

        attempts = action.retries + 1
        last_result: ActionResult | None = None
        for attempt in range(attempts):
            last_result = await self.router.execute(action, context)
            if last_result.ok:
                return last_result
            if attempt < attempts - 1:
                await asyncio.sleep(action.retry_delay_seconds)
        return last_result or ActionResult(action.action_type, False, "action did not run")

