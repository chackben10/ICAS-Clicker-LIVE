from __future__ import annotations

import os
import sys
from dataclasses import replace
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from production_hub.core.automation.engine import AutomationEngine
from production_hub.core.automation.catalog import normalize_trigger
from production_hub.core.config.defaults import build_default_automations, build_default_endpoints, build_default_midi_mappings
from production_hub.core.config.input_lists import ensure_default_input_lists
from production_hub.core.config.models import AppPaths
from production_hub.core.config.repository import ConfigRepository, default_app_root
from production_hub.core.security.sanitize import redact_secrets
from production_hub.core.endpoints.actions import ActionRouter
from production_hub.core.endpoints.executor import EndpointExecutor
from production_hub.core.endpoints.models import ActionDefinition, ActionResult, EndpointDefinition, EndpointResponseDefinition
from production_hub.core.endpoints.search import search_song_library
from production_hub.core.endpoints.registry import EndpointRegistry
from production_hub.core.endpoints.variables import resolve_template
from production_hub.core.health.monitor import HealthMonitor
from production_hub.core.health.status_models import IntegrationHealth, STATUS_CONNECTED, STATUS_DISABLED, STATUS_OFFLINE
from production_hub.core.logging.log_repository import LogRepository
from production_hub.core.logging.logger import StructuredLogger, configure_logging
from production_hub.integrations.midi.models import MidiMapping
from production_hub.integrations.midi.receiver import MidiReceiver
from production_hub.integrations.obs.service import ObsService
from production_hub.integrations.panasonic_awp.preset_service import PanasonicPresetService
from production_hub.integrations.panasonic_awp.service import PanasonicAwpService
from production_hub.integrations.propresenter.service import ProPresenterService
from production_hub.integrations.propresenter.audio_service import strip_audio_extension
from production_hub.integrations.scoreboard.repository import ScoreboardRepository
from production_hub.integrations.scoreboard.models import ScoreRow
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
    midi: MidiReceiver
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
    if ensure_midi_defaults(config):
        config_repository.save_app_config(config)
    if ensure_api_cors_origins(config):
        config_repository.save_app_config(config)
    if ensure_default_input_lists(config):
        config_repository.save_app_config(config)
    loaded_endpoints = config_repository.load_endpoints()
    loaded_endpoint_data = [item.to_dict() for item in loaded_endpoints]
    endpoints = ensure_builtin_endpoint_defaults(ensure_endpoint_input_defaults(ensure_required_endpoints(loaded_endpoints)))
    if [item.to_dict() for item in endpoints] != loaded_endpoint_data:
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
    scoreboard = ScoreboardService(scoreboard_repo, config.integrations.scoreboard)
    midi_mappings = [MidiMapping.from_dict(item) for item in config.integrations.midi.mappings]
    midi = MidiReceiver(config.integrations.midi, midi_mappings, lambda _action, _context: None)

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
    existing = {item.key for item in endpoints}
    additions = [endpoint for endpoint in build_default_endpoints() if endpoint.key not in existing]
    return [*endpoints, *additions]


def ensure_endpoint_input_defaults(endpoints: list[EndpointDefinition]) -> list[EndpointDefinition]:
    defaults = {item.key: item for item in build_default_endpoints()}
    repaired: list[EndpointDefinition] = []
    for endpoint in endpoints:
        default = defaults.get(endpoint.key)
        if default and default.inputs and not endpoint.inputs:
            endpoint.inputs = [type(input_def).from_dict(input_def.to_dict()) for input_def in default.inputs]
        repaired.append(endpoint)
    return repaired


def ensure_builtin_endpoint_defaults(endpoints: list[EndpointDefinition]) -> list[EndpointDefinition]:
    defaults = {item.key: item for item in build_default_endpoints()}
    last_action_data_routes = {
        "/active-presentation",
        "/slide-index",
        "/current-base",
        "/service_logos",
        "/macros",
        "/audio/playlists",
        "/audio/tracks",
        "/auto-show",
    }
    repaired: list[EndpointDefinition] = []
    for endpoint in endpoints:
        default = defaults.get(endpoint.key)
        if default and endpoint.key == "debug" and endpoint.route == "/debug":
            endpoint.route = default.route
            endpoint.aliases = list(default.aliases)
            endpoint.response = type(default.response).from_dict(default.response.to_dict())
        if endpoint.route in last_action_data_routes:
            endpoint.response = EndpointResponseDefinition("last_action_data")
        if endpoint.route == "/audio/active":
            endpoint.response = EndpointResponseDefinition(
                "plain_text",
                "{{text}}",
                "",
                "text/plain; charset=utf-8",
            )
        if default and endpoint.key == "audio_trigger":
            endpoint.allowed_methods = list(default.allowed_methods)
        repaired.append(endpoint)
    return repaired


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


def ensure_midi_defaults(config: Any) -> bool:
    midi = config.integrations.midi
    changed = False
    if not midi.mappings:
        midi.mappings = [item.to_dict() for item in build_default_midi_mappings()]
        midi.enabled = True
        changed = True
    return changed


def ensure_builtin_automation_steps(automations: list[Any]) -> list[Any]:
    defaults = {item.key: item for item in build_default_automations()}
    repaired = []
    for automation in automations:
        if automation.key == "obs_connection_watchdog":
            continue
        default = defaults.get(automation.key)
        if default is None:
            repaired.append(automation)
            continue
        automation.trigger = normalize_trigger(automation.trigger)
        if automation.key == "auto_show_slides" and automation.interval_seconds <= 0:
            automation.interval_seconds = default.interval_seconds
        if automation.key == "bible_look_enforcement" and automation.trigger == "interval":
            automation.trigger = default.trigger
            automation.conditions = [dict(condition) for condition in default.conditions]
            automation.rules = dict(default.rules)
        if not automation.conditions and default.conditions:
            automation.conditions = [dict(condition) for condition in default.conditions]
            automation.rules = dict(default.rules)
        if not automation.actions and default.actions:
            automation.actions = [ActionDefinition.from_dict(action.to_dict()) for action in default.actions]
        if automation.key == "slide_label_audio_sync" and len(automation.actions) == 1:
            action = automation.actions[0]
            if action.action_type == "propresenter.audio_trigger" and "slide_label" in str(action.params):
                automation.actions = [ActionDefinition("propresenter.audio_from_slide_label")]
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

    async def propresenter_get_active_presentation(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        data = await context.propresenter.full_presentation()
        return await _action_ok(action, "active presentation read", data)

    async def propresenter_get_slide_index(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        data = await context.propresenter.slide_index()
        return await _action_ok(action, "slide index read", data)

    async def propresenter_get_current_base(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        base = await context.propresenter.refresh_presentation_base()
        mode = "active" if base.endswith("/active") else "focused"
        return await _action_ok(
            action,
            "current base read",
            {"mode": mode, "base_url": f"{context.config.integrations.propresenter.base_url}{base}"},
        )

    async def propresenter_get_service_logos(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        return await _action_ok(
            action,
            "service logos read",
            {"items": [item.to_dict() for item in context.config.integrations.propresenter.service_logos]},
        )

    async def propresenter_get_macros(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        return await _action_ok(
            action,
            "macros read",
            {"items": [{"name": item.macro_name} for item in context.config.integrations.propresenter.macros]},
        )

    async def propresenter_get_thumbnail(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        uuid = str(param(action, action_context, "uuid", ""))
        index = int(param(action, action_context, "index", 0))
        tier = str(param(action, action_context, "tier", "low"))
        entry = await context.propresenter.thumbnails.fetch(uuid, index, tier)
        return await _action_ok(action, "thumbnail read", {"body": entry.body, "media_type": entry.content_type})

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
        if macro.startswith("{{"):
            macro = str(action_context.get("name", ""))
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
        resolved = await context.propresenter.audio.find_track_in_playlist(playlist, track)
        if resolved:
            playlist = resolved.playlist
            track = resolved.name
        await context.propresenter.audio.trigger(playlist, track)
        return await _action_ok(action, "audio triggered", {"playlist": playlist, "track": track})

    async def audio_from_slide_label(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        audio_config = context.config.integrations.propresenter.audio
        if not audio_config.slide_label_sync_enabled:
            return await _action_ok(action, "slide-label audio sync disabled")
        index = action_context.get("slide_index")
        if index is None:
            return await _action_ok(action, "no active slide")
        active = await context.propresenter.active_presentation()
        presentation = active.get("presentation") if isinstance(active, dict) else {}
        presentation = presentation if isinstance(presentation, dict) else {}
        identifier = presentation.get("id")
        identifier = identifier if isinstance(identifier, dict) else {}
        uuid = str(identifier.get("uuid") or "")
        wanted_uuid = str(action_context.get("presentation_uuid") or "")
        if wanted_uuid and uuid != wanted_uuid:
            return await _action_ok(action, "presentation changed before audio lookup")
        offset = 0
        slide = None
        for group in presentation.get("groups") or []:
            if not isinstance(group, dict):
                continue
            for candidate in group.get("slides") or []:
                if offset == int(index):
                    slide = candidate if isinstance(candidate, dict) else None
                    break
                offset += 1
            if slide is not None:
                break
        label = strip_audio_extension(str((slide or {}).get("label") or ""))
        if not label:
            return await _action_ok(action, "active slide has no audio label")
        history_key = f"{uuid}:{index}:{label}"
        if not context.propresenter.audio.remember_triggered(history_key):
            return await _action_ok(action, "slide-label audio already triggered")
        track = await context.propresenter.audio.find_track(label)
        if not track:
            return await _action_ok(action, "no matching slide-label audio track", {"label": label})
        if audio_config.trigger_delay_seconds:
            import asyncio

            await asyncio.sleep(audio_config.trigger_delay_seconds)
        await context.propresenter.audio.trigger(track.playlist, track.name)
        return await _action_ok(action, "slide-label audio triggered", {"playlist": track.playlist, "track": track.name})

    async def audio_clear(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        await context.propresenter.audio.clear()
        return await _action_ok(action, "audio cleared")

    async def audio_playlists(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        return await _action_ok(action, "audio playlists read", {"items": await context.propresenter.audio.playlists()})

    async def audio_tracks(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        playlist = str(param(action, action_context, "playlist", ""))
        return await _action_ok(action, "audio tracks read", {"items": await context.propresenter.audio.playlist_tracks(playlist)})

    async def audio_active(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        title = await context.propresenter.audio.active_text()
        text = f"item: {title}" if title else ""
        return await _action_ok(action, "active audio read", {"text": text, "title": title})

    async def health_get_status(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        snapshot = context.health_monitor.snapshot(
            context.endpoint_registry.all(),
            context.automation_engine.definitions.values(),
        )
        return await _action_ok(action, "health read", snapshot.to_dict())

    async def system_get_debug(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        return await _action_ok(
            action,
            "debug read",
            {
                "config": redact_secrets(context.config.to_dict()),
                "obs": {
                    "connected": context.obs.client.connected,
                    "lastError": context.obs.client.last_error,
                    "currentScene": context.obs.current_scene,
                    "sceneItems": {
                        scene: [item.to_dict() for item in items] for scene, items in context.obs.last_scene_items.items()
                    },
                },
                "runtime": context.runtime_state_repo.load().to_dict(),
            },
        )

    async def input_list_search_songs(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        query = str(param(action, action_context, "query", action_context.get("q", "")))
        if query.startswith("{{"):
            query = str(action_context.get("q", ""))
        list_key = str(param(action, action_context, "list_key", "song_library")) or "song_library"
        limit = int(param(action, action_context, "limit", 25))
        results = search_song_library(context, query, list_key=list_key, limit=limit)
        return await _action_ok(action, "song library searched", {"_return": results, "items": results, "query": query})

    async def obs_set_scene(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        scene = str(param(action, action_context, "scene", ""))
        transition = str(param(action, action_context, "transition", ""))
        raw_duration = param(action, action_context, "duration", "")
        if transition.startswith("{{"):
            transition = ""
        duration_text = str(raw_duration).strip()
        duration = int(duration_text) if duration_text and not duration_text.startswith("{{") else None
        if transition:
            await context.obs.set_transition(transition, duration)
            await context.obs.client.call("set_current_program_scene", scene)
            context.obs.current_scene = scene
            return await _action_ok(action, "OBS scene set", {"scene": scene, "transition": transition, "durationMs": duration})
        use_policy = str(param(action, action_context, "transition_policy", True)).lower() not in {"0", "false", "no", "off"}
        await context.obs.set_scene(scene, use_policy=use_policy)
        return await _action_ok(action, "OBS scene set", {"scene": scene})

    async def obs_get_current_scene(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        scene = await context.obs.get_current_scene()
        return await _action_ok(action, "current OBS scene read", {"ok": True, "currentProgramSceneName": scene})

    async def obs_get_scene_items(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        scene = str(param(action, action_context, "scene", context.config.integrations.obs.main_layout_scene)) or context.config.integrations.obs.main_layout_scene
        items = await context.obs.get_scene_items(scene)
        return await _action_ok(action, "OBS scene items read", {"ok": True, "sceneName": scene, "items": [item.to_dict() for item in items]})

    async def obs_apply_scene_items(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        scene = str(param(action, action_context, "scene", action_context.get("sceneName", context.config.integrations.obs.main_layout_scene)))
        payload = dict(action_context.get("body") if isinstance(action_context.get("body"), dict) else action_context)
        applied = await context.obs.apply_scene_item_visibility(scene, payload)
        return await _action_ok(action, "OBS scene items applied", {"ok": True, "sceneName": scene, "applied": applied})

    async def obs_legacy_set_sources(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        mode = str(param(action, action_context, "mode", "none")).lower()
        scene = str(param(action, action_context, "scene", "ProPresenter Slides"))
        src_ann = str(param(action, action_context, "srcAnn", "Audience Camera"))
        src_cam = str(param(action, action_context, "srcCam", "PTZ Camera"))
        if mode not in {"none", "ann", "cam"}:
            return ActionResult(action.action_type, False, "mode must be none|ann|cam")
        items = await context.obs.get_scene_items(scene)
        by_name = {item.source_name: item.scene_item_id for item in items}
        payload: dict[str, list[int]] = {"show": [], "hide": []}
        if mode == "none":
            payload["hide"] = [by_name[name] for name in (src_ann, src_cam) if name in by_name]
        elif mode == "ann":
            payload["show"] = [by_name[src_ann]] if src_ann in by_name else []
            payload["hide"] = [by_name[src_cam]] if src_cam in by_name else []
        elif mode == "cam":
            payload["show"] = [by_name[src_cam]] if src_cam in by_name else []
            payload["hide"] = [by_name[src_ann]] if src_ann in by_name else []
        await context.obs.apply_scene_item_visibility(scene, payload)
        return await _action_ok(action, "legacy OBS source mode applied", {"mode": mode, "scene": scene, **payload})

    async def obs_apply_look_rule(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        look_name = str(param(action, action_context, "look_name", ""))
        if not look_name or look_name.startswith("{{"):
            look_name = await context.propresenter.current_look_name()
        result = await context.obs.apply_look_rule(look_name, force=True)
        return await _action_ok(action, "OBS look rule applied", result or {"look_name": look_name, "skipped": True})

    async def obs_set_scene_item_enabled(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        scene = str(param(action, action_context, "scene", context.config.integrations.obs.main_layout_scene))
        enabled = str(param(action, action_context, "enabled", True)).lower() in {"1", "true", "yes", "on"}
        raw_id = str(param(action, action_context, "scene_item_id", "")).strip()
        source_name = str(param(action, action_context, "source_name", "")).strip()
        scene_item_id = int(raw_id) if raw_id else 0
        if not scene_item_id and source_name:
            items = await context.obs.get_scene_items(scene)
            match = next((item for item in items if item.source_name == source_name), None)
            if match:
                scene_item_id = match.scene_item_id
        if not scene_item_id:
            raise ValueError("scene_item_id or matching source_name is required")
        await context.obs.set_scene_item_enabled(scene, scene_item_id, enabled)
        return await _action_ok(
            action,
            "OBS source visibility set",
            {"scene": scene, "scene_item_id": scene_item_id, "source_name": source_name, "enabled": enabled},
        )

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
        ok = await context.panasonic.recall_preset(preset)
        if not ok:
            return ActionResult(action.action_type, False, "camera preset recall failed", {"preset": preset})
        return await _action_ok(action, "camera preset recalled", {"preset": preset})

    async def panasonic_save_preset(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        preset = int(param(action, action_context, "preset", 0))
        ok = await context.panasonic.save_preset(preset)
        if not ok:
            return ActionResult(action.action_type, False, "camera preset save failed", {"preset": preset})
        return await _action_ok(action, "camera preset saved", {"preset": preset})

    async def panasonic_send_command(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        command = str(param(action, action_context, "command", ""))
        endpoint = str(param(action, action_context, "endpoint", "aw_ptz"))
        ok = await context.panasonic.send_command(command, endpoint)
        if not ok:
            return ActionResult(action.action_type, False, "camera command failed", {"command": command, "endpoint": endpoint})
        return await _action_ok(action, "camera command sent", {"command": command, "endpoint": endpoint})

    def scoreboard_row(row_id: str = "", name: str = "") -> ScoreRow | None:
        state = context.scoreboard.get_state()
        if row_id:
            match = next((row for row in state.rows if row.id == row_id), None)
            if match:
                return match
        if name:
            return next((row for row in state.rows if row.name == name), None)
        return None

    def scoreboard_save(rows: list[ScoreRow], writer_action: str) -> dict[str, Any]:
        state = context.scoreboard.get_state()
        history = state.legacy_payload().get("history", [])
        history.append([row.to_dict() for row in state.rows])
        updated = context.scoreboard.update_state(
            {"rows": [row.to_dict() for row in rows], "history": history[-100:]},
            writer={"source": "Production Hub action", "action": writer_action},
            expected_revision=state.revision,
        )
        return {"revision": updated.revision, "rows": len(updated.rows)}

    async def scoreboard_add_row(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        name = str(param(action, action_context, "name", ""))
        score = int(param(action, action_context, "score", 0))
        state = context.scoreboard.add_row(name, score)
        return await _action_ok(action, "scoreboard row added", {"revision": state.revision, "name": name, "score": score})

    async def scoreboard_get_state(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        return await _action_ok(action, "scoreboard state read", context.scoreboard.get_state().legacy_payload())

    async def scoreboard_replace_state(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        payload = action_context.get("body") if isinstance(action_context.get("body"), dict) else action_context
        expected = payload.get("expected_revision") if isinstance(payload, dict) else None
        if expected is not None:
            expected = int(expected)
        state = context.scoreboard.update_state(
            dict(payload) if isinstance(payload, dict) else {},
            writer={"source": "Production Hub endpoint", "action": action.action_type},
            expected_revision=expected,
        )
        return await _action_ok(action, "scoreboard state replaced", state.legacy_payload())

    async def scoreboard_update_score(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        row_id = str(param(action, action_context, "row_id", ""))
        name = str(param(action, action_context, "name", ""))
        delta = int(param(action, action_context, "delta", 0))
        row = scoreboard_row(row_id, name)
        if row is None:
            raise ValueError("scoreboard row not found")
        state = context.scoreboard.get_state()
        rows = [replace(item, score=int(item.score) + delta) if item.id == row.id else item for item in state.rows]
        data = scoreboard_save(rows, action.action_type)
        return await _action_ok(action, "score updated", {**data, "row_id": row.id, "delta": delta})

    async def scoreboard_set_score(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        row_id = str(param(action, action_context, "row_id", ""))
        name = str(param(action, action_context, "name", ""))
        score = int(param(action, action_context, "score", 0))
        row = scoreboard_row(row_id, name)
        if row is None:
            raise ValueError("scoreboard row not found")
        state = context.scoreboard.get_state()
        rows = [replace(item, score=score) if item.id == row.id else item for item in state.rows]
        data = scoreboard_save(rows, action.action_type)
        return await _action_ok(action, "score set", {**data, "row_id": row.id, "score": score})

    async def scoreboard_clear_row(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        row_id = str(param(action, action_context, "row_id", ""))
        name = str(param(action, action_context, "name", ""))
        row = scoreboard_row(row_id, name)
        if row is None:
            raise ValueError("scoreboard row not found")
        state = context.scoreboard.get_state()
        rows = [replace(item, score=0) if item.id == row.id else item for item in state.rows]
        data = scoreboard_save(rows, action.action_type)
        return await _action_ok(action, "score row cleared", {**data, "row_id": row.id})

    async def scoreboard_clear_all(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        data = scoreboard_save([], action.action_type)
        return await _action_ok(action, "scoreboard cleared", data)

    async def scoreboard_undo(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        state = context.scoreboard.undo()
        return await _action_ok(action, "scoreboard undo complete", {"revision": state.revision, "rows": len(state.rows)})

    async def scoreboard_rename_row(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        row_id = str(param(action, action_context, "row_id", ""))
        name = str(param(action, action_context, "name", ""))
        new_name = str(param(action, action_context, "new_name", ""))
        row = scoreboard_row(row_id, name)
        if row is None:
            raise ValueError("scoreboard row not found")
        state = context.scoreboard.get_state()
        rows = [replace(item, name=new_name) if item.id == row.id else item for item in state.rows]
        data = scoreboard_save(rows, action.action_type)
        return await _action_ok(action, "scoreboard row renamed", {**data, "row_id": row.id, "name": new_name})

    async def runtime_get_auto_show(action: ActionDefinition, action_context: dict[str, Any]) -> ActionResult:
        state = context.runtime_state_repo.load()
        return await _action_ok(action, "auto-show state read", {"enabled": state.auto_show_enabled})

    handlers = {
        "propresenter.next_slide": propresenter_next,
        "propresenter.previous_slide": propresenter_previous,
        "propresenter.get_active_presentation": propresenter_get_active_presentation,
        "propresenter.get_slide_index": propresenter_get_slide_index,
        "propresenter.get_current_base": propresenter_get_current_base,
        "propresenter.get_service_logos": propresenter_get_service_logos,
        "propresenter.get_macros": propresenter_get_macros,
        "propresenter.get_thumbnail": propresenter_get_thumbnail,
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
        "propresenter.audio_from_slide_label": audio_from_slide_label,
        "propresenter.audio_clear": audio_clear,
        "propresenter.audio_playlists": audio_playlists,
        "propresenter.audio_tracks": audio_tracks,
        "propresenter.audio_active": audio_active,
        "health.get_status": health_get_status,
        "system.get_debug": system_get_debug,
        "input_list.search_songs": input_list_search_songs,
        "obs.set_scene": obs_set_scene,
        "obs.get_current_scene": obs_get_current_scene,
        "obs.get_scene_items": obs_get_scene_items,
        "obs.apply_scene_items": obs_apply_scene_items,
        "obs.legacy_set_sources": obs_legacy_set_sources,
        "obs.apply_look_rule": obs_apply_look_rule,
        "obs.set_scene_item_enabled": obs_set_scene_item_enabled,
        "obs.reconnect": obs_reconnect,
        "runtime.auto_show": runtime_auto_show,
        "panasonic.recall_preset": panasonic_recall_preset,
        "panasonic.save_preset": panasonic_save_preset,
        "panasonic.send_command": panasonic_send_command,
        "scoreboard.get_state": scoreboard_get_state,
        "scoreboard.replace_state": scoreboard_replace_state,
        "scoreboard.add_row": scoreboard_add_row,
        "scoreboard.update_score": scoreboard_update_score,
        "scoreboard.set_score": scoreboard_set_score,
        "scoreboard.clear_row": scoreboard_clear_row,
        "scoreboard.clear_all": scoreboard_clear_all,
        "scoreboard.undo": scoreboard_undo,
        "scoreboard.rename_row": scoreboard_rename_row,
        "runtime.get_auto_show": runtime_get_auto_show,
    }
    for action_type, handler in handlers.items():
        router.register(action_type, handler)
