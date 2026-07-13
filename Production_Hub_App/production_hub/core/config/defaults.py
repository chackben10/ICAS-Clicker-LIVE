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
from production_hub.core.endpoints.models import ActionDefinition, EndpointDefinition, EndpointInputDefinition
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
    return [
        EndpointDefinition("next_slide", "Next Slide", "/next", [ActionDefinition("propresenter.next_slide")]),
        EndpointDefinition("previous_slide", "Previous Slide", "/previous", [ActionDefinition("propresenter.previous_slide")]),
        EndpointDefinition("focus_slide", "Focus Slide", "/focus", [ActionDefinition("propresenter.focus_slide")]),
        EndpointDefinition(
            "stream_beginning",
            "Stream Beginning",
            "/preset",
            [
                ActionDefinition("propresenter.trigger_presentation", {"label": "Starting Announcements"}),
                ActionDefinition("obs.set_scene", {"scene": "Stream Start", "transition_policy": True}),
            ],
        ),
        EndpointDefinition(
            "camera",
            "Camera",
            "/preset",
            [
                ActionDefinition("propresenter.trigger_presentation", {"label": "PTZ Camera"}),
                ActionDefinition("obs.set_scene", {"scene": "PTZ Camera", "transition_policy": True}),
            ],
        ),
        EndpointDefinition(
            "show_slides",
            "Show Slides",
            "/preset",
            [
                ActionDefinition("propresenter.clear_announcements"),
                ActionDefinition("obs.set_scene", {"scene": "ProPresenter Input", "transition_policy": True}),
            ],
        ),
        EndpointDefinition(
            "service_logo",
            "Service Logo",
            "/preset",
            [
                ActionDefinition("propresenter.trigger_service_logo"),
                ActionDefinition("obs.set_scene", {"scene": "Audience Camera", "transition_policy": True}),
            ],
        ),
        EndpointDefinition(
            "testimonies",
            "Testimonies",
            "/preset",
            [
                ActionDefinition("propresenter.trigger_service_logo"),
                ActionDefinition("obs.set_scene", {"scene": "Testimonies", "transition_policy": True}),
            ],
        ),
        EndpointDefinition(
            "ending_stream",
            "Ending Stream",
            "/preset",
            [
                ActionDefinition("propresenter.trigger_presentation", {"label": "Ending Announcements"}),
                ActionDefinition("obs.set_scene", {"scene": "Thanks Screen", "transition_policy": True}),
            ],
        ),
        EndpointDefinition("clear_slide", "Clear Slide", "/preset", [ActionDefinition("propresenter.clear_slide")]),
        EndpointDefinition(
            "nsc_setup",
            "NSC Setup",
            "/preset",
            [
                ActionDefinition("propresenter.clear_announcements"),
                ActionDefinition("propresenter.trigger_presentation", {"label": "iMac Screen"}),
            ],
        ),
        EndpointDefinition(
            "trigger_macro",
            "Trigger Macro",
            "/macro",
            [ActionDefinition("propresenter.trigger_macro")],
            inputs=[
                EndpointInputDefinition(
                    name="macro_name",
                    label="Macro",
                    kind="select",
                    required=True,
                    option_source="macros",
                )
            ],
        ),
        EndpointDefinition("timer_start", "Timer Start", "/timer/start", [ActionDefinition("propresenter.timer_start")]),
        EndpointDefinition(
            "timer_stop_reset",
            "Timer Stop and Reset",
            "/timer/stop-reset",
            [
                ActionDefinition("propresenter.timer_stop"),
                ActionDefinition("delay", {"seconds": 0.5}),
                ActionDefinition("propresenter.timer_reset"),
            ],
        ),
        EndpointDefinition(
            "audio_trigger",
            "Audio Trigger",
            "/audio/trigger",
            [ActionDefinition("propresenter.audio_trigger")],
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
        ),
        EndpointDefinition("audio_clear", "Audio Clear", "/audio/clear", [ActionDefinition("propresenter.audio_clear")]),
        EndpointDefinition("auto_show", "Auto Show", "/auto-show", [ActionDefinition("runtime.auto_show")]),
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
