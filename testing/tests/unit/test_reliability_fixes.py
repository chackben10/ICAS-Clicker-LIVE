from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

from production_hub.app.clicker_dispatcher import ClickerActionDispatcher
from production_hub.app.lifecycle import start_clicker_listener
from production_hub.core.config.defaults import build_default_config
from production_hub.integrations.obs.service import ObsService
from production_hub.integrations.propresenter.service import ProPresenterService
from production_hub.integrations.propresenter.status_stream import ProPresenterStatusStream


class ReliabilityFixTests(unittest.IsolatedAsyncioTestCase):
    def test_release_build_supports_persistent_codesign_identity(self) -> None:
        script_path = (
            Path(__file__).resolve().parents[3]
            / "Production_Hub_App"
            / "scripts"
            / "build_macos_app.py"
        )
        spec = importlib.util.spec_from_file_location(
            "production_hub_build_macos_app",
            script_path,
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: module}), patch.object(
            sys,
            "path",
            [str(script_path.parent), *sys.path],
        ):
            spec.loader.exec_module(module)

        command = module.codesign_command(
            Path("/tmp/Production Hub.app"),
            "Developer ID Application: ICAS (TEAMID)",
        )
        self.assertIn("Developer ID Application: ICAS (TEAMID)", command)
        self.assertIn("--timestamp", command)
        self.assertIn("runtime", command)

    @staticmethod
    def bible_presentation() -> dict:
        return {
            "presentation": {
                "id": None,
                "groups": [
                    {
                        "name": "John 3:1-21",
                        "slides": [
                            {"text": "John 3:16\rFor God so loved the world"},
                        ],
                    }
                ],
            }
        }

    async def test_propresenter_monitor_reads_share_short_lived_cache(self) -> None:
        service = ProPresenterService(build_default_config().integrations.propresenter)
        payload = {"current": {"id": {"uuid": "cue-1"}, "text": "Verse"}}
        active = {"presentation": {"id": None, "groups": [{"name": "John 3:16"}]}}
        service.client.get_json = AsyncMock(side_effect=[payload, active])

        self.assertEqual(payload, await service.monitored_slide_status())
        self.assertEqual(payload, await service.monitored_slide_status())
        self.assertEqual(active, await service.monitored_active_presentation())
        self.assertEqual(active, await service.monitored_active_presentation())

        self.assertEqual(
            [
                call("/status/slide"),
                call("/presentation/active"),
            ],
            service.client.get_json.await_args_list,
        )

    async def test_official_status_stream_cache_replaces_repeated_api_polls(self) -> None:
        service = ProPresenterService(build_default_config().integrations.propresenter)
        service.client.get_json = AsyncMock()
        with patch.object(ProPresenterStatusStream, "start"):
            stream = service.start_status_stream()
        stream.connected = True
        stream.callback(
            "look/current",
            {"id": {"uuid": "look-1", "name": "Bible"}},
        )
        stream.callback(
            "status/slide",
            {"current": {"uuid": "cue-1", "text": "John 3:16\rFor God so loved the world"}},
        )

        self.assertEqual("Bible", await service.monitored_current_look_name())
        self.assertEqual("cue-1", (await service.monitored_slide_status())["current"]["uuid"])
        service.client.get_json.assert_not_awaited()

        # /presentation/current is the presentation selected in the library,
        # not the one live on screen, so it must never satisfy a caller asking
        # for the active presentation.
        self.assertNotIn("presentation/current", ProPresenterStatusStream.PATHS)
        service.client.get_json.return_value = self.bible_presentation()
        stream.callback("presentation/current", {"presentation": {"id": "library-selection"}})
        self.assertEqual(
            self.bible_presentation(),
            await service.monitored_active_presentation(),
        )
        service.client.get_json.assert_awaited_once_with("/presentation/active")
        with patch.object(stream, "stop"):
            service.stop_status_stream()

    def test_status_stream_parses_official_aggregated_update_format(self) -> None:
        self.assertEqual(
            (
                "presentation/current",
                {"presentation": {"id": None}},
            ),
            ProPresenterStatusStream.parse_update(
                b'{"url":"/v1/presentation/current","data":{"presentation":{"id":null}}}'
            ),
        )

    async def test_stale_status_stream_cache_falls_back_to_one_shared_get(self) -> None:
        service = ProPresenterService(build_default_config().integrations.propresenter)
        stale = {"current": {"uuid": "stale", "text": "Old"}}
        fresh = {"current": {"uuid": "fresh", "text": "New"}}
        service.client.get_json = AsyncMock(return_value=fresh)
        with patch.object(ProPresenterStatusStream, "start"):
            stream = service.start_status_stream()
        stream.connected = True
        service._monitor_cache["slide_status"] = (time.monotonic() - 2.0, stale)

        self.assertEqual(fresh, await service.monitored_slide_status())
        self.assertEqual(fresh, await service.monitored_slide_status())
        service.client.get_json.assert_awaited_once_with("/status/slide")
        with patch.object(stream, "stop"):
            service.stop_status_stream()

    async def test_obs_same_scene_request_is_a_no_op(self) -> None:
        service = ObsService(build_default_config().integrations.obs)
        service.current_scene = "ProPresenter Input"
        service.client.call = AsyncMock()

        self.assertTrue(await service.set_scene("ProPresenter Input"))

        service.client.call.assert_not_awaited()

    def test_clicker_dispatcher_runs_outside_automation_loop_and_preserves_order(self) -> None:
        completed = threading.Event()
        calls: list[str] = []

        class FakeProPresenter:
            async def next_slide(self) -> None:
                calls.append("next")

            async def previous_slide(self) -> None:
                calls.append("previous")
                completed.set()

        logger = SimpleNamespace(info=MagicMock(), warning=MagicMock())
        dispatcher = ClickerActionDispatcher(FakeProPresenter(), logger)
        try:
            self.assertTrue(dispatcher.submit("next"))
            self.assertTrue(dispatcher.submit("previous"))
            self.assertTrue(completed.wait(2))
        finally:
            dispatcher.stop()

        self.assertEqual(["next", "previous"], calls)

    def test_clicker_listener_retries_creation_and_recovers_disabled_tap(self) -> None:
        quartz = types.ModuleType("Quartz")
        quartz.kCGEventKeyDown = 10
        quartz.kCGEventTapDisabledByTimeout = -2
        quartz.kCGEventTapDisabledByUserInput = -1
        quartz.kCGKeyboardEventKeycode = 9
        quartz.kCGSessionEventTap = 1
        quartz.kCGHeadInsertEventTap = 0
        quartz.kCGEventTapOptionDefault = 0
        quartz.kCFRunLoopCommonModes = object()
        quartz.kCFRunLoopDefaultMode = object()
        quartz.CGEventMaskBit = lambda value: 1 << value
        quartz.CGEventGetIntegerValueField = lambda _event, _field: 0
        quartz.CGEventGetLocation = lambda _event: SimpleNamespace(x=0, y=0)
        quartz.CFMachPortCreateRunLoopSource = lambda *_args: object()
        quartz.CFRunLoopAddSource = lambda *_args: None
        quartz.CFRunLoopGetCurrent = lambda: object()

        state = SimpleNamespace(
            create_calls=0,
            callback=None,
            disabled_sent=False,
            enables=[],
            event_mask=None,
        )

        def create_tap(*args):
            state.create_calls += 1
            state.callback = args[4]
            state.event_mask = args[3]
            return None if state.create_calls == 1 else object()

        def enable(tap, enabled):
            state.enables.append((tap, enabled))

        def run_loop(*_args):
            if not state.disabled_sent and state.callback is not None:
                state.disabled_sent = True
                state.callback(None, quartz.kCGEventTapDisabledByTimeout, object(), None)
            time.sleep(0.005)

        quartz.CGEventTapCreate = create_tap
        quartz.CGEventTapEnable = enable
        quartz.CFRunLoopRunInMode = run_loop

        propresenter = SimpleNamespace(
            next_slide=AsyncMock(),
            previous_slide=AsyncMock(),
        )
        logger = SimpleNamespace(info=MagicMock(), warning=MagicMock())
        context = SimpleNamespace(
            propresenter=propresenter,
            logger=logger,
            config=SimpleNamespace(
                integrations=SimpleNamespace(
                    propresenter=SimpleNamespace(
                        enabled=True,
                        next_slide_key_code=69,
                        previous_slide_key_code=78,
                    )
                )
            ),
        )

        with (
            patch.object(sys, "platform", "darwin"),
            patch.dict(sys.modules, {"Quartz": quartz}),
            patch("production_hub.app.lifecycle.CLICKER_LISTENER_RETRY_SECONDS", 0.01),
        ):
            unused_loop = asyncio.new_event_loop()
            try:
                handle = start_clicker_listener(context, unused_loop)
                self.assertIsNotNone(handle)
                deadline = time.monotonic() + 2
                while (state.create_calls < 2 or not state.disabled_sent) and time.monotonic() < deadline:
                    time.sleep(0.01)
                handle.stop()  # type: ignore[union-attr]
            finally:
                unused_loop.close()

        self.assertGreaterEqual(state.create_calls, 2)
        self.assertGreaterEqual(sum(1 for _tap, enabled in state.enables if enabled), 2)
        self.assertEqual(1 << quartz.kCGEventKeyDown, state.event_mask)

    def test_clicker_listener_falls_back_to_keyboard_listen_only(self) -> None:
        quartz = types.ModuleType("Quartz")
        quartz.kCGEventKeyDown = 10
        quartz.kCGEventTapDisabledByTimeout = -2
        quartz.kCGEventTapDisabledByUserInput = -1
        quartz.kCGKeyboardEventKeycode = 9
        quartz.kCGSessionEventTap = 1
        quartz.kCGHeadInsertEventTap = 0
        quartz.kCGEventTapOptionDefault = 0
        quartz.kCGEventTapOptionListenOnly = 1
        quartz.kCFRunLoopCommonModes = object()
        quartz.kCFRunLoopDefaultMode = object()
        quartz.CGEventMaskBit = lambda value: 1 << value
        quartz.CGEventGetIntegerValueField = lambda _event, _field: 0
        state = SimpleNamespace(options=[], enables=[], listen_requests=0)

        quartz.CGPreflightListenEventAccess = lambda: False

        def request_listen_access():
            state.listen_requests += 1
            return True

        quartz.CGRequestListenEventAccess = request_listen_access
        quartz.CFMachPortCreateRunLoopSource = lambda *_args: object()
        quartz.CFRunLoopAddSource = lambda *_args: None
        quartz.CFRunLoopGetCurrent = lambda: object()

        def create_tap(*args):
            state.options.append(args[2])
            return object() if args[2] == quartz.kCGEventTapOptionListenOnly else None

        quartz.CGEventTapCreate = create_tap
        quartz.CGEventTapEnable = lambda tap, enabled: state.enables.append((tap, enabled))
        quartz.CFRunLoopRunInMode = lambda *_args: time.sleep(0.005)

        logger = SimpleNamespace(info=MagicMock(), warning=MagicMock())
        context = SimpleNamespace(
            propresenter=SimpleNamespace(
                next_slide=AsyncMock(),
                previous_slide=AsyncMock(),
            ),
            logger=logger,
            config=SimpleNamespace(
                integrations=SimpleNamespace(
                    propresenter=SimpleNamespace(
                        enabled=True,
                        next_slide_key_code=69,
                        previous_slide_key_code=78,
                    )
                )
            ),
        )

        with (
            patch.object(sys, "platform", "darwin"),
            patch.dict(sys.modules, {"Quartz": quartz}),
        ):
            unused_loop = asyncio.new_event_loop()
            try:
                handle = start_clicker_listener(context, unused_loop)
                self.assertIsNotNone(handle)
                deadline = time.monotonic() + 2
                while len(state.options) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                handle.stop()  # type: ignore[union-attr]
            finally:
                unused_loop.close()

        self.assertEqual(
            [quartz.kCGEventTapOptionDefault, quartz.kCGEventTapOptionListenOnly],
            state.options[:2],
        )
        self.assertEqual(1, state.listen_requests)
        logger.warning.assert_any_call(
            "clicker_listener_listen_only",
            "Clicker listener started in keyboard listen-only mode",
        )


if __name__ == "__main__":
    unittest.main()
