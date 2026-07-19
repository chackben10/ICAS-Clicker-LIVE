from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from production_hub.app.bootstrap import build_context
from production_hub.core.automation.models import AutomationDefinition
from production_hub.core.config.defaults import build_default_endpoints
from production_hub.core.config.repository import ConfigRepository
from production_hub.core.config.models import AppPaths
from production_hub.core.endpoints.models import ActionDefinition, EndpointDefinition
from production_hub.state.undo_manager import UndoManager


class UndoAndRepairTests(unittest.TestCase):
    def test_undo_manager_keeps_at_least_100_items_and_redoes(self) -> None:
        value = {"n": 0}
        manager = UndoManager(max_items=100)
        for index in range(105):
            before = index
            after = index + 1
            manager.record(
                f"change {index}",
                lambda before=before: value.update(n=before),
                lambda after=after: value.update(n=after),
            )
        self.assertTrue(manager.can_undo())
        self.assertEqual(len(manager._undo_stack), 100)
        self.assertEqual(manager.undo(), "Undid: change 104")
        self.assertEqual(value["n"], 104)
        self.assertTrue(manager.can_redo())
        self.assertEqual(manager.redo(), "Redid: change 104")
        self.assertEqual(value["n"], 105)

    def test_build_context_repairs_missing_audio_clear_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp))
            repo = ConfigRepository(paths)
            repo.save_endpoints(
                [
                    EndpointDefinition(
                        "next_slide",
                        "Next Slide",
                        "/next",
                        [ActionDefinition("propresenter.next_slide")],
                    )
                ]
            )
            context = build_context(Path(tmp))
            self.assertIsNotNone(context.endpoint_registry.get("audio_clear"))
            reloaded = ConfigRepository(paths).load_endpoints()
            self.assertTrue(any(endpoint.key == "audio_clear" for endpoint in reloaded))

    def test_build_context_repairs_builtin_endpoint_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp))
            repo = ConfigRepository(paths)
            repo.save_endpoints(
                [
                    EndpointDefinition(
                        "audio_trigger",
                        "Audio Trigger",
                        "/audio/trigger",
                        [ActionDefinition("propresenter.audio_trigger")],
                    )
                ]
            )
            context = build_context(Path(tmp))
            endpoint = context.endpoint_registry.get("audio_trigger")
            self.assertIsNotNone(endpoint)
            self.assertEqual([input_def.name for input_def in endpoint.inputs], ["playlist", "track"])
            reloaded = ConfigRepository(paths).load_endpoints()[0]
            self.assertEqual(reloaded.inputs[0].option_source, "audio_playlists")

    def test_build_context_repairs_legacy_preset_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp))
            repo = ConfigRepository(paths)
            legacy_presets = []
            for endpoint in build_default_endpoints():
                if endpoint.route != "/preset":
                    continue
                endpoint.match_rules = []
                endpoint.allowed_methods = ["GET", "POST"]
                if endpoint.key in {"camera", "service_logo"}:
                    endpoint.actions = [
                        action
                        for action in endpoint.actions
                        if action.action_type != "propresenter.clear_slide"
                    ]
                legacy_presets.append(endpoint)
            repo.save_endpoints(legacy_presets)

            context = build_context(Path(tmp))
            expected_keys = {
                "stream_beginning",
                "camera",
                "show_slides",
                "service_logo",
                "testimonies",
                "ending_stream",
                "clear_slide",
                "safely_clear_slide",
                "nsc_setup",
            }

            for preset_key in expected_keys:
                match = context.endpoint_registry.matching_endpoint(
                    "/preset",
                    "POST",
                    {"preset": preset_key},
                )
                self.assertIsNotNone(match)
                self.assertEqual(match[0].key, preset_key)
                self.assertEqual(match[0].allowed_methods, ["POST"])

            reloaded = {
                endpoint.key: endpoint
                for endpoint in ConfigRepository(paths).load_endpoints()
                if endpoint.key in expected_keys
            }
            self.assertEqual(set(reloaded), expected_keys)
            for endpoint in reloaded.values():
                self.assertEqual(len(endpoint.match_rules), 1)
                self.assertEqual(endpoint.match_rules[0].value, endpoint.key)

            for endpoint_key in {"camera", "service_logo"}:
                clear_conditions = [
                    action.condition
                    for action in reloaded[endpoint_key].actions
                    if action.action_type == "propresenter.clear_slide"
                ]
                self.assertCountEqual(clear_conditions, ["{{clearslide}}", "{{safeclear}}"])

    def test_build_context_repairs_page_endpoint_response_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp))
            repo = ConfigRepository(paths)
            repo.save_endpoints(
                [
                    EndpointDefinition(
                        "auto_show",
                        "Auto Show",
                        "/auto-show",
                        [ActionDefinition("runtime.auto_show")],
                    ),
                    EndpointDefinition(
                        "audio_active",
                        "Active Audio",
                        "/audio/active",
                        [ActionDefinition("propresenter.audio_active")],
                    ),
                    EndpointDefinition(
                        "clicker_activation",
                        "Clicker Presentation Activation",
                        "/clicker-presentation-activation",
                        [ActionDefinition("runtime.get_clicker_presentation_activation")],
                    ),
                ]
            )
            context = build_context(Path(tmp))

            auto_show = context.endpoint_registry.get("auto_show")
            audio_active = context.endpoint_registry.get("audio_active")
            clicker_activation = context.endpoint_registry.get("clicker_activation")

            self.assertIsNotNone(auto_show)
            self.assertIsNotNone(audio_active)
            self.assertIsNotNone(clicker_activation)
            self.assertEqual(auto_show.response.response_type, "last_action_data")
            self.assertEqual(audio_active.response.response_type, "plain_text")
            self.assertEqual(audio_active.response.success_body, "{{text}}")
            self.assertEqual(clicker_activation.response.response_type, "last_action_data")

    def test_build_context_repairs_audio_trigger_methods(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp))
            repo = ConfigRepository(paths)
            repo.save_endpoints(
                [
                    EndpointDefinition(
                        "audio_trigger",
                        "Audio Trigger",
                        "/audio/trigger",
                        [ActionDefinition("propresenter.audio_trigger")],
                        allowed_methods=["POST"],
                    )
                ]
            )
            context = build_context(Path(tmp))
            endpoint = context.endpoint_registry.get("audio_trigger")

            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.allowed_methods, ["GET", "POST"])

    def test_default_endpoints_include_camera_preset_builder_endpoint(self) -> None:
        endpoint = next(item for item in build_default_endpoints() if item.key == "camera_preset_recall")
        self.assertEqual(endpoint.route, "/camera/preset")
        self.assertEqual(endpoint.allowed_methods, ["POST"])
        self.assertEqual(endpoint.inputs[0].kind, "integer")
        self.assertEqual(endpoint.inputs[0].min_value, "1")
        self.assertEqual(endpoint.inputs[0].max_value, "100")

    def test_build_context_moves_debug_api_off_debug_page_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp))
            repo = ConfigRepository(paths)
            repo.save_endpoints(
                [
                    EndpointDefinition(
                        "debug",
                        "Debug Snapshot",
                        "/debug",
                        [ActionDefinition("system.get_debug")],
                    )
                ]
            )
            context = build_context(Path(tmp))
            endpoint = context.endpoint_registry.get("debug")
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.route, "/api/debug")

    def test_build_context_repairs_blank_builtin_automation_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp))
            repo = ConfigRepository(paths)
            repo.save_automations(
                [
                    AutomationDefinition(
                        key="auto_show_slides",
                        name="Auto Show Slides",
                        trigger="presentation_state_changed",
                        conditions=[],
                        actions=[],
                    )
                ]
            )
            context = build_context(Path(tmp))
            repaired = context.automation_engine.definitions["auto_show_slides"]
            self.assertGreater(len(repaired.conditions), 0)
            self.assertGreater(len(repaired.actions), 0)
            reloaded = ConfigRepository(paths).load_automations()[0]
            self.assertGreater(len(reloaded.conditions), 0)
            self.assertGreater(len(reloaded.actions), 0)


if __name__ == "__main__":
    unittest.main()
