from __future__ import annotations

from production_hub.core.automation.models import AutomationDefinition
from production_hub.core.config.models import (
    AppConfig,
    MacroMapping,
    ObsLookRuleConfig,
    ObsSourceMapping,
    PresentationMapping,
    RemotePageConfig,
    ServiceLogoMapping,
)
from production_hub.core.endpoints.models import (
    ActionDefinition,
    EndpointDefinition,
    EndpointInputDefinition,
    EndpointMatchRule,
    EndpointResponseDefinition,
)
from production_hub.integrations.midi.models import MidiMapping


PRESENTATIONS = [
    PresentationMapping("Starting Announcements", "C62E6449-3FD6-42C1-BDF4-CABCA5F8E491"),
    PresentationMapping("PTZ Camera", "D47223A2-73BD-4C86-BB82-0D95E90D83F5"),
    PresentationMapping("Ending Announcements", "9CAAE21A-5AB2-41B3-B004-4135B36E134B"),
    PresentationMapping("Blank Preview", "7475C13E-FE99-4AF1-8760-526A845A1860"),
    PresentationMapping("iMac Screen", "AC813C59-FF90-483F-8532-406CF8DD056A"),
]

SERVICE_LOGOS = [
    ServiceLogoMapping("Basic Service Logo", "4ED2B2D8-EFE7-4875-BE88-186756A5E57E"),
    ServiceLogoMapping("Communion Service Logo", "82668B6D-5B98-4640-94E3-C69173FA4183"),
    ServiceLogoMapping("Youth Meeting Logo", "4B871221-EC8A-47A3-86F2-3E2D27311303"),
]

MACROS = [
    MacroMapping("Bible Macro", "Bible Macro"),
    MacroMapping("Malayalam Songs Macro", "Malayalam Songs Macro"),
    MacroMapping("[Aud] Malayalam Song Macro", "[Aud] Malayalam Song Macro"),
    MacroMapping("English Songs Macro", "English Songs Macro"),
    MacroMapping("[Aud] English Songs Macro", "[Aud] English Songs Macro"),
    MacroMapping("Presentation Macro", "Presentation Macro"),
    MacroMapping("Presentation Streamer Macro", "Presentation Streamer Macro"),
    MacroMapping("Presentation Picture-in-Picture Macro", "Presentation Picture-in-Picture Macro"),
    MacroMapping("Presentation Fullscreen Macro", "Presentation Fullscreen Macro"),
]

OBS_SOURCES = [
    ObsSourceMapping(72, "Mixer Input"),
    ObsSourceMapping(77, "Fullscreen Feed"),
    ObsSourceMapping(81, "PTZ Camera", "picture-in-picture placement"),
    ObsSourceMapping(73, "Streamer Plate"),
    ObsSourceMapping(78, "PTZ Camera", "streamer placement"),
    ObsSourceMapping(79, "Fullscreen Streamer"),
    ObsSourceMapping(75, "Audience Camera"),
    ObsSourceMapping(74, "PTZ Camera", "lower-third/song/Bible placement"),
    ObsSourceMapping(76, "LowerThirds Feed"),
    ObsSourceMapping(82, "Fullscreen LW3"),
]

OBS_LOOK_RULES = [
    ObsLookRuleConfig("Bible", "ProPresenter Input", [72, 74, 76], [77, 81, 73, 78, 79, 75, 82]),
    ObsLookRuleConfig("Malayalam Song", "ProPresenter Input", [72, 74, 76], [77, 81, 73, 78, 79, 75, 82]),
    ObsLookRuleConfig("[Aud] Malayalam Song", "ProPresenter Input", [72, 75, 76], [77, 81, 73, 78, 79, 74, 82]),
    ObsLookRuleConfig("English Song", "ProPresenter Input", [72, 74, 76], [77, 81, 73, 78, 79, 75, 82]),
    ObsLookRuleConfig("[Aud] English Song", "ProPresenter Input", [72, 75, 76], [77, 81, 73, 78, 79, 74, 82]),
    ObsLookRuleConfig("Presentation Slides", "ProPresenter Input", [72, 74, 82], [77, 81, 73, 78, 79, 75, 76]),
    ObsLookRuleConfig("Presentation Streamer", "ProPresenter Input", [72, 73, 78, 79], [77, 81, 75, 74, 76, 82]),
    ObsLookRuleConfig("Presentation Picture-in-Picture", "ProPresenter Input", [72, 77, 81], [73, 78, 79, 75, 74, 76, 82]),
    ObsLookRuleConfig("Presentation Fullscreen", "ProPresenter Input", [72, 77], [81, 73, 78, 79, 75, 74, 76, 82]),
]

PAD_NOTES = ("A", "A#", "B", "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#")


def build_default_midi_mappings() -> list[MidiMapping]:
    mappings: list[MidiMapping] = []
    for offset, note_name in enumerate(PAD_NOTES):
        mappings.append(MidiMapping.audio_pad(9 + offset, "Major Pads", f"{note_name} Major Pads", channel=-1))
        mappings.append(MidiMapping.audio_pad(21 + offset, "Minor Pads", f"{note_name} Minor Pads", channel=-1))
        mappings.append(MidiMapping.audio_pad(33 + offset, "Neutral Pads", f"{note_name} Neutral Pads", channel=-1))
    return mappings


def build_default_config() -> AppConfig:
    config = AppConfig()
    config.integrations.propresenter.presentations = list(PRESENTATIONS)
    config.integrations.propresenter.service_logos = list(SERVICE_LOGOS)
    config.integrations.propresenter.macros = list(MACROS)
    config.integrations.obs.known_scenes = [
        "Stream Start",
        "PTZ Camera",
        "ProPresenter Input",
        "Audience Camera",
        "Testimonies",
        "Stream Pause",
        "Thanks Screen",
    ]
    config.integrations.obs.source_mappings = list(OBS_SOURCES)
    config.integrations.obs.look_rules = list(OBS_LOOK_RULES)
    config.integrations.midi.mappings = [item.to_dict() for item in build_default_midi_mappings()]
    config.remote_pages = [
        RemotePageConfig("Presentation Clicker / Viewer", "index.html", required_integrations=["ProPresenter"]),
        RemotePageConfig("Media Control Panel", "control.html", required_integrations=["ProPresenter", "OBS"]),
        RemotePageConfig("iPad Control", "ipad-control.html", required_integrations=["ProPresenter", "OBS"]),
        RemotePageConfig("Pads Control", "pads-control.html", required_integrations=["ProPresenter"]),
        RemotePageConfig("Picker", "picker.html", required_integrations=["ProPresenter"]),
        RemotePageConfig("Debug", "debug.html", required_integrations=[]),
        RemotePageConfig("Scoreboard", "score.html", required_integrations=["Scoreboard Service"]),
        RemotePageConfig("Current Audio Display", "displays/current-audio.html", required_integrations=["ProPresenter"]),
        RemotePageConfig("Current Clock", "displays/current-clock.html"),
        RemotePageConfig("Countdown", "displays/countdown.html"),
        RemotePageConfig("Full Screen Countdown", "displays/full-screen-countdown.html"),
        RemotePageConfig("Fullscreen Wheel", "displays/fullscreen-wheel.html"),
        RemotePageConfig("Scoreboard Large", "scoreboard/large.html", required_integrations=["Scoreboard Service"]),
        RemotePageConfig("Scoreboard Dynamic", "scoreboard/dynamic.html", required_integrations=["Scoreboard Service"]),
        RemotePageConfig("Scoreboard Top Right", "scoreboard/top-right.html", required_integrations=["Scoreboard Service"]),
        RemotePageConfig("Scoreboard Bottom Right", "scoreboard/bottom-right.html", required_integrations=["Scoreboard Service"]),
    ]
    return config


def build_default_endpoints() -> list[EndpointDefinition]:
    ok_text = EndpointResponseDefinition("plain_text", "OK\n", "ERROR\n", "text/plain")
    data_response = EndpointResponseDefinition("last_action_data")
    preset_input = EndpointInputDefinition("preset", "Preset", "string", required=True)
    clear_input = EndpointInputDefinition("clearslide", "Clear slide after preset", "bool", default="false")
    safe_clear_input = EndpointInputDefinition("safeclear", "Safe clear slide after preset", "bool", default="false")
    logo_input = EndpointInputDefinition("service_logo_uuid", "Service logo", "select", option_source="service_logos")

    return [
        EndpointDefinition("health", "Health", "/health", [ActionDefinition("health.get_status")], allowed_methods=["GET"], response=EndpointResponseDefinition("plain_text", "OK", "ERROR", "text/plain"), behavior_mode="read"),
        EndpointDefinition("active_presentation", "Active Presentation", "/active-presentation", [ActionDefinition("propresenter.get_active_presentation")], allowed_methods=["GET"], response=data_response, behavior_mode="read"),
        EndpointDefinition("slide_index", "Slide Index", "/slide-index", [ActionDefinition("propresenter.get_slide_index")], allowed_methods=["GET"], response=data_response, behavior_mode="read"),
        EndpointDefinition("current_base", "Current Base", "/current-base", [ActionDefinition("propresenter.get_current_base")], allowed_methods=["GET"], response=data_response, behavior_mode="read"),
        EndpointDefinition("thumbnail", "Thumbnail", "/thumbnail", [ActionDefinition("propresenter.get_thumbnail", {"uuid": "{{uuid}}", "index": "{{index}}", "tier": "{{tier}}"})], allowed_methods=["GET"], inputs=[EndpointInputDefinition("uuid", "Presentation UUID", "string", required=True), EndpointInputDefinition("index", "Slide index", "integer", required=True, min_value="0"), EndpointInputDefinition("tier", "Quality tier", "select", default="low", options=["low", "high"])], response=EndpointResponseDefinition("binary", media_type="image/png"), behavior_mode="read"),
        EndpointDefinition("service_logos", "Service Logos", "/service_logos", [ActionDefinition("propresenter.get_service_logos")], allowed_methods=["GET"], response=data_response, behavior_mode="read"),
        EndpointDefinition("macros", "Macros", "/macros", [ActionDefinition("propresenter.get_macros")], allowed_methods=["GET"], response=data_response, behavior_mode="read"),
        EndpointDefinition("audio_playlists", "Audio Playlists", "/audio/playlists", [ActionDefinition("propresenter.audio_playlists")], allowed_methods=["GET"], response=data_response, behavior_mode="read"),
        EndpointDefinition("audio_tracks", "Audio Tracks", "/audio/tracks", [ActionDefinition("propresenter.audio_tracks", {"playlist": "{{playlist}}"})], allowed_methods=["GET"], inputs=[EndpointInputDefinition("playlist", "Playlist", "select", required=True, option_source="audio_playlists")], response=data_response, behavior_mode="read"),
        EndpointDefinition("audio_active", "Active Audio", "/audio/active", [ActionDefinition("propresenter.audio_active")], allowed_methods=["GET"], response=EndpointResponseDefinition("plain_text", "{{text}}", "", "text/plain; charset=utf-8"), behavior_mode="read"),
        EndpointDefinition("next_slide", "Next Slide", "/next", [ActionDefinition("propresenter.next_slide")], response=ok_text),
        EndpointDefinition("previous_slide", "Previous Slide", "/previous", [ActionDefinition("propresenter.previous_slide")], aliases=["/prev"], response=ok_text),
        EndpointDefinition("focus_slide", "Focus Slide", "/focus", [ActionDefinition("propresenter.focus_slide", {"index": "{{index}}"})], inputs=[EndpointInputDefinition("index", "Slide index", "integer", required=True, min_value="0")], response=ok_text),
        EndpointDefinition(
            "stream_beginning",
            "Stream Beginning",
            "/preset",
            [
                ActionDefinition("propresenter.trigger_presentation", {"label": "Starting Announcements"}),
                ActionDefinition("obs.set_scene", {"scene": "Stream Start", "transition_policy": True}),
            ],
            allowed_methods=["POST"],
            inputs=[preset_input],
            match_rules=[EndpointMatchRule("preset", "equals", "stream_beginning")],
            response=data_response,
        ),
        EndpointDefinition(
            "camera",
            "Camera",
            "/preset",
            [
                ActionDefinition("propresenter.trigger_presentation", {"label": "PTZ Camera"}),
                ActionDefinition("obs.set_scene", {"scene": "PTZ Camera", "transition_policy": True}),
                ActionDefinition("propresenter.clear_slide", {"delay_seconds": "{{clear_delay_seconds}}"}, condition="{{clearslide}}"),
                ActionDefinition("propresenter.clear_slide", {"delay_seconds": "{{clear_delay_seconds}}"}, condition="{{safeclear}}"),
            ],
            allowed_methods=["POST"],
            inputs=[preset_input, clear_input, safe_clear_input, EndpointInputDefinition("clear_delay_seconds", "Clear delay seconds", "float", default="0.5")],
            match_rules=[EndpointMatchRule("preset", "equals", "camera")],
            response=data_response,
        ),
        EndpointDefinition(
            "show_slides",
            "Show Slides",
            "/preset",
            [
                ActionDefinition("propresenter.clear_announcements"),
                ActionDefinition("obs.set_scene", {"scene": "ProPresenter Input", "transition_policy": True}),
            ],
            allowed_methods=["POST"],
            inputs=[preset_input],
            match_rules=[EndpointMatchRule("preset", "equals", "show_slides")],
            response=data_response,
        ),
        EndpointDefinition(
            "service_logo",
            "Service Logo",
            "/preset",
            [
                ActionDefinition("propresenter.trigger_service_logo", {"service_logo_uuid": "{{service_logo_uuid}}"}),
                ActionDefinition("obs.set_scene", {"scene": "Audience Camera", "transition_policy": True}),
                ActionDefinition("propresenter.clear_slide", {"delay_seconds": "{{clear_delay_seconds}}"}, condition="{{clearslide}}"),
                ActionDefinition("propresenter.clear_slide", {"delay_seconds": "{{clear_delay_seconds}}"}, condition="{{safeclear}}"),
            ],
            allowed_methods=["POST"],
            inputs=[preset_input, logo_input, clear_input, safe_clear_input, EndpointInputDefinition("clear_delay_seconds", "Clear delay seconds", "float", default="0.5")],
            match_rules=[EndpointMatchRule("preset", "equals", "service_logo")],
            response=data_response,
        ),
        EndpointDefinition(
            "testimonies",
            "Testimonies",
            "/preset",
            [
                ActionDefinition("propresenter.trigger_service_logo", {"service_logo_uuid": "{{service_logo_uuid}}"}),
                ActionDefinition("obs.set_scene", {"scene": "Testimonies", "transition_policy": True}),
            ],
            allowed_methods=["POST"],
            inputs=[preset_input, logo_input],
            match_rules=[EndpointMatchRule("preset", "equals", "testimonies")],
            response=data_response,
        ),
        EndpointDefinition(
            "ending_stream",
            "Ending Stream",
            "/preset",
            [
                ActionDefinition("propresenter.trigger_presentation", {"label": "Ending Announcements"}),
                ActionDefinition("obs.set_scene", {"scene": "Thanks Screen", "transition_policy": True}),
            ],
            allowed_methods=["POST"],
            inputs=[preset_input],
            match_rules=[EndpointMatchRule("preset", "equals", "ending_stream")],
            response=data_response,
        ),
        EndpointDefinition("clear_slide", "Clear Slide", "/preset", [ActionDefinition("propresenter.clear_slide")], allowed_methods=["POST"], inputs=[preset_input], match_rules=[EndpointMatchRule("preset", "equals", "clear_slide")], response=data_response),
        EndpointDefinition("safely_clear_slide", "Safely Clear Slide", "/preset", [ActionDefinition("propresenter.clear_slide")], allowed_methods=["POST"], inputs=[preset_input], match_rules=[EndpointMatchRule("preset", "equals", "safely_clear_slide")], response=data_response),
        EndpointDefinition(
            "nsc_setup",
            "NSC Setup",
            "/preset",
            [
                ActionDefinition("propresenter.clear_announcements"),
                ActionDefinition("propresenter.trigger_presentation", {"label": "iMac Screen"}),
            ],
            allowed_methods=["POST"],
            inputs=[preset_input],
            match_rules=[EndpointMatchRule("preset", "equals", "nsc_setup")],
            response=data_response,
        ),
        EndpointDefinition(
            "trigger_macro",
            "Trigger Macro",
            "/macro",
            [ActionDefinition("propresenter.trigger_macro", {"macro_name": "{{macro_name}}"})],
            allowed_methods=["POST"],
            inputs=[
                EndpointInputDefinition(name="macro_name", label="Macro", kind="select", option_source="macros"),
                EndpointInputDefinition(name="name", label="Legacy macro name", kind="select", option_source="macros"),
            ],
            response=data_response,
        ),
        EndpointDefinition("timer_start", "Timer Start", "/timer/start", [ActionDefinition("propresenter.timer_start")], response=ok_text),
        EndpointDefinition(
            "timer_stop_reset",
            "Timer Stop and Reset",
            "/timer/stop-reset",
            [
                ActionDefinition("propresenter.timer_stop"),
                ActionDefinition("delay", {"seconds": 0.5}),
                ActionDefinition("propresenter.timer_reset"),
            ],
            response=ok_text,
        ),
        EndpointDefinition(
            "audio_trigger",
            "Audio Trigger",
            "/audio/trigger",
            [ActionDefinition("propresenter.audio_trigger", {"playlist": "{{playlist}}", "track": "{{track}}"})],
            allowed_methods=["GET", "POST"],
            inputs=[
                EndpointInputDefinition(
                    name="playlist",
                    label="Playlist",
                    kind="select",
                    required=True,
                    option_source="audio_playlists",
                ),
                EndpointInputDefinition(name="track", label="Track", kind="text", required=True),
            ],
            response=data_response,
        ),
        EndpointDefinition("audio_clear", "Audio Clear", "/audio/clear", [ActionDefinition("propresenter.audio_clear")], response=data_response),
        EndpointDefinition("auto_show_get", "Get Auto Show", "/auto-show", [ActionDefinition("runtime.get_auto_show")], allowed_methods=["GET"], response=data_response, behavior_mode="read"),
        EndpointDefinition("auto_show_set", "Set Auto Show", "/auto-show", [ActionDefinition("runtime.auto_show", {"enabled": "{{enabled}}"})], allowed_methods=["POST"], inputs=[EndpointInputDefinition("enabled", "Enabled", "bool", required=True)], response=data_response),
        EndpointDefinition("score_get", "Get Scoreboard", "/score", [ActionDefinition("scoreboard.get_state")], allowed_methods=["GET"], response=data_response, behavior_mode="read"),
        EndpointDefinition("score_set", "Set Scoreboard", "/score", [ActionDefinition("scoreboard.replace_state")], allowed_methods=["POST"], response=data_response),
        EndpointDefinition("scene_current", "Current OBS Scene", "/scene/current", [ActionDefinition("obs.get_current_scene")], allowed_methods=["GET"], response=data_response, behavior_mode="read"),
        EndpointDefinition("scene_set", "Set OBS Scene", "/scene/set", [ActionDefinition("obs.set_scene", {"scene": "{{name}}", "transition": "{{transition}}", "duration": "{{duration}}"})], allowed_methods=["GET"], inputs=[EndpointInputDefinition("name", "Scene", "select", required=True, option_source="obs_scenes"), EndpointInputDefinition("transition", "Transition", "string"), EndpointInputDefinition("duration", "Duration ms", "integer")], response=data_response),
        EndpointDefinition("scene_items", "OBS Scene Items", "/scene/items", [ActionDefinition("obs.get_scene_items", {"scene": "{{scene}}"})], allowed_methods=["GET"], inputs=[EndpointInputDefinition("scene", "Scene", "select", option_source="obs_scenes")], response=data_response, behavior_mode="read"),
        EndpointDefinition("scene_items_apply", "Apply OBS Scene Items", "/scene/items/apply", [ActionDefinition("obs.apply_scene_items", {"scene": "{{scene}}"})], allowed_methods=["POST"], inputs=[EndpointInputDefinition("scene", "Scene", "select", option_source="obs_scenes")], response=data_response),
        EndpointDefinition("legacy_obs_set", "Legacy OBS Source Mode", "/set", [ActionDefinition("obs.legacy_set_sources", {"mode": "{{mode}}", "scene": "{{scene}}", "srcAnn": "{{srcAnn}}", "srcCam": "{{srcCam}}"})], allowed_methods=["GET"], inputs=[EndpointInputDefinition("mode", "Mode", "select", required=True, options=["none", "ann", "cam"]), EndpointInputDefinition("scene", "Scene", "string", default="ProPresenter Slides"), EndpointInputDefinition("srcAnn", "Announcement source", "string", default="Audience Camera"), EndpointInputDefinition("srcCam", "Camera source", "string", default="PTZ Camera")], response=ok_text),
        EndpointDefinition("obs_propresenter_input_items", "ProPresenter Input OBS Items", "/obs/propresenter-input/items", [ActionDefinition("obs.get_scene_items", {"scene": "ProPresenter Input"})], allowed_methods=["GET"], response=data_response, behavior_mode="read"),
        EndpointDefinition("obs_look_refresh", "Refresh OBS Look", "/obs/look/refresh", [ActionDefinition("obs.apply_look_rule", {"look_name": "{{current_look}}"})], allowed_methods=["GET"], response=data_response),
        EndpointDefinition("debug", "Debug Snapshot", "/api/debug", [ActionDefinition("system.get_debug")], allowed_methods=["GET"], response=data_response, behavior_mode="read"),
        EndpointDefinition("admin_health", "Admin Health", "/admin/health", [ActionDefinition("health.get_status")], allowed_methods=["GET"], response=data_response, behavior_mode="read"),
        EndpointDefinition(
            "song_library_search",
            "Song Library Search",
            "/song-library/search",
            [ActionDefinition("input_list.search_songs", {"query": "{{query}}", "list_key": "song_library", "limit": "25"})],
            allowed_methods=["GET", "POST"],
            inputs=[
                EndpointInputDefinition("query", "Search text", "string"),
                EndpointInputDefinition("q", "Search text alias", "string"),
            ],
            aliases=["/songs/search"],
            response=data_response,
            behavior_mode="read",
        ),
        EndpointDefinition("camera_preset_recall", "Recall Camera Preset", "/camera/preset", [ActionDefinition("panasonic.recall_preset", {"preset": "{{preset}}"})], allowed_methods=["POST"], inputs=[EndpointInputDefinition("preset", "Preset number", "integer", required=True, min_value="1", max_value="100")], response=data_response),
        EndpointDefinition("camera_preset_recall_get", "Recall Camera Preset by Path", "/camera/preset/{preset:int}", [ActionDefinition("panasonic.recall_preset", {"preset": "{{preset}}"})], allowed_methods=["GET"], inputs=[EndpointInputDefinition("preset", "Preset number", "integer", required=True, min_value="1", max_value="100")], response=data_response),
    ]


def build_default_automations() -> list[AutomationDefinition]:
    return [
        AutomationDefinition(
            key="bible_look_enforcement",
            name="Bible Look Enforcement",
            trigger="interval",
            enabled=True,
            interval_seconds=0.75,
            cooldown_seconds=2.5,
            conditions=[
                {
                    "condition_type": "propresenter.current_look",
                    "params": {"look_name": "Bible", "matches": False},
                }
            ],
            actions=[ActionDefinition("propresenter.trigger_macro", {"macro_name": "Bible Macro"})],
            description="Trigger Bible Macro when a one-group colon-titled active presentation is not using the Bible look.",
        ),
        AutomationDefinition(
            key="obs_look_sync",
            name="Sync OBS Layout From ProPresenter Look",
            trigger="look_changed_or_poll",
            enabled=True,
            debounce_seconds=0.20,
            conditions=[{"condition_type": "always", "params": {}}],
            actions=[ActionDefinition("obs.apply_look_rule", {"look_name": "{{current_look}}"})],
            description="Apply OBS source visibility rules when the current ProPresenter look matches a configured rule.",
        ),
        AutomationDefinition(
            key="slide_label_audio_sync",
            name="Slide Label Audio Sync",
            trigger="active_slide_changed",
            enabled=True,
            debounce_seconds=0.5,
            conditions=[{"condition_type": "always", "params": {}}],
            actions=[ActionDefinition("propresenter.audio_trigger", {"playlist": "{{playlist}}", "track": "{{slide_label}}"})],
            description="Trigger matching pad audio from active slide labels.",
        ),
        AutomationDefinition(
            key="auto_show_slides",
            name="Auto Show Slides",
            trigger="presentation_state_changed",
            enabled=True,
            conditions=[{"condition_type": "runtime.auto_show_enabled", "params": {"enabled": True}}],
            actions=[
                ActionDefinition("propresenter.clear_announcements"),
                ActionDefinition("obs.set_scene", {"scene": "ProPresenter Input", "transition_policy": True}),
            ],
            description="Clear announcements and move OBS to the ProPresenter Input scene when Auto Show is enabled.",
        ),
        AutomationDefinition(
            key="obs_connection_watchdog",
            name="OBS Connection Watchdog",
            trigger="interval",
            enabled=True,
            interval_seconds=4.0,
            conditions=[{"condition_type": "always", "params": {}}],
            actions=[ActionDefinition("obs.reconnect")],
            description="Reconnect OBS safely when disconnected.",
        ),
    ]
