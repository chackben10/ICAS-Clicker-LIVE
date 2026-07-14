from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from production_hub.core.config.input_lists import input_list_choices, normalize_list_choice


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
    path: tuple[str, ...] = ()


# AI/agent maintenance note:
# Every executable Production Hub capability that should be available from MIDI,
# endpoints, automations, or other action builders belongs in ACTION_SPECS.
# Pair each new ActionSpec with a handler in app.bootstrap.register_action_handlers.
# production_hub/tests/unit/test_action_palette_catalog.py enforces both sides.
ACTION_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec("propresenter.next_slide", "Next Slide", "ProPresenter", "Advance the active or focused presentation.", path=("ProPresenter Actions", "Slide Control Actions")),
    ActionSpec("propresenter.previous_slide", "Previous Slide", "ProPresenter", "Go backward in the active or focused presentation.", path=("ProPresenter Actions", "Slide Control Actions")),
    ActionSpec("propresenter.get_active_presentation", "Get Active Presentation", "ProPresenter", "Return the active ProPresenter presentation.", path=("Read / Data Modules", "ProPresenter")),
    ActionSpec("propresenter.get_slide_index", "Get Slide Index", "ProPresenter", "Return the current ProPresenter slide index.", path=("Read / Data Modules", "ProPresenter")),
    ActionSpec("propresenter.get_current_base", "Get Current Base", "ProPresenter", "Return whether the app is using the active or focused presentation base.", path=("Read / Data Modules", "ProPresenter")),
    ActionSpec("propresenter.get_service_logos", "Get Service Logos", "ProPresenter", "Return configured service-logo mappings.", path=("Read / Data Modules", "ProPresenter")),
    ActionSpec("propresenter.get_macros", "Get Macros", "ProPresenter", "Return configured macro mappings.", path=("Read / Data Modules", "ProPresenter")),
    ActionSpec(
        "propresenter.get_thumbnail",
        "Get Thumbnail",
        "ProPresenter",
        "Return a ProPresenter slide thumbnail image.",
        (
            FieldSpec("uuid", "Presentation UUID", "text", ""),
            FieldSpec("index", "Slide index", "text", "0"),
            FieldSpec("tier", "Quality tier", "select", "low", options=("low", "high")),
        ),
        ("Read / Data Modules", "ProPresenter"),
    ),
    ActionSpec(
        "propresenter.focus_slide",
        "Trigger Slide Index",
        "ProPresenter",
        "Trigger a specific slide index.",
        (FieldSpec("index", "Slide index", "text", "0", "Use a number, or a variable such as {{index}}."),),
        ("ProPresenter Actions", "Slide Control Actions"),
    ),
    ActionSpec(
        "propresenter.trigger_presentation",
        "Trigger Presentation",
        "ProPresenter",
        "Trigger one configured ProPresenter presentation.",
        (FieldSpec("label", "Presentation", "select", "", context_options="presentations"),),
        ("ProPresenter Actions", "Presentation Actions"),
    ),
    ActionSpec(
        "propresenter.trigger_service_logo",
        "Trigger Service Logo",
        "ProPresenter",
        "Trigger one configured service-logo presentation.",
        (FieldSpec("service_logo_uuid", "Service logo", "select", "", context_options="service_logos"),),
        ("ProPresenter Actions", "Presentation Actions"),
    ),
    ActionSpec("propresenter.clear_announcements", "Clear Announcements", "ProPresenter", "Clear the announcements layer.", path=("ProPresenter Actions", "Clear Actions")),
    ActionSpec(
        "propresenter.clear_slide",
        "Clear Slide",
        "ProPresenter",
        "Clear the slide layer, optionally after a delay.",
        (FieldSpec("delay_seconds", "Delay seconds", "text", "0"),),
        ("ProPresenter Actions", "Clear Actions"),
    ),
    ActionSpec(
        "propresenter.trigger_macro",
        "Trigger Macro",
        "ProPresenter",
        "Trigger an allow-listed macro by name.",
        (FieldSpec("macro_name", "Macro", "select", "", context_options="macros"),),
        ("ProPresenter Actions", "Macro Actions"),
    ),
    ActionSpec("propresenter.timer_start", "Start Timer", "ProPresenter", "Start the configured timer.", path=("ProPresenter Actions", "Timer Actions")),
    ActionSpec("propresenter.timer_stop", "Stop Timer", "ProPresenter", "Stop the configured timer.", path=("ProPresenter Actions", "Timer Actions")),
    ActionSpec("propresenter.timer_reset", "Reset Timer", "ProPresenter", "Reset the configured timer.", path=("ProPresenter Actions", "Timer Actions")),
    ActionSpec(
        "propresenter.audio_trigger",
        "Trigger Audio",
        "ProPresenter",
        "Trigger an audio track from a playlist.",
        (
            FieldSpec("playlist", "Playlist", "select", "", context_options="audio_playlists"),
            FieldSpec("track", "Track", "text", "", "Exact track name, or a variable such as {{track}}."),
        ),
        ("ProPresenter Actions", "Audio Actions"),
    ),
    ActionSpec(
        "propresenter.audio_from_slide_label",
        "Play Audio From Slide Label",
        "ProPresenter",
        "Find and play the audio track named by the newly active slide label.",
        path=("ProPresenter Actions", "Audio Actions"),
    ),
    ActionSpec("propresenter.audio_clear", "Clear Audio", "ProPresenter", "Clear the audio layer.", path=("ProPresenter Actions", "Audio Actions")),
    ActionSpec("propresenter.audio_playlists", "Get Audio Playlists", "ProPresenter", "Return configured audio playlists.", path=("Read / Data Modules", "ProPresenter")),
    ActionSpec(
        "propresenter.audio_tracks",
        "Get Audio Tracks",
        "ProPresenter",
        "Return tracks in an audio playlist.",
        (FieldSpec("playlist", "Playlist", "select", "", context_options="audio_playlists"),),
        ("Read / Data Modules", "ProPresenter"),
    ),
    ActionSpec("propresenter.audio_active", "Get Active Audio", "ProPresenter", "Return active audio text.", path=("Read / Data Modules", "ProPresenter")),
    ActionSpec("health.get_status", "Get Health", "Health", "Return current integration health.", path=("Read / Data Modules", "System")),
    ActionSpec("system.get_debug", "Get Debug Snapshot", "System", "Return sanitized app, OBS, and runtime diagnostic data.", path=("Read / Data Modules", "System")),
    ActionSpec(
        "input_list.search_songs",
        "Search Song Library",
        "Input Lists",
        "Return fuzzy and phonetic matches from enabled song-library rows.",
        (
            FieldSpec("query", "Search text", "text", "{{query}}"),
            FieldSpec("list_key", "Input list key", "text", "song_library"),
            FieldSpec("limit", "Max results", "text", "25"),
        ),
        ("Read / Data Modules", "Input Lists"),
    ),
    ActionSpec(
        "obs.set_scene",
        "Set OBS Scene",
        "OBS",
        "Switch OBS to a scene using the transition policy.",
        (
            FieldSpec("scene", "Scene", "select", "", context_options="obs_scenes"),
            FieldSpec("transition_policy", "Use transition policy", "bool", True),
        ),
        ("OBS Actions", "Scene Actions"),
    ),
    ActionSpec("obs.get_current_scene", "Get Current OBS Scene", "OBS", "Return the current OBS scene.", path=("Read / Data Modules", "OBS")),
    ActionSpec(
        "obs.get_scene_items",
        "Get OBS Scene Items",
        "OBS",
        "Return scene items for a scene.",
        (FieldSpec("scene", "Scene", "select", "", context_options="obs_scenes"),),
        ("Read / Data Modules", "OBS"),
    ),
    ActionSpec(
        "obs.apply_scene_items",
        "Apply OBS Scene Items",
        "OBS",
        "Apply raw show/hide/items visibility payload to a scene.",
        (FieldSpec("scene", "Scene", "select", "", context_options="obs_scenes"),),
        ("OBS Actions", "Source Actions"),
    ),
    ActionSpec(
        "obs.legacy_set_sources",
        "Legacy Source Mode",
        "OBS",
        "Set announcement/camera source visibility using the legacy mode endpoint.",
        (
            FieldSpec("mode", "Mode", "select", "none", options=("none", "ann", "cam")),
            FieldSpec("scene", "Scene", "text", "ProPresenter Slides"),
            FieldSpec("srcAnn", "Announcement source", "text", "Audience Camera"),
            FieldSpec("srcCam", "Camera source", "text", "PTZ Camera"),
        ),
        ("OBS Actions", "Source Actions"),
    ),
    ActionSpec(
        "obs.apply_look_rule",
        "Apply OBS Look Rule",
        "OBS",
        "Apply a configured OBS source visibility rule by look name.",
        (FieldSpec("look_name", "Look name", "select", "", context_options="obs_looks"),),
        ("OBS Actions", "Look Actions"),
    ),
    ActionSpec(
        "obs.set_scene_item_enabled",
        "Set OBS Source Visibility",
        "OBS",
        "Show or hide a source in an OBS scene by scene item id or source name.",
        (
            FieldSpec("scene", "Scene", "select", "", context_options="obs_scenes"),
            FieldSpec("scene_item_id", "Scene item id", "text", "", "Preferred when known."),
            FieldSpec("source_name", "Source name", "text", "", "Used when scene item id is blank."),
            FieldSpec("enabled", "Visible", "bool", True),
        ),
        ("OBS Actions", "Source Actions"),
    ),
    ActionSpec("obs.reconnect", "Reconnect OBS", "OBS", "Reconnect OBS and refresh its health status.", path=("OBS Actions", "Connection Actions")),
    ActionSpec(
        "panasonic.recall_preset",
        "Recall Camera Preset",
        "Panasonic AWP",
        "Recall a Panasonic camera position preset.",
        (FieldSpec("preset", "Preset number", "text", "1", "Use a number, or bind to an endpoint input."),),
        ("PTZ Actions", "Preset Actions"),
    ),
    ActionSpec(
        "panasonic.save_preset",
        "Save Camera Preset",
        "Panasonic AWP",
        "Save the current camera position to a preset.",
        (FieldSpec("preset", "Preset number", "text", "1"),),
        ("PTZ Actions", "Preset Actions"),
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
        ("PTZ Actions", "Command Actions"),
    ),
    ActionSpec(
        "scoreboard.add_row",
        "Add Scoreboard Row",
        "Scoreboard",
        "Add a row to the scoreboard.",
        (FieldSpec("name", "Name", "text", ""), FieldSpec("score", "Score", "text", "0")),
        ("Scoreboard Actions", "Row Actions"),
    ),
    ActionSpec("scoreboard.get_state", "Get Scoreboard State", "Scoreboard", "Return scoreboard state.", path=("Read / Data Modules", "Scoreboard")),
    ActionSpec(
        "scoreboard.replace_state",
        "Replace Scoreboard State",
        "Scoreboard",
        "Replace scoreboard state with a request payload.",
        (FieldSpec("payload", "Payload", "text", "{{body}}"),),
        ("Scoreboard Actions", "Board Actions"),
    ),
    ActionSpec(
        "scoreboard.update_score",
        "Change Score",
        "Scoreboard",
        "Add a delta to a scoreboard row.",
        (
            FieldSpec("row_id", "Row ID", "text", "", "Preferred when known."),
            FieldSpec("name", "Name", "text", "", "Used when row ID is blank."),
            FieldSpec("delta", "Delta", "text", "1"),
        ),
        ("Scoreboard Actions", "Score Actions"),
    ),
    ActionSpec(
        "scoreboard.set_score",
        "Set Score",
        "Scoreboard",
        "Set a scoreboard row to an exact score.",
        (
            FieldSpec("row_id", "Row ID", "text", "", "Preferred when known."),
            FieldSpec("name", "Name", "text", "", "Used when row ID is blank."),
            FieldSpec("score", "Score", "text", "0"),
        ),
        ("Scoreboard Actions", "Score Actions"),
    ),
    ActionSpec(
        "scoreboard.clear_row",
        "Clear Row",
        "Scoreboard",
        "Set one scoreboard row to zero.",
        (
            FieldSpec("row_id", "Row ID", "text", "", "Preferred when known."),
            FieldSpec("name", "Name", "text", "", "Used when row ID is blank."),
        ),
        ("Scoreboard Actions", "Score Actions"),
    ),
    ActionSpec("scoreboard.clear_all", "Clear All Scores", "Scoreboard", "Remove all scoreboard rows.", path=("Scoreboard Actions", "Board Actions")),
    ActionSpec("scoreboard.undo", "Undo Scoreboard", "Scoreboard", "Undo the last scoreboard change.", path=("Scoreboard Actions", "Board Actions")),
    ActionSpec(
        "scoreboard.rename_row",
        "Rename Row",
        "Scoreboard",
        "Rename one scoreboard row.",
        (
            FieldSpec("row_id", "Row ID", "text", "", "Preferred when known."),
            FieldSpec("name", "Current name", "text", "", "Used when row ID is blank."),
            FieldSpec("new_name", "New name", "text", ""),
        ),
        ("Scoreboard Actions", "Row Actions"),
    ),
    ActionSpec(
        "runtime.auto_show",
        "Set Auto Show",
        "Runtime",
        "Enable or disable Auto Show Slides.",
        (FieldSpec("enabled", "Enabled", "bool", True),),
        ("Runtime Actions", "State Actions"),
    ),
    ActionSpec("runtime.get_auto_show", "Get Auto Show", "Runtime", "Return Auto Show Slides state.", path=("Read / Data Modules", "Runtime")),
    ActionSpec(
        "delay",
        "Wait",
        "Utility",
        "Pause execution before continuing to the next step.",
        (FieldSpec("seconds", "Seconds", "text", "1"),),
        ("Utility Actions", "Timing Actions"),
    ),
)


def action_spec(action_type: str) -> ActionSpec:
    for spec in ACTION_SPECS:
        if spec.action_type == action_type:
            return spec
    return ActionSpec(action_type, action_type, "Custom", "Custom action type.", ())


def default_action_params(action_type: str) -> dict[str, Any]:
    return {field.name: field.default for field in action_spec(action_type).fields}


def action_tree_path(action_type: str) -> tuple[str, ...]:
    spec = action_spec(action_type)
    return spec.path or (f"{spec.category} Actions",)


def action_options(context: Any, field: FieldSpec) -> list[str]:
    ref = field.context_options
    if ref:
        choices = input_list_choices(context.config, ref)
        if choices:
            return choices
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
    if field.context_options and "|" in value:
        return normalize_list_choice(value)
    return value.strip()
