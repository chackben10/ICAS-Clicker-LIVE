from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from production_hub.api.server import create_app
from production_hub.app.bootstrap import build_context
from production_hub.core.config.defaults import build_default_config
from production_hub.core.config.input_lists import row, static_cell
from production_hub.core.config.models import InputListDefinition
from production_hub.core.config.remote_pages import discover_remote_pages
from production_hub.core.endpoints.models import ActionDefinition, EndpointDefinition, EndpointInputDefinition


class InteractiveNestingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.button_depth = 0
        self.selects_inside_buttons = 0
        self.ids: set[str] = set()
        self.duplicate_ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs) -> None:
        element_id = next((value for key, value in attrs if key == "id"), None)
        if element_id:
            if element_id in self.ids:
                self.duplicate_ids.add(element_id)
            self.ids.add(element_id)
        if tag == "button":
            self.button_depth += 1
        elif tag == "select" and self.button_depth:
            self.selects_inside_buttons += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "button" and self.button_depth:
            self.button_depth -= 1


class RemotePageDiscoveryTests(unittest.TestCase):
    def test_root_clicker_has_live_song_library_search_view(self) -> None:
        root_index = Path(__file__).resolve().parents[4] / "index.html"
        html = root_index.read_text(encoding="utf-8")
        self.assertIn('id="searchModeButton"', html)
        self.assertIn('id="songSearchInput"', html)
        self.assertIn('id="searchResultsBody"', html)
        self.assertIn('/song-library/search?query=', html)
        self.assertIn('dom.songSearchInput.addEventListener("input"', html)
        self.assertIn('.search-result-lyrics', html)
        self.assertIn('result?.match_field === "lyrics"', html)
        self.assertIn('typeof result?.lyric_preview === "string"', html)
        self.assertIn('lyrics.textContent = lyricPreview', html)
        self.assertIn('row.setAttribute("aria-describedby", lyrics.id)', html)
        self.assertIn('/clicker-presentation-activation', html)
        self.assertIn('clickerPresentationActivationEnabled', html)
        self.assertIn('clicker_presentation_activation_disabled', html)
        self.assertIn('Song Book Mode', html)
        self.assertIn('interactionLocked: previewTriggerInFlight || clickerPresentationActivationEnabled !== true', html)

    def test_control_page_has_shared_clicker_toggle_and_obs_safe_pickers(self) -> None:
        root_control = Path(__file__).resolve().parents[4] / "control.html"
        html = root_control.read_text(encoding="utf-8")

        self.assertIn('id="clickerActivationToggle"', html)
        self.assertIn('/clicker-presentation-activation', html)
        self.assertIn('id="obsModeToggle"', html)
        self.assertIn('icas-control-obs-mode', html)
        self.assertIn('id="pickerOverlay"', html)
        self.assertIn('function openSelectionPicker(', html)
        self.assertIn('<span class="picker-open-action" aria-hidden="true">▾</span>', html)
        self.assertNotIn('<span class="picker-open-action">Choose</span>', html)
        self.assertNotIn('body.obs-mode .grid', html)
        for button, preset in {
            "btnStreamBeginning": "stream_beginning",
            "btnCamera": "camera",
            "btnServiceLogo": "service_logo",
            "btnShowSlides": "show_slides",
            "btnTestimonies": "testimonies",
            "btnEndingStream": "ending_stream",
            "btnClearSlide": "clear_slide",
            "btnNSCSetup": "nsc_setup",
        }.items():
            self.assertIn(
                f'attachTapHandler({button}, () => runPreset("{preset}"));',
                html,
            )
        self.assertIn('setInterval(refreshRuntimeState, 10000)', html)
        self.assertNotIn('setInterval(fullRefresh, 10000)', html)
        self.assertIn('const payload = { preset: presetName, clearslide: true };', html)
        self.assertIn('const payload = { macro_name: name, name };', html)

        parser = InteractiveNestingParser()
        parser.feed(html)
        self.assertEqual(0, parser.selects_inside_buttons)
        self.assertEqual(set(), parser.duplicate_ids)

        referenced_ids = set(re.findall(r'document\.getElementById\("([^"]+)"\)', html))
        self.assertEqual(set(), referenced_ids - parser.ids)

        for interaction in [
            'attachTapHandler(btnSetMacro, () => runMacro());',
            'attachTapHandler(btnCameraClearSlide, () => runPresetClearSlide("camera"));',
            'attachTapHandler(btnServiceLogoClearSlide, () => runPresetClearSlide("service_logo"));',
            'attachTapHandler(btnAudioControl, () => openAudioModal());',
            'attachTapHandler(btnClearAudio, async () => {',
            'autoShowToggle.addEventListener("change"',
            'clickerActivationToggle.addEventListener("change"',
            'obsModeToggle.addEventListener("change"',
            'btnTextSizeSlider.addEventListener("input"',
        ]:
            self.assertIn(interaction, html)

    def test_control_page_interface_scale_covers_main_page_and_settings(self) -> None:
        root_control = Path(__file__).resolve().parents[4] / "control.html"
        html = root_control.read_text(encoding="utf-8")

        for selector, property_name in {
            ".page-title": "font-size",
            ".top-btn": "font-size",
            ".remote-switch": "padding",
            ".remote-switch-label": "font-size",
            ".switch-track": "width",
            ".panel-title": "font-size",
            ".desc": "font-size",
            ".settings-card": "font-size",
            ".settings-section": "padding",
            ".settings-label": "font-size",
            ".settings-muted": "font-size",
            ".mini-btn": "font-size",
            ".segment button": "font-size",
            ".settings-close-btn": "font-size",
        }.items():
            self.assertRegex(
                html,
                rf"(?s){selector.replace('.', r'\.') }\s*\{{[^}}]*"
                rf"{property_name}:\s*[^;]*var\(--btn-scale\)",
            )

        self.assertIn('<div class="settings-label">Interface Size</div>', html)

    def test_control_macro_payload_works_with_legacy_required_input(self) -> None:
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            context.endpoint_registry.replace_all(
                [
                    EndpointDefinition(
                        "trigger_macro",
                        "Trigger Macro",
                        "/macro",
                        [ActionDefinition("propresenter.trigger_macro")],
                        allowed_methods=["POST"],
                        inputs=[
                            EndpointInputDefinition(
                                "macro_name",
                                "Macro",
                                "select",
                                required=True,
                            )
                        ],
                    )
                ]
            )
            context.propresenter.trigger_macro = AsyncMock(return_value=True)

            response = TestClient(create_app(context)).post(
                "/macro",
                json={"macro_name": "Stage Display", "name": "Stage Display"},
            )

            self.assertEqual(200, response.status_code)
            context.propresenter.trigger_macro.assert_awaited_once_with("Stage Display")

    def test_all_control_page_backend_contracts_execute(self) -> None:
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            context.propresenter.trigger_presentation_label = AsyncMock(return_value=True)
            context.propresenter.clear_announcements = AsyncMock(return_value=True)
            context.propresenter.trigger_service_logo = AsyncMock(return_value=True)
            context.propresenter.clear_slide = AsyncMock(return_value=True)
            context.propresenter.trigger_macro = AsyncMock(return_value=True)
            context.obs.set_scene = AsyncMock(return_value=True)
            context.propresenter.audio.playlists = AsyncMock(return_value=["Pads"])
            context.propresenter.audio.playlist_tracks = AsyncMock(return_value=["Peace.mp3"])
            context.propresenter.audio.active_text = AsyncMock(return_value="Peace.mp3")
            context.propresenter.audio.find_track_in_playlist = AsyncMock(return_value=None)
            context.propresenter.audio.trigger = AsyncMock(return_value=True)
            context.propresenter.audio.clear = AsyncMock(return_value=True)
            client = TestClient(create_app(context))

            for route in [
                "/health",
                "/service_logos",
                "/macros",
                "/audio/playlists",
                "/audio/tracks?playlist=Pads",
                "/audio/active",
                "/auto-show",
                "/clicker-presentation-activation",
            ]:
                with self.subTest(method="GET", route=route):
                    self.assertEqual(200, client.get(route).status_code)

            preset_payloads = [
                {"preset": "stream_beginning"},
                {"preset": "camera"},
                {"preset": "service_logo", "service_logo_uuid": "service-logo-uuid"},
                {"preset": "show_slides"},
                {"preset": "testimonies", "service_logo_uuid": "testimony-logo-uuid"},
                {"preset": "ending_stream"},
                {"preset": "clear_slide"},
                {"preset": "nsc_setup"},
                {"preset": "camera", "clearslide": True},
                {
                    "preset": "service_logo",
                    "service_logo_uuid": "service-logo-uuid",
                    "clearslide": True,
                },
            ]
            for payload in preset_payloads:
                with self.subTest(method="POST", route="/preset", payload=payload):
                    self.assertEqual(200, client.post("/preset", json=payload).status_code)

            post_requests = [
                ("/macro", {"macro_name": "Stage Display", "name": "Stage Display"}),
                ("/audio/trigger", {"playlist": "Pads", "track": "Peace.mp3"}),
                ("/audio/clear", None),
                ("/auto-show", {"enabled": True}),
                ("/clicker-presentation-activation", {"enabled": False}),
            ]
            for route, payload in post_requests:
                with self.subTest(method="POST", route=route):
                    response = client.post(route, json=payload) if payload is not None else client.post(route)
                    self.assertEqual(200, response.status_code)

            context.propresenter.trigger_macro.assert_awaited_with("Stage Display")
            context.propresenter.audio.trigger.assert_awaited_with("Pads", "Peace.mp3")
            context.propresenter.audio.clear.assert_awaited_once()

    def test_discovers_all_repository_html_pages(self) -> None:
        workspace = Path(__file__).resolve().parents[4]
        pages = discover_remote_pages(workspace, build_default_config().remote_pages)
        paths = {str(page["path"]) for page in pages}

        self.assertIn("ipad-control.html", paths)
        self.assertIn("pads-control.html", paths)
        self.assertIn("scoreboard/large.html", paths)
        self.assertIn("displays/current-audio.html", paths)

    def test_song_search_endpoint_returns_context_for_a_phonetic_lyric_match(self) -> None:
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            context.config.ui.input_lists = [
                InputListDefinition(
                    "song_library",
                    "Song Library",
                    rows=[
                        row(
                            True,
                            library_name=static_cell("Malayalam Songs"),
                            songs=static_cell(
                                [
                                    {
                                        "name": "Unrelated Title",
                                        "uuid": "lyric-match-uuid",
                                        "lyrics": "Before the line Daivame nin sneham ennum after the line",
                                    }
                                ]
                            ),
                        )
                    ],
                )
            ]
            response = TestClient(create_app(context)).get(
                "/song-library/search",
                params={"query": "dyvame nin snehem"},
            )

            self.assertEqual(200, response.status_code)
            payload = response.json()
            self.assertIsInstance(payload, list)
            self.assertEqual("lyric-match-uuid", payload[0]["uuid"])
            self.assertEqual("lyrics", payload[0]["match_field"])
            self.assertIn("Daivame nin sneham", payload[0]["lyric_preview"])
            self.assertNotIn("lyrics", payload[0])

    def test_current_audio_root_alias_serves_display_page(self) -> None:
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            client = TestClient(create_app(context))
            response = client.get("/current-audio.html")
            self.assertEqual(200, response.status_code)
            self.assertIn("Current Audio", response.text)
            extensionless = client.get("/displays/current-audio")
            self.assertEqual(200, extensionless.status_code)
            self.assertIn("Current Audio", extensionless.text)

    def test_remote_pages_win_over_configured_endpoint_conflicts(self) -> None:
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            for key, route in [("index", "/index"), ("control", "/control"), ("debug", "/debug")]:
                context.endpoint_registry.register(
                    EndpointDefinition(
                        f"conflicting_{key}",
                        f"Conflicting {key}",
                        route,
                        [ActionDefinition("delay", {"seconds": "0"})],
                    )
                )
            client = TestClient(create_app(context))
            for route in ["/index", "/control", "/debug"]:
                response = client.get(route)
                self.assertEqual(200, response.status_code)
                self.assertIn("<!DOCTYPE html>", response.text)
                self.assertIn("text/html", response.headers.get("content-type", ""))

    def test_configured_endpoint_response_keeps_cors_headers(self) -> None:
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            context.config.api.cors_allow_origins = ["https://icas-clicker.work"]
            client = TestClient(create_app(context))

            response = client.get("/health", headers={"Origin": "https://icas-clicker.work"})

            self.assertEqual(200, response.status_code)
            self.assertEqual("https://icas-clicker.work", response.headers.get("access-control-allow-origin"))
            self.assertEqual("true", response.headers.get("access-control-allow-private-network"))
            self.assertIn("Origin", response.headers.get("vary", ""))


if __name__ == "__main__":
    unittest.main()
