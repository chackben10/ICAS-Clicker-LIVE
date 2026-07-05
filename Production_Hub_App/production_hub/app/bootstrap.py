from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from production_hub.core.automation.engine import AutomationEngine
from production_hub.core.config.defaults import build_default_automations, build_default_endpoints
from production_hub.core.config.models import AppPaths
from production_hub.core.config.repository import ConfigRepository, default_app_root
from production_hub.core.endpoints.actions import ActionRouter
from production_hub.core.endpoints.executor import EndpointExecutor
from production_hub.core.endpoints.models import ActionDefinition, ActionResult, EndpointDefinition
from production_hub.core.endpoints.registry import EndpointRegistry
from production_hub.core.endpoints.variables import resolve_template
from production_hub.core.health.monitor import HealthMonitor
from production_hub.core.health.status_models import IntegrationHealth, STATUS_CONNECTED, STATUS_DISABLED, STATUS_OFFLINE
from production_hub.core.logging.log_repository import LogRepository
from production_hub.core.logging.logger import StructuredLogger, configure_logging
from production_hub.integrations.midi.placeholder import MidiPlaceholder
from production_hub.integrations.obs.service import ObsService
from production_hub.integrations.panasonic_awp.preset_service import PanasonicPresetService
from production_hub.integrations.panasonic_awp.service import PanasonicAwpService
from production_hub.integrations.propresenter.service import ProPresenterService
from production_hub.integrations.scoreboard.repository import ScoreboardRepository
from production_hub.integrations.scoreboard.service import ScoreboardService
from production_hub.state.state_repository import RuntimeStateRepository
from production_hub.state.undo_manager import UndoManager


@dataclass
class ApplicationContext:
    paths: AppPaths
    workspace_root: Path
    config_repository: ConfigRepository
    config: Any
    endpoint_registry: EndpointRegistry
    endpoint_executor: EndpointExecutor
    automation_engine: AutomationEngine
    runtime_state_repo: RuntimeStateRepository
    propresenter: ProPresenterService
    obs: ObsService
    panasonic: PanasonicAwpService
    panasonic_presets: PanasonicPresetService
    scoreboard: ScoreboardService
    midi: MidiPlaceholder
    health_monitor: HealthMonitor
    log_repository: LogRepository
    logger: StructuredLogger
    undo_manager: UndoManager


def _workspace_root() -> Path:
    configured = os.environ.get("PRODUCTION_HUB_WORKSPACE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        bundle_resources = Path(sys.executable).resolve().parents[1] / "Resources" / "remote_pages"
        if bundle_resources.exists():
            return bundle_resources
        pyinstaller_root = getattr(sys, "_MEIPASS", "")
        if pyinstaller_root:
            bundled_pages = Path(pyinstaller_root) / "remote_pages"
            if bundled_pages.exists():
                return bundled_pages
    return Path(__file__).resolve().parents[3]


async def _action_ok(action: ActionDefinition, message: str, data: dict[str, Any] | None = None) -> ActionResult:
    return ActionResult(action.action_type, True, message, data or {})


def build_context(data_dir: Path | None = None) -> ApplicationContext:
    paths = AppPaths(data_dir or default_app_root())
    config_repository = ConfigRepository(paths)
    config = config_repository.load_app_config()
    if ensure_api_cors_origins(config):
        config_repository.save_app_config(config)
    loaded_endpoints = config_repository.load_endpoints()
    endpoints = ensure_required_endpoints(loaded_endpoints)
    if not any(item.key == "audio_clear" for item in loaded_endpoints):
        config_repository.save_endpoints(endpoints)
    loaded_automations = config_repository.load_automations()
    loaded_automation_data = [item.to_dict() for item in loaded_automations]
    automations = ensure_builtin_automation_steps(loaded_automations)
    if [item.to_dict() for item in automations] != loaded_automation_data:
        config_repository.save_automations(automations)

    base_logger = configure_logging(paths.logs_dir)
    logger = StructuredLogger(base_logger, "bootstrap")

    runtime_state_repo = RuntimeStateRepository(paths.state_dir, paths.automatic_backups_dir)
    scoreboard_repo = ScoreboardRepository(paths.state_dir, paths.automatic_backups_dir)

    propresenter = ProPresenterService(config.integrations.propresenter)
    obs = ObsService(config.integrations.obs)
    panasonic = PanasonicAwpService(config.integrations.panasonic)
    panasonic_presets = PanasonicPresetService(config.integrations.panasonic)
    scoreboard = ScoreboardService(scoreboard_repo)
    midi = MidiPlaceholder()

    registry = EndpointRegistry(endpoints)
    router = ActionRouter()
    executor = EndpointExecutor(router)
    automation_engine = AutomationEngine(automations)
    health_monitor = HealthMonitor(config)
    log_repository = LogRepository(paths.logs_dir)
    undo_manager = UndoManager(max_items=100)

    context = ApplicationContext(
        paths=paths,
        workspace_root=_workspace_root(),
        config_repository=config_repository,
        config=config,
        endpoint_registry=registry,
        endpoint_executor=executor,
        automation_engine=automation_engine,
        runtime_state_repo=runtime_state_repo,
        propresenter=propresenter,
        obs=obs,
        panasonic=panasonic,
        panasonic_presets=panasonic_presets,
        scoreboard=scoreboard,
        midi=midi,
        health_monitor=health_monitor,
        log_repository=log_repository,
        logger=logger,
        undo_manager=undo_manager,
    )

    register_action_handlers(context, router)
    seed_initial_health(context)
    logger.info("context_ready", "Production Hub context initialized", data_dir=str(paths.root))
    return context


def ensure_required_endpoints(endpoints: list[EndpointDefinition]) -> list[EndpointDefinition]:
    if any(item.key == "audio_clear" for item in endpoints):
        return endpoints
    defaults = {item.key: item for item in build_default_endpoints()}
    audio_clear = defaults.get("audio_clear")
    return [*endpoints, audio_clear] if audio_clear else endpoints


def ensure_api_cors_origins(config: Any) -> bool:
    required = [
        "https://icas-clicker.work",
        "https://www.icas-clicker.work",
        "https://control.icas-clicker.work",
        "https://slides.icas-clicker.work",
    ]
    origins = list(config.api.cors_allow_origins or [])
    changed = False
    for origin in required:
        if origin not in origins:
            origins.append(origin)
            changed = True
    if changed:
        config.api.cors_allow_origins = origins
    return changed


def ensure_builtin_automation_steps(automations: list[Any]) -> list[Any]:
    defaults = {item.key: item for item in build_default_automations()}
    repaired = []
    for automation in automations:
        default = defaults.get(automation.key)
        if default is None:
            repaired.append(automation)
            continue
        if not automation.conditions and default.conditions:
            automation.conditions = [dict(condition) for condition in default.conditions]
        if not automation.actions and default.actions:
            automation.actions = [ActionDefinition.from_dict(action.to_dict()) for action in default.actions]
        repaired.append(automation)
    return repaired


def seed_initial_health(context: ApplicationContext) -> None:
    context.health_monitor.update(
        IntegrationHealth(
            "ProPresenter",
            STATUS_OFFLINE if context.config.integrations.propresenter.enabled else STATUS_DISABLED,
            f"{context.config.integrations.propresenter.host}:{context.config.integrations.propresenter.port}",
        )
    )
    context.health_monitor.update(
        IntegrationHealth(
            "OBS",
            STATUS_OFFLINE if context.config.integrations.obs.enabled else STATUS_DISABLED,
            f"{context.config.integrations.obs.host}:{context.config.integrations.obs.port}",
        )
    )
    context.health_monitor.update(
        IntegrationHealth(
            "Panasonic AWP",
            STATUS_OFFLINE if context.config.integrations.panasonic.enabled else STATUS_DISABLED,
            context.config.integrations.panasonic.camera_ip,
        )
    )
    context.health_monitor.update(
        IntegrationHealth(
            "VISCA Bridge",
            STATUS_OFFLINE if context.config.integrations.visca.enabled else STATUS_DISABLED,
            f"{context.config.integrations.visca.listen_ip}:{context.config.integrations.visca.udp_port}",
        )
    )
    context.health_monitor.update(context.scoreboard.health())
    context.health_monitor.update(IntegrationHealth("Remote API Server", STATUS_CONNECTED, context.config.api.base_url))
    context.health_monitor.update(context.midi.health())


def register_action_handlers(context: ApplicationContext, router: ActionRouter) -> None:
    def param(action: ActionDefinition, action_context: dict[str, Any], key: str, default: Any = "") -> Any:
        if key in action.params:
            return resolve_template(action.params[key], action_context)
        return resolve_template(action_context.get(key, default), action_context)

    async def propresenter_next(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        await context.propresenter.next_slide()
        return await _action_ok(action, "next slide triggered")

    async def propresenter_previous(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        await context.propresenter.previous_slide()
        return await _action_ok(action, "previous slide triggered")

    async def propresenter_focus(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        index = int(param(action, action_context, "index", 0))
        await context.propresenter.focus_slide(index)
        return await _action_ok(action, "slide focused", {"index": index})

    async def trigger_presentation(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        label = str(param(action, action_context, "label", ""))
        await context.propresenter.trigger_presentation_label(label)
        return await _action_ok(action, "presentation triggered", {"label": label})

    async def trigger_service_logo(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        logo = str(param(action, action_context, "service_logo_uuid", ""))
        await context.propresenter.trigger_service_logo(logo)
        return await _action_ok(action, "service logo triggered", {"service_logo_uuid": logo})

    async def clear_announcements(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        await context.propresenter.clear_announcements()
        return await _action_ok(action, "announcements layer cleared")

    async def clear_slide(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        delay = float(param(action, action_context, "delay_seconds", 0))
        await context.propresenter.clear_slide(delay)
        return await _action_ok(action, "slide layer cleared")

    async def trigger_macro(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        macro = str(param(action, action_context, "macro_name", action_context.get("name", "")))
        await context.propresenter.trigger_macro(macro)
        return await _action_ok(action, "macro triggered", {"name": macro})

    async def timer_start(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        await context.propresenter.timer_start()
        return await _action_ok(action, "timer started")

    async def timer_stop(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        await context.propresenter.timer_stop()
        return await _action_ok(action, "timer stopped")

    async def timer_reset(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        await context.propresenter.timer_reset()
        return await _action_ok(action, "timer reset")

    async def audio_trigger(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        playlist = str(param(action, action_context, "playlist", ""))
        track = str(param(action, action_context, "track", ""))
        await context.propresenter.audio.trigger(playlist, track)
        return await _action_ok(action, "audio triggered", {"playlist": playlist, "track": track})

    async def audio_clear(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        await context.propresenter.audio.clear()
        return await _action_ok(action, "audio cleared")

    async def obs_set_scene(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        scene = str(param(action, action_context, "scene", ""))
        use_policy = str(param(action, action_context, "transition_policy", True)).lower() not in {"0", "false", "no", "off"}
        await context.obs.set_scene(scene, use_policy=use_policy)
        return await _action_ok(action, "OBS scene set", {"scene": scene})

    async def obs_apply_look_rule(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        look_name = str(param(action, action_context, "look_name", ""))
        result = await context.obs.apply_look_rule(look_name, force=True)
        return await _action_ok(action, "OBS look rule applied", result or {"look_name": look_name, "skipped": True})

    async def obs_reconnect(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        connected = await context.obs.connect()
        context.health_monitor.update(context.obs.client.health())
        if connected:
            return await _action_ok(action, "OBS reconnected")
        return ActionResult(action.action_type, False, "OBS unavailable")

    async def runtime_auto_show(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        state = context.runtime_state_repo.load()
        if "enabled" in action.params or "enabled" in action_context:
            enabled = str(param(action, action_context, "enabled", state.auto_show_enabled)).lower() in {"1", "true", "yes", "on"}
            state.auto_show_enabled = enabled
            context.runtime_state_repo.save(state)
        return await _action_ok(action, "auto-show state read", {"enabled": state.auto_show_enabled})

    async def panasonic_recall_preset(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        preset = int(param(action, action_context, "preset", 0))
        await context.panasonic.recall_preset(preset)
        return await _action_ok(action, "camera preset recalled", {"preset": preset})

    async def panasonic_save_preset(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        preset = int(param(action, action_context, "preset", 0))
        await context.panasonic.save_preset(preset)
        return await _action_ok(action, "camera preset saved", {"preset": preset})

    async def panasonic_send_command(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        command = str(param(action, action_context, "command", ""))
        endpoint = str(param(action, action_context, "endpoint", "aw_ptz"))
        await context.panasonic.send_command(command, endpoint)
        return await _action_ok(action, "camera command sent", {"command": command, "endpoint": endpoint})

    handlers = {
        "propresenter.next_slide": propresenter_next,
        "propresenter.previous_slide": propresenter_previous,
        "propresenter.focus_slide": propresenter_focus,
        "propresenter.trigger_presentation": trigger_presentation,
        "propresenter.trigger_service_logo": trigger_service_logo,
        "propresenter.clear_announcements": clear_announcements,
        "propresenter.clear_slide": clear_slide,
        "propresenter.trigger_macro": trigger_macro,
        "propresenter.timer_start": timer_start,
        "propresenter.timer_stop": timer_stop,
        "propresenter.timer_reset": timer_reset,
        "propresenter.audio_trigger": audio_trigger,
        "propresenter.audio_clear": audio_clear,
        "obs.set_scene": obs_set_scene,
        "obs.apply_look_rule": obs_apply_look_rule,
        "obs.reconnect": obs_reconnect,
        "runtime.auto_show": runtime_auto_show,
        "panasonic.recall_preset": panasonic_recall_preset,
        "panasonic.save_preset": panasonic_save_preset,
        "panasonic.send_command": panasonic_send_command,
    }
    for action_type, handler in handlers.items():
        router.register(action_type, handler)
