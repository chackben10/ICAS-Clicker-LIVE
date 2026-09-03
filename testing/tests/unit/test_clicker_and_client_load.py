"""Phase 2: stop Production Hub competing with ProPresenter.

Each test here pins one source of avoidable load on the operator's critical
path, so a later change cannot quietly put it back.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock

import pytest

from production_hub.app.clicker_dispatcher import ClickerActionDispatcher
from production_hub.core.config.models import ProPresenterConfig
from production_hub.core.metrics.press_to_cue import PressToCueRecorder
from production_hub.integrations.propresenter.client import (
    ProPresenterClient,
    ProPresenterHttpError,
    ProPresenterTransportError,
)
from production_hub.integrations.propresenter.service import ProPresenterService
from testing.harness.fake_servers import ScriptedHttpServer


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def _record(self, level: str, event: str, _message: str, **metadata) -> None:
        self.events.append((level, event, metadata))

    def info(self, event, message, **metadata):
        self._record("info", event, message, **metadata)

    def warning(self, event, message, **metadata):
        self._record("warning", event, message, **metadata)

    def error(self, event, message, **metadata):
        self._record("error", event, message, **metadata)

    def names(self) -> list[str]:
        return [event for _level, event, _metadata in self.events]


class FakeProPresenter:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []
        self.loops: list[int] = []
        self.macros: list[str] = []
        self.done = threading.Event()
        self.config = ProPresenterConfig(host="127.0.0.1", port=1)

    async def next_slide(self) -> bool:
        return await self._record("next")

    async def previous_slide(self) -> bool:
        return await self._record("previous")

    async def trigger_macro(self, macro_name: str) -> bool:
        self.macros.append(macro_name)
        return await self._record("bible")

    async def _record(self, direction: str) -> bool:
        self.calls.append(direction)
        self.loops.append(id(asyncio.get_running_loop()))
        self.done.set()
        if self.error is not None:
            raise self.error
        return True


def _wait(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestDispatcher:
    def test_every_press_shares_one_event_loop(self) -> None:
        """A fresh loop per keypress was setup cost on the critical path."""

        propresenter = FakeProPresenter()
        logger = RecordingLogger()
        dispatcher = ClickerActionDispatcher(propresenter, logger)
        try:
            for _ in range(4):
                assert dispatcher.submit("next") is True
            assert _wait(lambda: len(propresenter.calls) == 4)
        finally:
            dispatcher.stop()

        assert len(set(propresenter.loops)) == 1

    def test_the_press_timestamp_reaches_the_recorder(self) -> None:
        propresenter = FakeProPresenter()
        recorder = PressToCueRecorder(min_cue_interval_seconds=0.0)
        dispatcher = ClickerActionDispatcher(propresenter, RecordingLogger(), recorder=recorder)
        try:
            pressed_at = time.monotonic() - 0.75
            dispatcher.submit("next", pressed_at)
            assert _wait(lambda: propresenter.calls == ["next"])
            sample = recorder.record_cue("slide:a:1")
        finally:
            dispatcher.stop()

        assert sample is not None
        # The clock starts at the press, not when the worker picked it up.
        assert sample.latency_ms >= 750.0

    def test_no_previous_cue_is_not_reported_as_a_failure(self) -> None:
        """ProPresenter refuses `previous` on the first dynamic Bible verse."""

        propresenter = FakeProPresenter(error=ProPresenterHttpError(400, "Bad Request"))
        logger = RecordingLogger()
        dispatcher = ClickerActionDispatcher(propresenter, logger, recorder=None)
        try:
            dispatcher.submit("previous")
            assert _wait(lambda: propresenter.done.is_set())
            assert _wait(lambda: "clicker_action_no_previous_cue" in logger.names())
        finally:
            dispatcher.stop()

        assert "clicker_action_failed" not in logger.names()

    def test_a_real_failure_is_still_reported(self) -> None:
        propresenter = FakeProPresenter(error=ProPresenterTransportError("connection refused"))
        logger = RecordingLogger()
        dispatcher = ClickerActionDispatcher(propresenter, logger)
        try:
            dispatcher.submit("previous")
            assert _wait(lambda: "clicker_action_failed" in logger.names())
        finally:
            dispatcher.stop()

    def test_the_bible_key_triggers_the_macro_and_is_not_timed_as_a_slide(self) -> None:
        """Bible mode is not a slide move, so it must not skew press-to-cue."""

        propresenter = FakeProPresenter()
        recorder = PressToCueRecorder(min_cue_interval_seconds=0.0)
        dispatcher = ClickerActionDispatcher(propresenter, RecordingLogger(), recorder=recorder)
        try:
            dispatcher.submit("bible")
            assert _wait(lambda: propresenter.calls == ["bible"])
        finally:
            dispatcher.stop()

        assert propresenter.macros == [propresenter.config.bible_macro_trigger_uuid]
        assert recorder.summary()["pending"] == 0

    def test_a_full_queue_refuses_input_rather_than_growing(self) -> None:
        release = threading.Event()

        class Blocking(FakeProPresenter):
            async def _record(self, direction: str) -> bool:
                await asyncio.to_thread(release.wait, 5)
                return await super()._record(direction)

        dispatcher = ClickerActionDispatcher(Blocking(), RecordingLogger(), queue_size=2)
        try:
            accepted = [dispatcher.submit("next") for _ in range(6)]
            assert accepted[:2] == [True, True]
            assert False in accepted
        finally:
            release.set()
            dispatcher.stop()


class TestPressCostsOneRoundTrip:
    def service(self) -> ProPresenterService:
        return ProPresenterService(ProPresenterConfig(host="127.0.0.1", port=1))

    @pytest.mark.asyncio
    async def test_a_warm_cache_means_one_request_per_press(self) -> None:
        service = self.service()
        # As the status stream would have left it.
        service._cache_monitor_value(
            "slide_index", {"presentation_index": {"presentation_id": {"uuid": "a"}, "index": 2}}
        )
        service.client.get_json = AsyncMock()
        service.client.trigger = AsyncMock(return_value=True)

        assert await service.next_slide() is True

        # The old path spent an uncached GET /presentation/slide_index first.
        service.client.get_json.assert_not_awaited()
        service.client.trigger.assert_awaited_once_with("/presentation/active/next/trigger")

    @pytest.mark.asyncio
    async def test_a_stale_cache_is_corrected_after_the_press_not_before(self) -> None:
        service = self.service()
        service._cache_monitor_value(
            "slide_index", {"presentation_index": {"presentation_id": {"uuid": "a"}, "index": 2}}
        )
        # ProPresenter has actually moved to a focused-only presentation.
        service.client.get_json = AsyncMock(return_value={"presentation_index": {}})
        service.client.trigger = AsyncMock(
            side_effect=[ProPresenterHttpError(404, "Not Found"), True]
        )

        assert await service.previous_slide() is True

        assert [call.args[0] for call in service.client.trigger.await_args_list] == [
            "/presentation/active/previous/trigger",
            "/presentation/focused/previous/trigger",
        ]

    @pytest.mark.asyncio
    async def test_an_unreachable_propresenter_is_not_retried(self) -> None:
        service = self.service()
        service._cache_monitor_value(
            "slide_index", {"presentation_index": {"presentation_id": {"uuid": "a"}, "index": 2}}
        )
        service.client.trigger = AsyncMock(side_effect=ProPresenterTransportError("refused"))

        with pytest.raises(ProPresenterTransportError):
            await service.next_slide()

        # A second request would fail identically; it only delays the failure.
        assert service.client.trigger.await_count == 1


class TestPooledConnections:
    @pytest.mark.asyncio
    async def test_repeated_calls_reuse_one_connection(self) -> None:
        """Every call used to open and tear down its own TCP connection."""

        with ScriptedHttpServer() as server:
            for _ in range(5):
                server.enqueue("/v1/presentation/slide_index", body={"ok": True})
            client = ProPresenterClient(ProPresenterConfig(host="127.0.0.1", port=server.port))
            try:
                for _ in range(5):
                    assert await client.get_json("/presentation/slide_index") == {"ok": True}
            finally:
                await client.aclose()

            ports = {request.client_port for request in server.requests}
            assert len(ports) == 1, f"expected one pooled connection, saw {len(ports)}"

    @pytest.mark.asyncio
    async def test_an_error_status_carries_its_code(self) -> None:
        with ScriptedHttpServer() as server:
            server.enqueue("/v1/presentation/active/previous/trigger", body={}, status=400)
            client = ProPresenterClient(ProPresenterConfig(host="127.0.0.1", port=server.port))
            try:
                with pytest.raises(ProPresenterHttpError) as raised:
                    await client.trigger("/presentation/active/previous/trigger")
            finally:
                await client.aclose()
            assert raised.value.code == 400


class TestClickerInterception:
    """The clicker only works because Production Hub translates its keys.

    The default key codes are keypad + (69) and keypad - (78). ProPresenter
    does not bind either, so nothing else on the machine acts on them: the
    interception is the mechanism, not an enhancement over ProPresenter's own
    handling. Turning it off disables the clicker rather than handing it back.
    """

    def test_the_listener_is_enabled_by_default(self) -> None:
        assert ProPresenterConfig().clicker_listener_enabled is True

    def test_it_can_be_switched_off_for_sites_that_do_not_use_a_clicker(self) -> None:
        config = ProPresenterConfig(clicker_listener_enabled=False)
        assert config.clicker_listener_enabled is False

    def test_listen_only_still_forwards_by_default(self) -> None:
        """Accessibility is optional for a clicker on keys ProPresenter ignores.

        Without it the tap can listen but not consume. With keypad +/- that is
        harmless -- ProPresenter ignores the keystroke -- and forwarding must
        continue or the clicker stops working entirely.
        """

        assert ProPresenterConfig().clicker_forward_when_listen_only is True
        assert ProPresenterConfig().next_slide_key_code == 69
        assert ProPresenterConfig().previous_slide_key_code == 78

    def test_forwarding_can_be_disabled_for_keys_propresenter_binds(self) -> None:
        """Remapped onto arrows or space, forwarding an unswallowed press
        would advance two slides."""

        config = ProPresenterConfig(clicker_forward_when_listen_only=False)
        assert config.clicker_forward_when_listen_only is False
