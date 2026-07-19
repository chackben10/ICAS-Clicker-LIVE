from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from production_hub.api.clicker_policy import CLICKER_PRESENTATION_ACTIVATION_DISABLED
from production_hub.api.server import create_app
from production_hub.app.bootstrap import build_context
from production_hub.core.config.defaults import build_default_endpoints
from production_hub.core.config.models import ProPresenterConfig
from production_hub.core.endpoints.models import ActionDefinition, EndpointDefinition
from production_hub.core.endpoints.registry import EndpointRegistry
from production_hub.integrations.propresenter.service import ProPresenterService


class ProPresenterPresentationSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = ProPresenterService(ProPresenterConfig())

    async def test_presentation_by_uuid_quotes_the_path_segment(self) -> None:
        payload = {"presentation": {"id": {"uuid": "song/uuid with spaces"}, "groups": []}}
        self.service.client.get_json = AsyncMock(return_value=payload)

        result = await self.service.presentation_by_uuid("song/uuid with spaces")

        self.assertEqual(payload, result)
        self.service.client.get_json.assert_awaited_once_with(
            "/presentation/song%2Fuuid%20with%20spaces"
        )

    async def test_trigger_presentation_slide_targets_uuid_and_index(self) -> None:
        self.service.client.trigger = AsyncMock(return_value=True)

        result = await self.service.trigger_presentation_slide("song/uuid", 7)

        self.assertTrue(result)
        self.service.client.trigger.assert_awaited_once_with(
            "/presentation/song%2Fuuid/7/trigger"
        )

    async def test_trigger_presentation_slide_rejects_negative_index(self) -> None:
        self.service.client.trigger = AsyncMock(return_value=True)

        with self.assertRaisesRegex(ValueError, "nonnegative"):
            await self.service.trigger_presentation_slide("song-uuid", -1)

        self.service.client.trigger.assert_not_awaited()

    def test_default_endpoints_expose_read_and_post_only_trigger(self) -> None:
        endpoints = {endpoint.key: endpoint for endpoint in build_default_endpoints()}
        read_endpoint = endpoints["presentation_by_uuid"]
        trigger_endpoint = endpoints["trigger_presentation_slide"]
        activation_get = endpoints["clicker_presentation_activation_get"]
        activation_set = endpoints["clicker_presentation_activation_set"]

        self.assertEqual("/presentation/{uuid}", read_endpoint.route)
        self.assertEqual(["GET"], read_endpoint.allowed_methods)
        self.assertEqual("read", read_endpoint.behavior_mode)
        self.assertEqual("last_action_data", read_endpoint.response.response_type)
        self.assertEqual(["uuid"], [item.name for item in read_endpoint.inputs])

        self.assertEqual("/presentation/{uuid}/{index:int}/trigger", trigger_endpoint.route)
        self.assertEqual(["POST"], trigger_endpoint.allowed_methods)
        self.assertEqual(["uuid", "index"], [item.name for item in trigger_endpoint.inputs])
        self.assertEqual("0", trigger_endpoint.inputs[1].min_value)
        self.assertEqual("/clicker-presentation-activation", activation_get.route)
        self.assertEqual(["GET"], activation_get.allowed_methods)
        self.assertEqual("read", activation_get.behavior_mode)
        self.assertEqual("runtime.get_clicker_presentation_activation", activation_get.actions[0].action_type)
        self.assertEqual("/clicker-presentation-activation", activation_set.route)
        self.assertEqual(["POST"], activation_set.allowed_methods)
        self.assertEqual("runtime.clicker_presentation_activation", activation_set.actions[0].action_type)
        self.assertTrue(activation_set.inputs[0].required)

    def test_trigger_endpoint_matches_uuid_and_integer_index(self) -> None:
        registry = EndpointRegistry(build_default_endpoints())

        matches = registry.matches("/presentation/song-uuid/12/trigger", "POST")

        self.assertEqual(1, len(matches))
        endpoint, params = matches[0]
        self.assertEqual("trigger_presentation_slide", endpoint.key)
        self.assertEqual({"uuid": "song-uuid", "index": 12}, params)
        self.assertEqual([], registry.matches("/presentation/song-uuid/12/trigger", "GET"))

    def test_configured_trigger_is_forbidden_when_clicker_activation_is_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            context.propresenter.trigger_presentation_slide = AsyncMock(return_value=True)
            context.runtime_state_repo.update(
                lambda state: setattr(state, "clicker_presentation_activation_enabled", False)
            )

            response = TestClient(create_app(context)).post(
                "/presentation/song-uuid/3/trigger"
            )

            self.assertEqual(403, response.status_code)
            self.assertEqual(
                CLICKER_PRESENTATION_ACTIVATION_DISABLED,
                response.json()["detail"]["error"],
            )
            context.propresenter.trigger_presentation_slide.assert_not_awaited()

    def test_custom_endpoint_key_cannot_bypass_disabled_clicker_activation(self) -> None:
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            context.endpoint_registry.remove("trigger_presentation_slide")
            context.endpoint_registry.register(
                EndpointDefinition(
                    "custom_song_trigger",
                    "Custom Song Trigger",
                    "/presentation/{uuid}/{index:int}/trigger",
                    [
                        ActionDefinition(
                            "propresenter.trigger_presentation_slide",
                            {"uuid": "{{uuid}}", "index": "{{index}}"},
                        )
                    ],
                    allowed_methods=["POST"],
                )
            )
            context.propresenter.trigger_presentation_slide = AsyncMock(return_value=True)
            context.runtime_state_repo.update(
                lambda state: setattr(state, "clicker_presentation_activation_enabled", False)
            )

            response = TestClient(create_app(context)).post(
                "/presentation/song-uuid/3/trigger"
            )

            self.assertEqual(403, response.status_code)
            self.assertEqual(
                CLICKER_PRESENTATION_ACTIVATION_DISABLED,
                response.json()["detail"]["error"],
            )
            context.propresenter.trigger_presentation_slide.assert_not_awaited()

    def test_disabled_clicker_activation_keeps_song_book_reads_and_live_focus_available(self) -> None:
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            presentation = {"presentation": {"id": {"uuid": "song-uuid"}, "groups": []}}
            context.propresenter.presentation_by_uuid = AsyncMock(return_value=presentation)
            context.propresenter.focus_slide = AsyncMock(return_value=True)
            context.runtime_state_repo.update(
                lambda state: setattr(state, "clicker_presentation_activation_enabled", False)
            )
            client = TestClient(create_app(context))

            preview_response = client.get("/presentation/song-uuid")
            focus_response = client.get("/focus", params={"index": 2})

            self.assertEqual(200, preview_response.status_code)
            self.assertEqual(presentation, preview_response.json())
            self.assertEqual(200, focus_response.status_code)
            context.propresenter.presentation_by_uuid.assert_awaited_once_with("song-uuid")
            context.propresenter.focus_slide.assert_awaited_once_with(2)

    def test_configured_trigger_runs_when_clicker_activation_is_enabled(self) -> None:
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            context.propresenter.trigger_presentation_slide = AsyncMock(return_value=True)

            response = TestClient(create_app(context)).post(
                "/presentation/song-uuid/4/trigger"
            )

            self.assertEqual(200, response.status_code)
            self.assertEqual({"uuid": "song-uuid", "index": 4}, response.json())
            context.propresenter.trigger_presentation_slide.assert_awaited_once_with("song-uuid", 4)

    def test_fallback_trigger_is_forbidden_when_clicker_activation_is_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            context.endpoint_registry.remove("trigger_presentation_slide")
            context.propresenter.trigger_presentation_slide = AsyncMock(return_value=True)
            context.runtime_state_repo.update(
                lambda state: setattr(state, "clicker_presentation_activation_enabled", False)
            )

            response = TestClient(create_app(context)).post(
                "/presentation/song-uuid/5/trigger"
            )

            self.assertEqual(403, response.status_code)
            self.assertEqual(
                CLICKER_PRESENTATION_ACTIVATION_DISABLED,
                response.json()["detail"]["error"],
            )
            context.propresenter.trigger_presentation_slide.assert_not_awaited()

    def test_fallback_trigger_runs_when_clicker_activation_is_enabled(self) -> None:
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            context.endpoint_registry.remove("trigger_presentation_slide")
            context.propresenter.trigger_presentation_slide = AsyncMock(return_value=True)

            response = TestClient(create_app(context)).post(
                "/presentation/song-uuid/6/trigger"
            )

            self.assertEqual(200, response.status_code)
            self.assertEqual(
                {"ok": True, "uuid": "song-uuid", "index": 6},
                response.json(),
            )
            context.propresenter.trigger_presentation_slide.assert_awaited_once_with("song-uuid", 6)

    def test_clicker_activation_configured_endpoints_read_write_and_persist(self) -> None:
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            context = build_context(data_dir)
            client = TestClient(create_app(context))

            initial = client.get("/clicker-presentation-activation")
            updated = client.post(
                "/clicker-presentation-activation",
                json={"enabled": False},
            )
            current = client.get("/clicker-presentation-activation")

            self.assertEqual({"enabled": True}, initial.json())
            self.assertEqual({"enabled": False}, updated.json())
            self.assertEqual({"enabled": False}, current.json())
            self.assertEqual("no-store", current.headers.get("cache-control"))
            reloaded = build_context(data_dir).runtime_state_repo.load()
            self.assertFalse(reloaded.clicker_presentation_activation_enabled)

    def test_clicker_activation_fallback_routes_read_and_write(self) -> None:
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            context.endpoint_registry.remove("clicker_presentation_activation_get")
            context.endpoint_registry.remove("clicker_presentation_activation_set")
            client = TestClient(create_app(context))

            response = client.post(
                "/clicker-presentation-activation",
                json={"enabled": False},
            )

            self.assertEqual(200, response.status_code)
            self.assertEqual({"enabled": False}, response.json())
            self.assertEqual(
                {"enabled": False},
                client.get("/clicker-presentation-activation").json(),
            )


if __name__ == "__main__":
    unittest.main()
