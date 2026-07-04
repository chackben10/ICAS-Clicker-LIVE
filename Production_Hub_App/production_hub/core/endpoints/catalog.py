from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    kind: str = "text"
    default: Any = ""
    help_text: str = ""
    options: tuple[str, ...] = ()
    context_options: str = ""


@dataclass(frozen=True)
class ActionSpec:
    action_type: str
    label: str
    category: str
    description: str
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)


ACTION_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec("propresenter.next_slide", "Next Slide", "ProPresenter", "Advance the active or focused presentation."),
    ActionSpec("propresenter.previous_slide", "Previous Slide", "ProPresenter", "Go backward in the active or focused presentation."),
    ActionSpec(
        "propresenter.focus_slide",
        "Trigger Slide Index",
        "ProPresenter",
        "Trigger a specific slide index.",
        (FieldSpec("index", "Slide index", "text", "0", "Use a number, or a variable such as {{index}}."),),
    ),
    ActionSpec(
        "propresenter.trigger_presentation",
        "Trigger Presentation",
        "ProPresenter",
        "Trigger one configured ProPresenter presentation.",
        (FieldSpec("label", "Presentation", "select", "", context_options="presentations"),),
    ),
    ActionSpec(
        "propresenter.trigger_service_logo",
        "Trigger Service Logo",
        "ProPresenter",
        "Trigger one configured service-logo presentation.",
        (FieldSpec("service_logo_uuid", "Service logo", "select", "", context_options="service_logos"),),
    ),
    ActionSpec("propresenter.clear_announcements", "Clear Announcements", "ProPresenter", "Clear the announcements layer."),
    ActionSpec(
        "propresenter.clear_slide",
        "Clear Slide",
        "ProPresenter",
        "Clear the slide layer, optionally after a delay.",
        (FieldSpec("delay_seconds", "Delay seconds", "text", "0"),),
    ),
    ActionSpec(
        "propresenter.trigger_macro",
        "Trigger Macro",
        "ProPresenter",
        "Trigger an allow-listed macro by name.",
        (FieldSpec("macro_name", "Macro", "select", "", context_options="macros"),),
    ),
    ActionSpec("propresenter.timer_start", "Start Timer", "ProPresenter", "Start the configured timer."),
    ActionSpec("propresenter.timer_stop", "Stop Timer", "ProPresenter", "Stop the configured timer."),
    ActionSpec("propresenter.timer_reset", "Reset Timer", "ProPresenter", "Reset the configured timer."),
    ActionSpec(
        "propresenter.audio_trigger",
        "Trigger Audio",
        "ProPresenter",
        "Trigger an audio track from a playlist.",
        (
            FieldSpec("playlist", "Playlist", "select", "", context_options="audio_playlists"),
            FieldSpec("track", "Track", "text", "", "Exact track name, or a variable such as {{track}}."),
        ),
    ),
    ActionSpec("propresenter.audio_clear", "Clear Audio", "ProPresenter", "Clear the audio layer."),
    ActionSpec(
        "obs.set_scene",
        "Set OBS Scene",
        "OBS",
        "Switch OBS to a scene using the transition policy.",
        (
            FieldSpec("scene", "Scene", "select", "", context_options="obs_scenes"),
            FieldSpec("transition_policy", "Use transition policy", "bool", True),
        ),
    ),
    ActionSpec(
        "obs.apply_look_rule",
        "Apply OBS Look Rule",
        "OBS",
        "Apply a configured OBS source visibility rule by look name.",
        (FieldSpec("look_name", "Look name", "select", "", context_options="obs_looks"),),
    ),
    ActionSpec("obs.reconnect", "Reconnect OBS", "OBS", "Reconnect OBS and refresh its health status."),
    ActionSpec(
        "panasonic.recall_preset",
        "Recall Camera Preset",
        "Panasonic AWP",
        "Recall a Panasonic camera position preset.",
        (FieldSpec("preset", "Preset number", "text", "1", "Use a number, or a variable such as {{preset}}."),),
    ),
    ActionSpec(
        "panasonic.save_preset",
        "Save Camera Preset",
        "Panasonic AWP",
        "Save the current camera position to a preset.",
        (FieldSpec("preset", "Preset number", "text", "1"),),
    ),
    ActionSpec(
        "panasonic.send_command",
        "Send Camera Command",
        "Panasonic AWP",
        "Send a Panasonic CGI command to aw_ptz or aw_cam.",
        (
            FieldSpec("command", "Command", "text", "#R01"),
            FieldSpec("endpoint", "Camera endpoint", "select", "aw_ptz", options=("aw_ptz", "aw_cam")),
        ),
    ),
    ActionSpec(
        "runtime.auto_show",
        "Set Auto Show",
        "Runtime",
        "Enable or disable Auto Show Slides.",
        (FieldSpec("enabled", "Enabled", "bool", True),),
    ),
    ActionSpec(
        "delay",
        "Wait",
        "Utility",
        "Pause execution before continuing to the next step.",
        (FieldSpec("seconds", "Seconds", "text", "1"),),
    ),
)


def action_spec(action_type: str) -> ActionSpec:
    for spec in ACTION_SPECS:
        if spec.action_type == action_type:
            return spec
    return ActionSpec(action_type, action_type, "Custom", "Custom action type.", ())


def action_options(context: Any, field: FieldSpec) -> list[str]:
    ref = field.context_options
    if ref == "presentations":
        return [item.label for item in context.config.integrations.propresenter.presentations]
    if ref == "service_logos":
        return [f"{item.name} | {item.uuid}" for item in context.config.integrations.propresenter.service_logos]
    if ref == "macros":
        return [item.macro_name for item in context.config.integrations.propresenter.macros]
    if ref == "audio_playlists":
        return list(context.config.integrations.propresenter.audio.playlists)
    if ref == "obs_scenes":
        return list(context.config.integrations.obs.known_scenes)
    if ref == "obs_looks":
        return [item.look_name for item in context.config.integrations.obs.look_rules]
    return list(field.options)


def normalize_select_value(field: FieldSpec, value: str) -> str:
    if field.context_options == "service_logos" and "|" in value:
        return value.rsplit("|", 1)[-1].strip()
    return value.strip()
