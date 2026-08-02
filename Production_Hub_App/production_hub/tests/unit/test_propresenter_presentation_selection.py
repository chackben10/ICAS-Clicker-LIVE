from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

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
        self.service.focused_playlist = AsyncMock(return_value={"playlist": None, "item": None})
        self.service.active_presentation = AsyncMock(
            return_value={"presentation": {"id": {"uuid": "song/uuid"}}}
        )
        self.service.focused_presentation = AsyncMock(return_value={})
        self.service.client.trigger = AsyncMock(return_value=True)

        result = await self.service.trigger_presentation_slide("song/uuid", 7)

        self.assertTrue(result)
        self.service.client.trigger.assert_awaited_once_with(
            "/presentation/song%2Fuuid/7/trigger"
        )

    async def test_playlist_reads_quote_uuid_and_use_focused_endpoint(self) -> None:
        self.service.client.get_json = AsyncMock(side_effect=[{"playlist": {}}, {"items": []}])

        focused = await self.service.focused_playlist()
        playlist = await self.service.playlist_by_uuid("service/with spaces")

        self.assertEqual({"playlist": {}}, focused)
        self.assertEqual({"items": []}, playlist)
        self.assertEqual(
            [
                unittest.mock.call("/playlist/focused"),
                unittest.mock.call("/playlist/service%2Fwith%20spaces"),
            ],
            self.service.client.get_json.await_args_list,
        )

    @staticmethod
    def playlist_item(
        index: int,
        uuid: str = "song-uuid",
        *,
        item_type: str = "presentation",
        destination: str = "presentation",
        hidden: bool = False,
    ) -> dict:
        item = {
            "id": {"uuid": f"item-{index}", "name": f"Item {index}", "index": index},
            "type": item_type,
            "destination": destination,
            "is_hidden": hidden,
        }
        if item_type == "presentation":
            item["presentation_info"] = {"presentation_uuid": uuid}
        return item

    @staticmethod
    def presentation_payload(uuid: str = "song-uuid", *, first_enabled: bool = True) -> dict:
        return {
            "presentation": {
                "id": {"uuid": uuid, "name": "Song"},
                "groups": [{"slides": [{"enabled": first_enabled, "text": "First"}]}],
            }
        }

    async def test_searched_song_uses_next_eligible_occurrence_in_focused_playlist(self) -> None:
        self.service.focused_playlist = AsyncMock(
            return_value={
                "playlist": {"uuid": "playlist-uuid"},
                "item": {"index": 5},
            }
        )
        self.service.playlist_by_uuid = AsyncMock(
            return_value={
                "items": [
                    self.playlist_item(2),
                    self.playlist_item(6, hidden=True),
                    self.playlist_item(7, destination="announcements"),
                    self.playlist_item(8),
                    self.playlist_item(9, item_type="placeholder"),
                ]
            }
        )
        self.service.presentation_by_uuid = AsyncMock(
            return_value=self.presentation_payload(first_enabled=True)
        )
        self.service.client.trigger = AsyncMock(return_value=True)

        await self.service.trigger_presentation_slide("song-uuid", 4)

        self.service.client.trigger.assert_awaited_once_with(
            "/playlist/playlist-uuid/8/4/trigger"
        )

    async def test_searched_song_falls_back_to_closest_earlier_occurrence(self) -> None:
        self.service.focused_playlist = AsyncMock(
            return_value={
                "playlist": {"uuid": "playlist-uuid"},
                "item": {"index": 10},
            }
        )
        self.service.playlist_by_uuid = AsyncMock(
            return_value={"items": [self.playlist_item(2), self.playlist_item(7)]}
        )
        self.service.presentation_by_uuid = AsyncMock(
            return_value=self.presentation_payload(first_enabled=True)
        )
        self.service.client.trigger = AsyncMock(return_value=True)

        await self.service.trigger_presentation_slide("song-uuid", 3)

        self.service.client.trigger.assert_awaited_once_with(
            "/playlist/playlist-uuid/7/3/trigger"
        )

    async def test_searched_current_playlist_song_uses_current_occurrence_without_warmup(self) -> None:
        self.service.focused_playlist = AsyncMock(
            return_value={
                "playlist": {"uuid": "playlist-uuid"},
                "item": {"index": 5},
            }
        )
        self.service.playlist_by_uuid = AsyncMock(
            return_value={"items": [self.playlist_item(5), self.playlist_item(8)]}
        )
        self.service.presentation_by_uuid = AsyncMock(
            return_value=self.presentation_payload(first_enabled=False)
        )
        self.service.client.trigger = AsyncMock(return_value=True)

        await self.service.trigger_presentation_slide("song-uuid", 3)

        self.service.client.trigger.assert_awaited_once_with(
            "/playlist/playlist-uuid/5/3/trigger"
        )
        self.service.presentation_by_uuid.assert_not_awaited()

    async def test_searched_song_absent_from_playlist_uses_uuid_route(self) -> None:
        self.service.focused_playlist = AsyncMock(
            return_value={
                "playlist": {"uuid": "playlist-uuid"},
                "item": {"index": 5},
            }
        )
        self.service.playlist_by_uuid = AsyncMock(
            return_value={"items": [self.playlist_item(6, "different-uuid")]}
        )
        self.service.active_presentation = AsyncMock(return_value={})
        self.service.focused_presentation = AsyncMock(return_value={})
        self.service.presentation_by_uuid = AsyncMock(
            return_value=self.presentation_payload(first_enabled=True)
        )
        self.service.client.trigger = AsyncMock(return_value=True)

        await self.service.trigger_presentation_slide("song-uuid", 2)

        self.service.client.trigger.assert_awaited_once_with(
            "/presentation/song-uuid/2/trigger"
        )

    async def test_focused_playlist_load_failure_does_not_fall_back_and_lose_focus(self) -> None:
        self.service.focused_playlist = AsyncMock(
            return_value={
                "playlist": {"uuid": "playlist-uuid"},
                "item": {"index": 5},
            }
        )
        self.service.playlist_by_uuid = AsyncMock(side_effect=TimeoutError("playlist unavailable"))
        self.service.client.trigger = AsyncMock(return_value=True)

        with self.assertRaisesRegex(TimeoutError, "playlist unavailable"):
            await self.service.trigger_presentation_slide("song-uuid", 2)

        self.service.client.trigger.assert_not_awaited()

    async def test_playlist_switch_primes_disabled_first_slide_then_triggers_requested_index(self) -> None:
        item = self.playlist_item(8)
        self.service.playlist_by_uuid = AsyncMock(return_value={"items": [item]})
        self.service.focused_playlist = AsyncMock(
            return_value={"playlist": {"uuid": "playlist-uuid"}, "item": {"index": 5}}
        )
        self.service.presentation_by_uuid = AsyncMock(
            return_value=self.presentation_payload(first_enabled=False)
        )
        self.service._wait_for_target_load = AsyncMock()
        self.service.client.trigger = AsyncMock(return_value=True)

        await self.service.trigger_playlist_presentation_slide("playlist-uuid", 8, 4)

        self.assertEqual(
            [
                unittest.mock.call("/playlist/playlist-uuid/8/0/trigger"),
                unittest.mock.call("/playlist/playlist-uuid/8/4/trigger"),
            ],
            self.service.client.trigger.await_args_list,
        )
        self.service._wait_for_target_load.assert_awaited_once_with(
            "song-uuid", "playlist-uuid", 8
        )

    async def test_warmup_keeps_full_settling_window_after_target_loads(self) -> None:
        self.service._target_loaded = AsyncMock(return_value=True)

        with patch(
            "production_hub.integrations.propresenter.service.asyncio.sleep",
            new=AsyncMock(),
        ) as mocked_sleep:
            await self.service._wait_for_target_load("song-uuid", "playlist-uuid", 8)

        mocked_sleep.assert_awaited_once()
        self.assertGreater(mocked_sleep.await_args.args[0], 0.9)

    async def test_switch_timeout_still_triggers_requested_index(self) -> None:
        self.service.PRESENTATION_SWITCH_WAIT_SECONDS = 0.01
        self.service.focused_playlist = AsyncMock(return_value={"playlist": None, "item": None})
        self.service.active_presentation = AsyncMock(return_value={})
        self.service.focused_presentation = AsyncMock(return_value={})
        self.service.presentation_by_uuid = AsyncMock(
            return_value=self.presentation_payload(first_enabled=False)
        )
        self.service._target_loaded = AsyncMock(return_value=False)
        self.service.client.trigger = AsyncMock(return_value=True)

        await self.service.trigger_presentation_slide("song-uuid", 5)

        self.assertEqual(
            [
                unittest.mock.call("/presentation/song-uuid/0/trigger"),
                unittest.mock.call("/presentation/song-uuid/5/trigger"),
            ],
            self.service.client.trigger.await_args_list,
        )

    async def test_switch_without_a_first_slide_triggers_requested_index_once(self) -> None:
        self.service.focused_playlist = AsyncMock(return_value={"playlist": None, "item": None})
        self.service.active_presentation = AsyncMock(return_value={})
        self.service.focused_presentation = AsyncMock(return_value={})
        self.service.presentation_by_uuid = AsyncMock(
            return_value={"presentation": {"id": {"uuid": "song-uuid"}, "groups": []}}
        )
        self.service.client.trigger = AsyncMock(return_value=True)

        await self.service.trigger_presentation_slide("song-uuid", 5)

        self.service.client.trigger.assert_awaited_once_with(
            "/presentation/song-uuid/5/trigger"
        )

    async def test_current_playlist_item_and_index_zero_skip_warmup(self) -> None:
        item = self.playlist_item(8)
        self.service.playlist_by_uuid = AsyncMock(return_value={"items": [item]})
        self.service.focused_playlist = AsyncMock(
            return_value={"playlist": {"uuid": "playlist-uuid"}, "item": {"index": 8}}
        )
        self.service.presentation_by_uuid = AsyncMock(
            return_value=self.presentation_payload(first_enabled=False)
        )
        self.service.client.trigger = AsyncMock(return_value=True)

        await self.service.trigger_playlist_presentation_slide("playlist-uuid", 8, 0)

        self.service.client.trigger.assert_awaited_once_with(
            "/playlist/playlist-uuid/8/0/trigger"
        )
        self.service.presentation_by_uuid.assert_not_awaited()

    async def test_playlist_trigger_rejects_non_presentable_items(self) -> None:
        cases = [
            self.playlist_item(1, item_type="header"),
            self.playlist_item(2, item_type="placeholder"),
            self.playlist_item(3, hidden=True),
            self.playlist_item(4, destination="announcements"),
        ]
        self.service.client.trigger = AsyncMock(return_value=True)
        for item in cases:
            with self.subTest(item=item):
                self.service.playlist_by_uuid = AsyncMock(return_value={"items": [item]})
                with self.assertRaisesRegex(ValueError, "not an allowed presentation"):
                    await self.service.trigger_playlist_presentation_slide(
                        "playlist-uuid",
                        item["id"]["index"],
                        1,
                    )
        self.service.client.trigger.assert_not_awaited()

    async def test_trigger_presentation_slide_rejects_negative_index(self) -> None:
        self.service.client.trigger = AsyncMock(return_value=True)

        with self.assertRaisesRegex(ValueError, "nonnegative"):
            await self.service.trigger_presentation_slide("song-uuid", -1)

        self.service.client.trigger.assert_not_awaited()

    def test_default_endpoints_expose_read_and_post_only_trigger(self) -> None:
        endpoints = {endpoint.key: endpoint for endpoint in build_default_endpoints()}
        read_endpoint = endpoints["presentation_by_uuid"]
        trigger_endpoint = endpoints["trigger_presentation_slide"]
        focused_playlist = endpoints["focused_playlist"]
        playlist_by_uuid = endpoints["playlist_by_uuid"]
        playlist_trigger = endpoints["trigger_playlist_presentation_slide"]
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
        self.assertEqual("/playlist/focused", focused_playlist.route)
        self.assertEqual(["GET"], focused_playlist.allowed_methods)
        self.assertEqual("read", focused_playlist.behavior_mode)
        self.assertEqual("/playlist/{uuid}", playlist_by_uuid.route)
        self.assertEqual(["GET"], playlist_by_uuid.allowed_methods)
        self.assertEqual(
            "/playlist/{playlist_uuid}/{item_index:int}/{slide_index:int}/trigger",
            playlist_trigger.route,
        )
        self.assertEqual(["POST"], playlist_trigger.allowed_methods)
        self.assertEqual(
            ["playlist_uuid", "item_index", "slide_index"],
            [item.name for item in playlist_trigger.inputs],
        )
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

    def test_playlist_trigger_endpoint_matches_three_path_inputs(self) -> None:
        registry = EndpointRegistry(build_default_endpoints())

        matches = registry.matches("/playlist/service-uuid/8/3/trigger", "POST")

        self.assertEqual(1, len(matches))
        endpoint, params = matches[0]
        self.assertEqual("trigger_playlist_presentation_slide", endpoint.key)
        self.assertEqual(
            {"playlist_uuid": "service-uuid", "item_index": 8, "slide_index": 3},
            params,
        )
        self.assertEqual([], registry.matches("/playlist/service-uuid/8/3/trigger", "GET"))

    def test_existing_endpoint_profiles_receive_required_playlist_endpoints(self) -> None:
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            context = build_context(data_dir)
            playlist_keys = {
                "focused_playlist",
                "playlist_by_uuid",
                "trigger_playlist_presentation_slide",
            }
            context.config_repository.save_endpoints(
                [
                    endpoint
                    for endpoint in context.endpoint_registry.all()
                    if endpoint.key not in playlist_keys
                ]
            )

            repaired = build_context(data_dir)

            self.assertTrue(playlist_keys.issubset({item.key for item in repaired.endpoint_registry.all()}))

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

    def test_playlist_trigger_is_forbidden_when_clicker_activation_is_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            context.propresenter.trigger_playlist_presentation_slide = AsyncMock(return_value=True)
            context.runtime_state_repo.update(
                lambda state: setattr(state, "clicker_presentation_activation_enabled", False)
            )

            response = TestClient(create_app(context)).post(
                "/playlist/service-uuid/8/3/trigger"
            )

            self.assertEqual(403, response.status_code)
            self.assertEqual(
                CLICKER_PRESENTATION_ACTIVATION_DISABLED,
                response.json()["detail"]["error"],
            )
            context.propresenter.trigger_playlist_presentation_slide.assert_not_awaited()

    def test_custom_playlist_trigger_cannot_bypass_disabled_clicker_activation(self) -> None:
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            context.endpoint_registry.remove("trigger_playlist_presentation_slide")
            context.endpoint_registry.register(
                EndpointDefinition(
                    "custom_playlist_trigger",
                    "Custom Playlist Trigger",
                    "/playlist/{playlist_uuid}/{item_index:int}/{slide_index:int}/trigger",
                    [
                        ActionDefinition(
                            "propresenter.trigger_playlist_presentation_slide",
                            {
                                "playlist_uuid": "{{playlist_uuid}}",
                                "item_index": "{{item_index}}",
                                "slide_index": "{{slide_index}}",
                            },
                        )
                    ],
                    allowed_methods=["POST"],
                )
            )
            context.propresenter.trigger_playlist_presentation_slide = AsyncMock(return_value=True)
            context.runtime_state_repo.update(
                lambda state: setattr(state, "clicker_presentation_activation_enabled", False)
            )

            response = TestClient(create_app(context)).post(
                "/playlist/service-uuid/8/3/trigger"
            )

            self.assertEqual(403, response.status_code)
            context.propresenter.trigger_playlist_presentation_slide.assert_not_awaited()

    def test_playlist_reads_and_trigger_execute_through_configured_endpoints(self) -> None:
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            context.propresenter.focused_playlist = AsyncMock(
                return_value={"playlist": {"uuid": "service-uuid"}, "item": {"index": 8}}
            )
            context.propresenter.playlist_by_uuid = AsyncMock(
                return_value={"id": {"uuid": "service-uuid"}, "items": []}
            )
            context.propresenter.trigger_playlist_presentation_slide = AsyncMock(return_value=True)
            client = TestClient(create_app(context))

            focused = client.get("/playlist/focused")
            playlist = client.get("/playlist/service-uuid")
            triggered = client.post("/playlist/service-uuid/8/3/trigger")

            self.assertEqual(200, focused.status_code)
            self.assertEqual("service-uuid", focused.json()["playlist"]["uuid"])
            self.assertEqual(200, playlist.status_code)
            self.assertEqual("service-uuid", playlist.json()["id"]["uuid"])
            self.assertEqual(
                {
                    "playlist_uuid": "service-uuid",
                    "item_index": 8,
                    "slide_index": 3,
                },
                triggered.json(),
            )
            context.propresenter.trigger_playlist_presentation_slide.assert_awaited_once_with(
                "service-uuid",
                8,
                3,
            )

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
