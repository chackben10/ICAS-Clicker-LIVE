from __future__ import annotations

import asyncio
import socket
import sys
import threading
from dataclasses import dataclass
from typing import Any

from production_hub.api.server import create_app
from production_hub.app.bootstrap import ApplicationContext
from production_hub.core.automation.evaluator import evaluate_rule_tree
from production_hub.core.automation.triggers import AutomationTriggerMonitor
from production_hub.core.config.input_lists import poll_due_input_lists
from production_hub.core.automation.models import AutomationDefinition, AutomationRunState
from production_hub.core.endpoints.models import ActionDefinition, EndpointDefinition
from production_hub.core.health.status_models import IntegrationHealth, STATUS_CONNECTED, STATUS_OFFLINE, STATUS_RECONNECTING
from production_hub.integrations.panasonic_awp.models import PanasonicCommand
from production_hub.integrations.visca.udp_listener import ViscaUdpListener


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

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=3)


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

    async def handle(command: PanasonicCommand) -> None:
        await context.panasonic.send_command(command.command, command.endpoint)

    listener = ViscaUdpListener(context.config.integrations.visca, handle)
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

    stop_event = threading.Event()

    async def trigger_next() -> None:
        await context.propresenter.next_slide()

    async def trigger_previous() -> None:
        await context.propresenter.previous_slide()

    def schedule_clicker_action(coro) -> None:
        future = asyncio.run_coroutine_threadsafe(coro, loop)

        def _done(done_future) -> None:
            try:
                done_future.result()
            except Exception as exc:
                context.logger.warning("clicker_action_failed", "Clicker slide action failed", error=str(exc))

        future.add_done_callback(_done)

    def _run() -> None:
        next_key = int(context.config.integrations.propresenter.next_slide_key_code)
        previous_key = int(context.config.integrations.propresenter.previous_slide_key_code)

        def callback(_proxy, event_type, event, _refcon):
            if event_type != Quartz.kCGEventKeyDown:
                return event
            key_code = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))
            if key_code == next_key:
                schedule_clicker_action(trigger_next())
                return None
            if key_code == previous_key:
                schedule_clicker_action(trigger_previous())
                return None
            return event

        event_mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,
            event_mask,
            callback,
            None,
        )
        if tap is None:
            context.logger.warning(
                "clicker_listener_unavailable",
                "Global clicker keys are disabled; grant Accessibility permission to Production Hub if needed.",
            )
            return

        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        context.logger.info("clicker_listener_started", "Global clicker key listener started", next_key=next_key, previous_key=previous_key)
        while not stop_event.is_set():
            Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 0.2, False)
        Quartz.CGEventTapEnable(tap, False)

    thread = threading.Thread(target=_run, name="production-hub-clicker-listener", daemon=True)
    thread.start()
    return ClickerListenerHandle(thread=thread, stop_event=stop_event)


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
    trigger_monitor.forget_missing(set(context.automation_engine.definitions))
    for definition in list(context.automation_engine.definitions.values()):
        register_generic_automation_handler(context, definition)
        try:
            due, action_context = await trigger_monitor.due(definition, now)
        except Exception as exc:
            state = context.automation_engine.states.get(definition.key)
            if state:
                state.last_condition_result = f"trigger_unavailable:{exc}"
            continue
        if due:
            await context.automation_engine.run_once(definition.key, action_context)


async def _background_services_main(context: ApplicationContext, stop_event: threading.Event) -> None:
    register_automation_handlers(context)
    loop = asyncio.get_running_loop()
    listener = await start_visca_listener(context)
    clicker_listener = start_clicker_listener(context, loop)
    midi_receiver = start_midi_receiver(context, loop)
    trigger_monitor = AutomationTriggerMonitor(context)
    next_list_due: dict[str, float] = {}
    input_list_poll_tasks: dict[str, asyncio.Task[None]] = {}
    next_reconnect_at = 0.0

    try:
        while not stop_event.is_set():
            await asyncio.sleep(0.25)
            await _run_due_automations(context, trigger_monitor)
            await poll_due_input_lists(context, next_list_due, input_list_poll_tasks)
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
