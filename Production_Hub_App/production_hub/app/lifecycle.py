from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from production_hub.api.server import create_app
from production_hub.app.bible_look_guard import BibleLookGuard
from production_hub.app.bootstrap import ApplicationContext
from production_hub.app.clicker_dispatcher import ClickerActionDispatcher
from production_hub.core.automation.evaluator import evaluate_rule_tree
from production_hub.core.automation.triggers import AutomationTriggerMonitor
from production_hub.core.config.input_lists import poll_due_input_lists
from production_hub.core.automation.models import AutomationDefinition, AutomationRunState
from production_hub.core.endpoints.models import ActionDefinition, EndpointDefinition
from production_hub.core.health.status_models import IntegrationHealth, STATUS_CONNECTED, STATUS_OFFLINE, STATUS_RECONNECTING
from production_hub.integrations.panasonic_awp.models import PanasonicCommand
from production_hub.integrations.visca.udp_listener import ViscaUdpListener
from production_hub.integrations.visca.tracking_toggle import (
    ViscaAutofocusTrackingToggle,
)


@dataclass
class ApiServerHandle:
    threads: list[threading.Thread]
    servers: list[object]

    def stop(self) -> None:
        for server in self.servers:
            setattr(server, "should_exit", True)
        for thread in self.threads:
            thread.join(timeout=5)


LEGACY_API_PORTS = (17777, 5000)
AUTO_SHOW_AUTOMATION_KEYS = frozenset({"auto_show_slides", "auto_show_bible_slides"})
CLICKER_LISTENER_RETRY_SECONDS = 3.0
CLICKER_UNAVAILABLE_LOG_SECONDS = 30.0


@dataclass
class BackgroundServicesHandle:
    thread: threading.Thread
    stop_event: threading.Event

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)


@dataclass
class ClickerListenerHandle:
    thread: threading.Thread
    stop_event: threading.Event
    dispatcher: ClickerActionDispatcher

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=3)
        self.dispatcher.stop()


def _bind_probe(host: str, port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
    finally:
        probe.close()


def _start_uvicorn_thread(app, host: str, port: int) -> tuple[threading.Thread, object]:
    import uvicorn

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    def _run() -> None:
        asyncio.run(server.serve())

    thread = threading.Thread(target=_run, name=f"production-hub-api-{port}", daemon=True)
    thread.start()
    return thread, server


def start_api_server(context: ApplicationContext) -> ApiServerHandle:
    try:
        import uvicorn
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("Uvicorn is required to run the embedded API server. Install requirements.txt.") from exc

    try:
        _bind_probe(context.config.api.bind_host, context.config.api.port)
    except OSError as exc:
        raise RuntimeError(f"API server cannot bind {context.config.api.base_url}: {exc}") from exc

    app = create_app(context)
    threads: list[threading.Thread] = []
    servers: list[object] = []

    thread, server = _start_uvicorn_thread(app, context.config.api.bind_host, context.config.api.port)
    threads.append(thread)
    servers.append(server)

    for legacy_port in LEGACY_API_PORTS:
        if legacy_port == context.config.api.port:
            continue
        try:
            _bind_probe(context.config.api.bind_host, legacy_port)
        except OSError as exc:
            context.logger.warning(
                "legacy_api_port_unavailable",
                "Legacy compatibility API port is unavailable",
                port=legacy_port,
                error=str(exc),
            )
            continue
        legacy_thread, legacy_server = _start_uvicorn_thread(app, context.config.api.bind_host, legacy_port)
        threads.append(legacy_thread)
        servers.append(legacy_server)
        context.logger.info(
            "legacy_api_port_started",
            "Legacy compatibility API port started",
            url=f"http://{context.config.api.bind_host}:{legacy_port}",
        )

    context.health_monitor.update(IntegrationHealth("Remote API Server", STATUS_CONNECTED, context.config.api.base_url))
    return ApiServerHandle(threads=threads, servers=servers)


async def startup_checks(context: ApplicationContext) -> None:
    if context.config.integrations.obs.enabled:
        context.health_monitor.update(IntegrationHealth("OBS", STATUS_RECONNECTING, context.obs.client.target))
        connected = await context.obs.connect()
        context.health_monitor.update(context.obs.client.health())
        if not connected:
            context.logger.warning("obs_unavailable", "OBS is not currently available", error=context.obs.client.last_error)

    if context.config.integrations.propresenter.enabled:
        try:
            await context.propresenter.health_check()
        except Exception as exc:
            context.propresenter.client.mark_error(str(exc))
            context.logger.warning("propresenter_unavailable", "ProPresenter is not currently available", error=str(exc))
        context.health_monitor.update(context.propresenter.client.health())

    if context.config.integrations.panasonic.enabled:
        try:
            await context.panasonic.test_connection()
        except Exception as exc:
            context.panasonic.client.mark_error(str(exc))
            context.logger.warning("panasonic_unavailable", "Panasonic AWP is not currently available", error=str(exc))
        context.health_monitor.update(context.panasonic.client.health())


async def start_visca_listener(context: ApplicationContext) -> ViscaUdpListener | None:
    if not context.config.integrations.visca.enabled:
        return None

    tracking_toggle = ViscaAutofocusTrackingToggle(context)

    async def handle(command: PanasonicCommand) -> None:
        if await tracking_toggle.consume(command):
            return
        await tracking_toggle.cancel_pending("Manual VISCA camera control")
        await context.ptz_automation.manual_override_async("VISCA bridge command")
        await context.panasonic.send_command(command.command, command.endpoint)

    listener = ViscaUdpListener(context.config.integrations.visca, handle)
    listener.add_shutdown_callback(tracking_toggle.shutdown)
    try:
        await listener.start()
        target = f"{context.config.integrations.visca.listen_ip}:{context.config.integrations.visca.udp_port}"
        context.health_monitor.update(IntegrationHealth("VISCA Bridge", STATUS_CONNECTED, target))
        return listener
    except Exception as exc:
        target = f"{context.config.integrations.visca.listen_ip}:{context.config.integrations.visca.udp_port}"
        context.health_monitor.update(IntegrationHealth("VISCA Bridge", STATUS_OFFLINE, target, last_error=str(exc)))
        context.logger.warning("visca_unavailable", "VISCA bridge did not start", error=str(exc))
        return None


def start_midi_receiver(context: ApplicationContext, loop: asyncio.AbstractEventLoop):
    if not context.config.integrations.midi.enabled:
        context.health_monitor.update(context.midi.health())
        return None

    def schedule_midi_action(actions: list[ActionDefinition], action_context: dict[str, Any]) -> None:
        async def _run() -> None:
            context.logger.info(
                "midi_action_received",
                (
                    f"MIDI note {action_context.get('midi_note')} velocity {action_context.get('midi_velocity')} "
                    f"mapped to {len(actions)} action(s)"
                ),
                actions=[action.to_dict() for action in actions],
                midi=action_context,
            )
            endpoint = EndpointDefinition(
                key="midi:pad_trigger",
                name="MIDI Pad Trigger",
                route="/__midi/pad-trigger",
                actions=actions,
                enabled=True,
            )
            result = await context.endpoint_executor.execute(endpoint, action_context)
            if not result.ok:
                raise RuntimeError(result.error or "MIDI action failed")
            context.logger.info(
                "midi_action_complete",
                f"MIDI note {action_context.get('midi_note')} action complete",
                action_results=[item.to_dict() for item in result.action_results],
                midi=action_context,
            )

        future = asyncio.run_coroutine_threadsafe(_run(), loop)

        def _done(done_future) -> None:
            try:
                done_future.result()
                context.health_monitor.update(context.midi.health())
            except Exception as exc:
                context.logger.warning(
                    "midi_action_failed",
                    (
                        f"MIDI note {action_context.get('midi_note')} failed for "
                        f"{len(actions)} action(s): {exc}"
                    ),
                    actions=[action.to_dict() for action in actions],
                    midi=action_context,
                    error=str(exc),
                )

        future.add_done_callback(_done)

    context.midi.set_handler(schedule_midi_action)
    started = context.midi.start()
    context.health_monitor.update(context.midi.health())
    if not started:
        context.logger.warning("midi_unavailable", "MIDI receiver did not start", error=context.midi.health().last_error)
        return None
    context.logger.info("midi_receiver_started", "MIDI receiver started", input=context.midi.input_name)
    return context.midi


def start_clicker_listener(context: ApplicationContext, loop: asyncio.AbstractEventLoop) -> ClickerListenerHandle | None:
    if sys.platform != "darwin" or not context.config.integrations.propresenter.enabled:
        return None
    try:
        import Quartz
    except Exception as exc:
        context.logger.warning("clicker_listener_unavailable", "Quartz is unavailable; global clicker keys are disabled", error=str(exc))
        return None

    if not context.config.integrations.propresenter.clicker_listener_enabled:
        # Nothing is intercepted, so macOS is never asked for Input Monitoring
        # or Accessibility. The clicker's keypresses reach ProPresenter directly.
        context.logger.info(
            "clicker_listener_disabled",
            "Global clicker listener is disabled; clicker keys go straight to ProPresenter",
        )
        return None

    del loop  # Clicker actions intentionally use a dedicated worker/async loop.
    stop_event = threading.Event()
    dispatcher = ClickerActionDispatcher(
        context.propresenter,
        context.logger,
        recorder=getattr(context, "press_to_cue", None),
    )

    def _run() -> None:
        next_key = int(context.config.integrations.propresenter.next_slide_key_code)
        previous_key = int(context.config.integrations.propresenter.previous_slide_key_code)
        bible_key = int(context.config.integrations.propresenter.bible_mode_key_code or 0)
        tap_ref: dict[str, Any] = {}

        def callback(_proxy, event_type, event, _refcon):
            disabled_types = {
                value
                for value in (
                    getattr(Quartz, "kCGEventTapDisabledByTimeout", None),
                    getattr(Quartz, "kCGEventTapDisabledByUserInput", None),
                )
                if value is not None
            }
            if event_type in disabled_types:
                tap = tap_ref.get("tap")
                if tap is not None:
                    Quartz.CGEventTapEnable(tap, True)
                    context.logger.warning(
                        "clicker_listener_reenabled",
                        "macOS disabled the input listener; Production Hub re-enabled it",
                    )
                return event
            if event_type != Quartz.kCGEventKeyDown:
                return event
            key_code = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))
            if not tap_ref.get("forward", True):
                # Listening only: ProPresenter already has this keystroke.
                return event
            if bible_key and key_code == bible_key:
                # Bible mode is not a slide move, so it is not timed as one.
                dispatcher.submit("bible")
                return None if tap_ref.get("consume", True) else event
            if key_code not in (next_key, previous_key):
                return event
            # Taken here, in the tap callback, so press-to-cue starts at the
            # press itself rather than after the queue and the HTTP request.
            pressed_at = time.monotonic()
            dispatcher.submit("next" if key_code == next_key else "previous", pressed_at)
            return None if tap_ref.get("consume", True) else event

        event_mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
        last_unavailable_log = 0.0
        listen_request_attempted = False
        while not stop_event.is_set():
            tap_ref["consume"] = True
            tap_ref["forward"] = True
            tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionDefault,
                event_mask,
                callback,
                None,
            )
            listen_only_option = getattr(Quartz, "kCGEventTapOptionListenOnly", None)
            preflight_listen = getattr(Quartz, "CGPreflightListenEventAccess", None)
            listen_access = bool(preflight_listen()) if callable(preflight_listen) else False
            if not listen_access and not listen_request_attempted:
                listen_request_attempted = True
                request_listen = getattr(Quartz, "CGRequestListenEventAccess", None)
                if callable(request_listen):
                    listen_access = bool(request_listen())
            if tap is None and listen_only_option is not None and listen_access:
                tap = Quartz.CGEventTapCreate(
                    Quartz.kCGSessionEventTap,
                    Quartz.kCGHeadInsertEventTap,
                    listen_only_option,
                    event_mask,
                    callback,
                    None,
                )
                if tap is not None:
                    # A listen-only tap cannot swallow the keypress, so
                    # ProPresenter receives it too. Forwarding as well would
                    # advance twice, which is why forwarding is off unless the
                    # operator has explicitly accepted that risk.
                    tap_ref["consume"] = False
                    tap_ref["forward"] = bool(
                        context.config.integrations.propresenter.clicker_forward_when_listen_only
                    )
                    context.logger.warning(
                        "clicker_listener_listen_only",
                        "Clicker listener started in keyboard listen-only mode",
                    )
            if tap is None:
                now = time.monotonic()
                if now - last_unavailable_log >= CLICKER_UNAVAILABLE_LOG_SECONDS:
                    last_unavailable_log = now
                    context.logger.warning(
                        "clicker_listener_unavailable",
                        "Global input listener is unavailable; Production Hub will retry. Grant Input Monitoring and Accessibility permission if needed.",
                    )
                stop_event.wait(CLICKER_LISTENER_RETRY_SECONDS)
                continue

            tap_ref["tap"] = tap
            source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(tap, True)
            context.logger.info(
                "clicker_listener_started",
                "Global clicker key listener started",
                next_key=next_key,
                previous_key=previous_key,
            )
            while not stop_event.is_set():
                Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 0.2, False)
            Quartz.CGEventTapEnable(tap, False)
            tap_ref.clear()

    thread = threading.Thread(target=_run, name="production-hub-clicker-listener", daemon=True)
    thread.start()
    return ClickerListenerHandle(thread=thread, stop_event=stop_event, dispatcher=dispatcher)


def register_automation_handlers(context: ApplicationContext) -> None:
    for definition in context.automation_engine.definitions.values():
        register_generic_automation_handler(context, definition)


def register_generic_automation_handler(context: ApplicationContext, definition: AutomationDefinition) -> None:
    if context.automation_engine.has_handler(definition.key) or not definition.actions:
        return

    last_success_at = 0.0

    async def generic_handler(
        current_definition: AutomationDefinition,
        state: AutomationRunState,
        action_context: dict[str, Any],
    ) -> str:
        nonlocal last_success_at
        import time

        now = time.monotonic()
        if current_definition.cooldown_seconds and (now - last_success_at) < current_definition.cooldown_seconds:
            return "cooldown"

        conditions_ok, condition_message = await evaluate_rule_tree(context, current_definition.rules, action_context)
        state.last_condition_result = condition_message
        if not conditions_ok:
            return f"conditions_not_met:{condition_message}"

        endpoint = EndpointDefinition(
            key=f"automation:{current_definition.key}",
            name=current_definition.name,
            route=f"/__automation/{current_definition.key}",
            actions=current_definition.actions,
            enabled=True,
        )
        execution_context = {
            **action_context,
            "automation_id": current_definition.key,
            "automation_name": current_definition.name,
        }
        result = await context.endpoint_executor.execute(endpoint, execution_context)
        if result.ok:
            last_success_at = now
            return "actions_complete"
        raise RuntimeError(result.error or "automation actions failed")

    context.automation_engine.register_handler(definition.key, generic_handler)


async def _run_due_automations(
    context: ApplicationContext,
    trigger_monitor: AutomationTriggerMonitor,
) -> None:
    import time

    now = time.monotonic()
    if context.automation_engine.paused:
        trigger_monitor.reset()
        return

    trigger_monitor.forget_missing(set(context.automation_engine.definitions))
    auto_show_enabled = context.runtime_state_repo.auto_show_enabled()
    pending_runs: list[tuple[str, dict[str, Any]]] = []
    for definition in list(context.automation_engine.definitions.values()):
        if not definition.enabled:
            trigger_monitor.forget(definition.key)
            continue
        if definition.key in AUTO_SHOW_AUTOMATION_KEYS and not auto_show_enabled:
            trigger_monitor.forget(definition.key)
            continue
        register_generic_automation_handler(context, definition)
        try:
            due, action_context = await trigger_monitor.due(definition, now)
        except Exception as exc:
            state = context.automation_engine.states.get(definition.key)
            if state:
                state.last_condition_result = f"trigger_unavailable:{exc}"
            continue
        if due:
            pending_runs.append((definition.key, action_context))

    for key, action_context in pending_runs:
        enriched_context = trigger_monitor.enrich_from_snapshot_cycle(action_context, now)
        await context.automation_engine.run_once(key, enriched_context)


async def _background_services_main(context: ApplicationContext, stop_event: threading.Event) -> None:
    register_automation_handlers(context)
    loop = asyncio.get_running_loop()
    status_stream = None
    if context.config.integrations.propresenter.enabled:
        propresenter_config = context.config.integrations.propresenter

        def _fire_bible_macro() -> None:
            # The guard runs on the stream's reader thread; the trigger is async
            # and belongs on this loop.
            asyncio.run_coroutine_threadsafe(
                context.propresenter.trigger_macro(propresenter_config.bible_macro_trigger_uuid),
                loop,
            ).add_done_callback(_log_bible_macro_result)

        def _log_bible_macro_result(completed) -> None:
            try:
                completed.result()
            except Exception as exc:
                context.logger.warning(
                    "bible_look_correction_failed",
                    "Bible macro could not be triggered",
                    error=str(exc),
                )

        def _schedule_bible_verify(delay: float, callback) -> None:
            # The guard runs on the stream's reader thread; confirmation is a
            # timer and belongs on this loop.
            loop.call_soon_threadsafe(loop.call_later, delay, callback)

        def _rerender_bible_slide() -> None:
            # Runs on the stream's reader thread; the trigger is async.
            asyncio.run_coroutine_threadsafe(
                _rerender_and_log(),
                loop,
            )

        async def _rerender_and_log() -> None:
            try:
                reason = await context.propresenter.rerender_active_slide()
            except Exception as exc:
                context.logger.warning(
                    "bible_slide_rerender_failed",
                    "Could not re-trigger the verse",
                    error=str(exc),
                )
                return
            if reason:
                context.logger.warning(
                    "bible_slide_rerender_skipped",
                    "Did not re-trigger the verse",
                    reason=reason,
                )

        bible_guard = BibleLookGuard(
            propresenter_config.bible_look_name,
            _fire_bible_macro,
            logger=context.logger,
            schedule=_schedule_bible_verify,
            rerender=_rerender_bible_slide,
            settle_seconds=propresenter_config.bible_look_settle_seconds,
            retry_delay_seconds=propresenter_config.bible_look_retry_delay_seconds,
            max_attempts=propresenter_config.bible_look_max_attempts,
            reassert_count=propresenter_config.bible_look_reassert_count,
        )
        def _prime_status_state() -> None:
            # Runs on the stream's reader thread whenever it (re)connects.
            # /status/updates never pushes state that was already true, so the
            # current look and presentation are read once here; without it the
            # guard cannot see a reading that was already on screen.
            asyncio.run_coroutine_threadsafe(
                context.propresenter.prime_status_state(),
                loop,
            )

        # One aggregated ProPresenter subscription feeds the monitored_* caches
        # so trigger evaluation stops opening an API request per rule per tick.
        status_stream = context.propresenter.start_status_stream(
            bible_guard.observe,
            on_connect=_prime_status_state,
        )
    resource_sampler = getattr(context, "propresenter_resources", None)
    if resource_sampler is not None:
        # The lag tracks ProPresenter's resident size, so the curve has to be
        # recorded across real services, not reconstructed after one.
        resource_sampler.start()
    listener = await start_visca_listener(context)
    clicker_listener = start_clicker_listener(context, loop)
    midi_receiver = start_midi_receiver(context, loop)
    trigger_monitor = AutomationTriggerMonitor(context)
    next_list_due: dict[str, float] = {}
    input_list_poll_tasks: dict[str, asyncio.Task[None]] = {}
    input_list_poll_failures: dict[str, str] = {}
    next_reconnect_at = 0.0

    try:
        while not stop_event.is_set():
            await asyncio.sleep(0.25)
            await _run_due_automations(context, trigger_monitor)
            try:
                context.runtime_state_repo.flush_if_due()
            except Exception as exc:
                context.logger.warning(
                    "runtime_state_flush_failed",
                    "Runtime telemetry could not be persisted",
                    error=str(exc),
                )
            await poll_due_input_lists(
                context,
                next_list_due,
                input_list_poll_tasks,
                input_list_poll_failures,
            )
            import time

            now = time.monotonic()
            if now >= next_reconnect_at:
                next_reconnect_at = now + 4.0
                obs_config = context.config.integrations.obs
                if obs_config.enabled and obs_config.automatic_reconnect:
                    if context.obs.client.connected:
                        try:
                            await context.obs.get_current_scene()
                        except Exception:
                            await context.obs.connect()
                    else:
                        await context.obs.connect()
                    context.health_monitor.update(context.obs.client.health())
                propresenter_config = context.config.integrations.propresenter
                if propresenter_config.enabled and propresenter_config.automatic_reconnect:
                    try:
                        await context.propresenter.health_check()
                    except Exception as exc:
                        context.propresenter.client.mark_error(str(exc))
                    context.health_monitor.update(context.propresenter.client.health())
                if context.config.integrations.panasonic.enabled:
                    try:
                        await context.panasonic.test_connection()
                    except Exception as exc:
                        context.panasonic.client.mark_error(str(exc))
                    context.health_monitor.update(context.panasonic.client.health())
                if midi_receiver is not None and not midi_receiver.is_active:
                    midi_receiver.stop()
                    midi_receiver = None
                if context.config.integrations.midi.enabled and midi_receiver is None:
                    midi_receiver = start_midi_receiver(context, loop)
                if context.config.integrations.visca.enabled and listener is None:
                    listener = await start_visca_listener(context)
    finally:
        if resource_sampler is not None:
            resource_sampler.stop()
        if status_stream is not None:
            context.propresenter.stop_status_stream()
        try:
            await context.propresenter.client.aclose()
        except Exception:
            pass
        for task in input_list_poll_tasks.values():
            task.cancel()
        if input_list_poll_tasks:
            await asyncio.gather(*input_list_poll_tasks.values(), return_exceptions=True)
        if clicker_listener:
            clicker_listener.stop()
        if midi_receiver:
            midi_receiver.stop()
        if listener:
            await listener.stop()
        try:
            context.runtime_state_repo.flush()
        except Exception as exc:
            context.logger.warning(
                "runtime_state_flush_failed",
                "Runtime telemetry could not be persisted during shutdown",
                error=str(exc),
            )


def start_background_services(context: ApplicationContext) -> BackgroundServicesHandle:
    stop_event = threading.Event()

    def _run() -> None:
        try:
            asyncio.run(_background_services_main(context, stop_event))
        except Exception as exc:
            context.logger.error("background_services_failed", "Background services stopped unexpectedly", error=str(exc))

    thread = threading.Thread(target=_run, name="production-hub-background", daemon=True)
    thread.start()
    return BackgroundServicesHandle(thread=thread, stop_event=stop_event)
