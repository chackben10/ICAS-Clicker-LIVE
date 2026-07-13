from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from production_hub.core.endpoints.actions import ActionRouter
from production_hub.core.endpoints.models import ActionDefinition, ActionResult, EndpointDefinition, EndpointExecutionResult
from production_hub.core.endpoints.variables import resolve_template


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
            if not self._condition_passes(action, context):
                result.action_results.append(ActionResult(action.action_type, True, "skipped by condition"))
                continue
            action_result = await self._execute_action_with_retries(action, context)
            result.action_results.append(action_result)
            if not action_result.ok:
                result.ok = False
                result.error = action_result.message
                break
        result.finished_at = datetime.now(UTC).isoformat()
        return result

    def response_payload(self, endpoint: EndpointDefinition, result: EndpointExecutionResult, context: dict[str, Any]) -> Any:
        response = endpoint.response
        response_context = dict(context)
        for action_result in reversed(result.action_results):
            if action_result.data:
                response_context.update(action_result.data)
                break
        if response.response_type == "last_action_data":
            for action_result in reversed(result.action_results):
                if action_result.data:
                    if "_return" in action_result.data:
                        return resolve_template(action_result.data["_return"], response_context)
                    return resolve_template(action_result.data, response_context)
            return {"ok": result.ok}
        if response.response_type == "static_json":
            return {
                "ok": result.ok,
                "body": resolve_template(response.success_body if result.ok else response.error_body, response_context),
            }
        if response.response_type == "plain_text":
            return str(resolve_template(response.success_body if result.ok else response.error_body, response_context))
        return result.to_dict()

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

    def _condition_passes(self, action: ActionDefinition, context: dict[str, Any]) -> bool:
        condition = str(action.condition or "").strip()
        if not condition:
            return True
        value = resolve_template(condition, context)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
