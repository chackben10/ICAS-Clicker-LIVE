from __future__ import annotations

import unittest

from production_hub.core.endpoints.actions import ActionRouter
from production_hub.core.endpoints.executor import EndpointExecutor
from production_hub.core.endpoints.models import ActionDefinition, ActionResult, EndpointDefinition


class EndpointExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_sequential_actions_stop_on_failure(self) -> None:
        router = ActionRouter()
        calls: list[str] = []

        async def ok(action, context):
            calls.append(action.action_type)
            return ActionResult(action.action_type, True)

        async def fail(action, context):
            calls.append(action.action_type)
            return ActionResult(action.action_type, False, "failed")

        router.register("ok", ok)
        router.register("fail", fail)
        endpoint = EndpointDefinition(
            "test",
            "Test",
            "/test",
            [ActionDefinition("ok"), ActionDefinition("fail"), ActionDefinition("ok")],
        )
        result = await EndpointExecutor(router).execute(endpoint)
        self.assertFalse(result.ok)
        self.assertEqual(calls, ["ok", "fail"])


if __name__ == "__main__":
    unittest.main()

