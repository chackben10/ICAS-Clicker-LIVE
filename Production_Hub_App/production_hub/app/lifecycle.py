from __future__ import annotations

import asyncio
import socket
import sys
import threading
from dataclasses import dataclass
from typing import Any

from production_hub.api.server import create_app
from production_hub.app.bootstrap import ApplicationContext
from production_hub.core.automation.evaluator import evaluate_conditions
from production_hub.core.automation.models import AutomationDefinition, AutomationRunState
from production_hub.core.endpoints.models import EndpointDefinition
from production_hub.core.health.status_models import IntegrationHealth, STATUS_CONNECTED, STATUS_OFFLINE, STATUS_RECONNECTING
from production_hub.integrations.panasonic_awp.models import PanasonicCommand
from production_hub.integrations.propresenter.audio_service import strip_audio_extension
from production_hub.integrations.obs.service import ObsTemporarilyNotReady
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


def _get_presentation(active_obj: dict[str, Any]) -> dict[str, Any]:
    presentation = active_obj.get("presentation")
    return presentation if isinstance(presentation, dict) else {}


def _active_uuid(active_obj: dict[str, Any]) -> str:
    presentation_id = _get_presentation(active_obj).get("id")
    return str(presentation_id.get("uuid") or "") if isinstance(presentation_id, dict) else ""


def _first_group_name(active_obj: dict[str, Any]) -> str:
    groups = _get_presentation(active_obj).get("groups")
    if isinstance(groups, list) and groups and isinstance(groups[0], dict):
        return str(groups[0].get("name") or "")
    return ""


def _has_single_colon_group(active_obj: dict[str, Any]) -> bool:
    groups = _get_presentation(active_obj).get("groups")
    if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict):
        return False
    return ":" in str(groups[0].get("name") or "")


def _find_slide(active_obj: dict[str, Any], current_index: int) -> dict[str, Any] | None:
    groups = _get_presentation(active_obj).get("groups")
    if not isinstance(groups, list):
        return None
    offset = 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        slides = group.get("slides")
        if not isinstance(slides, list):
            continue
        for slide in slides:
            if offset == current_index:
                return slide if isinstance(slide, dict) else None
            offset += 1
    return None


def _slide_index(data: dict[str, Any]) -> int | None:
    presentation_index = data.get("presentation_index")
    if not isinstance(presentation_index, dict):
        return None
    index = presentation_index.get("index")
    return int(index) if index is not None else None


def register_automation_handlers(context: ApplicationContext) -> None:
    memory: dict[str, Any] = {
        "bible_condition": False,
        "bible_last_macro_at": 0.0,
        "last_auto_show_state": None,
    }

    async def bible_look_enforcement(definition: AutomationDefinition, state: AutomationRunState) -> str:
        if not context.config.integrations.propresenter.enabled:
            return "propresenter_disabled"
        active = await context.propresenter.active_presentation()
        condition = _has_single_colon_group(active)
        rising = condition and not bool(memory["bible_condition"])
        memory["bible_condition"] = condition
        if not rising:
            return "condition_not_rising"

        import time

        now = time.time()
        if (now - float(memory["bible_last_macro_at"])) < definition.cooldown_seconds:
            return "cooldown"

        current_look = await context.propresenter.current_look_name()
        if current_look == context.config.integrations.propresenter.bible_look_name:
            return "already_bible_look"

        macro_uuid = context.config.integrations.propresenter.bible_macro_trigger_uuid
        macro_q = context.propresenter.client.quote_segment(macro_uuid)
        await context.propresenter.client.trigger(f"/macro/{macro_q}/trigger")
        memory["bible_last_macro_at"] = now
        return "bible_macro_triggered"

    async def obs_look_sync(definition: AutomationDefinition, state: AutomationRunState) -> str:
        if not context.config.integrations.propresenter.enabled or not context.config.integrations.obs.enabled:
            return "integration_disabled"
        look_name = await context.propresenter.current_look_name()
        if not look_name:
            return "no_current_look"
        runtime = context.runtime_state_repo.load()
        if runtime.current_propresenter_look != look_name:
            runtime.current_propresenter_look = look_name
            context.runtime_state_repo.save(runtime)
        try:
            result = await context.obs.apply_look_rule(look_name)
        except ObsTemporarilyNotReady:
            return "obs_not_ready"
        if not result:
            return f"no_rule:{look_name}"
        return "visibility_skipped" if result.get("skipped") else "visibility_applied"

    async def slide_label_audio_sync(definition: AutomationDefinition, state: AutomationRunState) -> str:
        config = context.config.integrations.propresenter.audio
        if not context.config.integrations.propresenter.enabled or not config.slide_label_sync_enabled:
            return "audio_sync_disabled"
        index = _slide_index(await context.propresenter.slide_index())
        if index is None or index < 0:
            return "no_active_slide"
        active = await context.propresenter.active_presentation()
        uuid = _active_uuid(active)
        if not uuid:
            return "no_active_presentation"
        slide = _find_slide(active, index)
        if not slide:
            return "slide_not_found"
        label = strip_audio_extension(str(slide.get("label") or ""))
        if not label:
            return "slide_has_no_audio_label"
        key = f"{uuid}:{index}:{label}"
        if not context.propresenter.audio.remember_triggered(key):
            return "duplicate_slide_label"
        track = await context.propresenter.audio.find_track(label)
        if not track:
            return "no_matching_audio_track"
        await asyncio.sleep(config.trigger_delay_seconds)
        await context.propresenter.audio.trigger(track.playlist, track.track)
        return f"audio_triggered:{track.playlist}/{track.track}"

    async def auto_show_slides(definition: AutomationDefinition, state: AutomationRunState) -> str:
        runtime = context.runtime_state_repo.load()
        if not runtime.auto_show_enabled:
            memory["last_auto_show_state"] = None
            return "auto_show_disabled"
        index_data = await context.propresenter.slide_index()
        index = _slide_index(index_data)
        presentation_index = index_data.get("presentation_index") if isinstance(index_data.get("presentation_index"), dict) else {}
        presentation_id = presentation_index.get("presentation_id") if isinstance(presentation_index, dict) else {}
        uuid_from_index = str(presentation_id.get("uuid") or "") if isinstance(presentation_id, dict) else ""
        active = await context.propresenter.active_presentation()
        group_name = _first_group_name(active)
        if index is None and not group_name:
            memory["last_auto_show_state"] = None
            return "no_presentation_state"
        current_state = f"{uuid_from_index}:{index}|{group_name}"
        previous_state = memory.get("last_auto_show_state")
        memory["last_auto_show_state"] = current_state
        if previous_state and previous_state != current_state:
            await context.propresenter.clear_announcements()
            await context.obs.set_scene("ProPresenter Input", use_policy=True)
            return "show_slides_applied"
        return "state_primed" if previous_state is None else "state_unchanged"

    async def obs_connection_watchdog(definition: AutomationDefinition, state: AutomationRunState) -> str:
        if not context.config.integrations.obs.enabled:
            return "obs_disabled"
        if not context.obs.client.connected:
            connected = await context.obs.connect()
            context.health_monitor.update(context.obs.client.health())
            return "obs_reconnected" if connected else "obs_unavailable"
        try:
            await context.obs.get_current_scene()
            context.health_monitor.update(context.obs.client.health())
            return "obs_connected"
        except Exception:
            connected = await context.obs.connect()
            context.health_monitor.update(context.obs.client.health())
            return "obs_reconnected" if connected else "obs_unavailable"

    def soft_fail(handler):
        async def _wrapped(definition: AutomationDefinition, state: AutomationRunState) -> str:
            try:
                conditions_ok, condition_message = await evaluate_conditions(context, definition.conditions)
                state.last_condition_result = condition_message
                if not conditions_ok:
                    return f"conditions_not_met:{condition_message}"
                return await handler(definition, state)
            except Exception as exc:
                return f"unavailable:{exc}"

        return _wrapped

    context.automation_engine.register_handler("bible_look_enforcement", soft_fail(bible_look_enforcement))
    context.automation_engine.register_handler("obs_look_sync", soft_fail(obs_look_sync))
    context.automation_engine.register_handler("slide_label_audio_sync", soft_fail(slide_label_audio_sync))
    context.automation_engine.register_handler("auto_show_slides", soft_fail(auto_show_slides))
    context.automation_engine.register_handler("obs_connection_watchdog", soft_fail(obs_connection_watchdog))


def _automation_interval(context: ApplicationContext, definition: AutomationDefinition) -> float:
    if definition.interval_seconds > 0:
        return definition.interval_seconds
    if definition.key in {"obs_look_sync", "slide_label_audio_sync", "auto_show_slides"}:
        return max(0.25, float(context.config.integrations.propresenter.polling_interval_seconds))
    return 0


def register_generic_automation_handler(context: ApplicationContext, definition: AutomationDefinition) -> None:
    if context.automation_engine.has_handler(definition.key) or not definition.actions:
        return

    last_success_at = 0.0

    async def generic_handler(current_definition: AutomationDefinition, state: AutomationRunState) -> str:
        nonlocal last_success_at
        import time

        now = time.monotonic()
        if current_definition.cooldown_seconds and (now - last_success_at) < current_definition.cooldown_seconds:
            return "cooldown"

        conditions_ok, condition_message = await evaluate_conditions(context, current_definition.conditions)
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
        result = await context.endpoint_executor.execute(endpoint, {"automation_key": current_definition.key})
        if result.ok:
            last_success_at = now
            return "actions_complete"
        raise RuntimeError(result.error or "automation actions failed")

    context.automation_engine.register_handler(definition.key, generic_handler)


async def _run_due_automations(
    context: ApplicationContext,
    next_due: dict[str, float],
) -> None:
    import time

    now = time.monotonic()
    for definition in list(context.automation_engine.definitions.values()):
        register_generic_automation_handler(context, definition)
        interval = _automation_interval(context, definition)
        if interval <= 0:
            continue
        if definition.key not in next_due:
            next_due[definition.key] = now + interval
            continue
        if now < next_due[definition.key]:
            continue
        next_due[definition.key] = now + interval
        await context.automation_engine.run_once(definition.key)


async def _background_services_main(context: ApplicationContext, stop_event: threading.Event) -> None:
    register_automation_handlers(context)
    loop = asyncio.get_running_loop()
    listener = await start_visca_listener(context)
    clicker_listener = start_clicker_listener(context, loop)
    next_due: dict[str, float] = {}

    try:
        while not stop_event.is_set():
            await asyncio.sleep(0.25)
            await _run_due_automations(context, next_due)
    finally:
        if clicker_listener:
            clicker_listener.stop()
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
