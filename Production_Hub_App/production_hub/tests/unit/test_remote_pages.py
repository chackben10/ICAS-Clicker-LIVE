from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from production_hub.api.server import create_app
from production_hub.app.bootstrap import build_context
from production_hub.core.config.defaults import build_default_config
from production_hub.core.config.input_lists import row, static_cell
from production_hub.core.config.models import InputListDefinition
from production_hub.core.config.remote_pages import discover_remote_pages
from production_hub.core.endpoints.models import ActionDefinition, EndpointDefinition


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
