from __future__ import annotations

import asyncio
import os
import threading
import time
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox

from production_hub.core.config.input_lists import (
    column,
    input_list_by_key,
    polled_object_array_cell,
    row,
    song_object_fields,
    static_cell,
)
from production_hub.core.config.models import AppConfig, InputListDefinition
from production_hub.core.endpoints.search import search_song_library
from production_hub.state.undo_manager import UndoManager
from production_hub.ui.pages.common import run_background
from production_hub.ui.pages.input_lists_page import InputListsPage


APP = QApplication.instance() or QApplication([])


class UiBackgroundTests(unittest.TestCase):
    def test_completion_callback_runs_on_the_gui_thread(self) -> None:
        completed = threading.Event()
        callback_threads: list[threading.Thread] = []
        messages: list[tuple[bool, str]] = []

        async def work() -> str:
            return "finished"

        def done(ok: bool, message: str) -> None:
            callback_threads.append(threading.current_thread())
            messages.append((ok, message))
            completed.set()

        run_background(work, done)
        deadline = time.monotonic() + 2
        while not completed.is_set() and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(0.01)

        self.assertTrue(completed.is_set())
        self.assertEqual([(True, "finished")], messages)
        self.assertIs(threading.main_thread(), callback_threads[0])

    def test_input_list_page_refreshes_after_enabled_row_poll(self) -> None:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                columns=[
                    column("library_name", "Library Name"),
                    column("songs", "Songs", "array_object"),
                ],
                rows=[
                    row(
                        True,
                        library_name=static_cell("English Songs"),
                        songs=polled_object_array_cell(
                            "v1/library/English%20Songs",
                            "items[]",
                            song_object_fields(),
                        ),
                    )
                ],
            )
        ]

        class Client:
            def __init__(self) -> None:
                self.library_calls = 0

            async def get_json(self, path):
                if path.startswith("library/"):
                    self.library_calls += 1
                    return {"items": [{"name": "Amazing Grace", "uuid": "english-uuid"}]}
                return {"presentation": {"groups": [{"slides": [{"text": "Amazing grace lyrics"}]}]}}

        class Repository:
            def save_app_config(self, _config) -> None:
                pass

        client = Client()
        context = SimpleNamespace(
            config=config,
            propresenter=SimpleNamespace(client=client),
            config_repository=Repository(),
        )
        page = InputListsPage(context)
        self.assertFalse(page.status.isHidden())
        page.load_list(input_list_by_key(config, "song_library"))
        page.poll_enabled_row("song_library", 0)
        page.poll_now()

        deadline = time.monotonic() + 2
        while page._active_poll_keys and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(0.01)

        self.assertEqual(set(), page._active_poll_keys)
        self.assertTrue(page.poll_now_button.isEnabled())
        self.assertEqual(1, client.library_calls)
        self.assertIn("Search data is ready", page.status.text())
        cell = page.cell_from_table(0, 2)
        self.assertEqual("Amazing Grace", cell.value[0]["name"])
        self.assertEqual("Amazing grace lyrics", cell.value[0]["lyrics"])

    def test_song_row_checkbox_preserves_cache_and_reuses_it_when_reenabled(self) -> None:
        cached_songs = [
            {
                "name": "Amazing Grace",
                "uuid": "english-uuid",
                "lyrics": "cached lyric phrase",
            }
        ]
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                columns=[
                    column("library_name", "Library Name"),
                    column("songs", "Songs", "array_object"),
                ],
                rows=[
                    row(
                        True,
                        library_name=static_cell("English Songs"),
                        songs=polled_object_array_cell(
                            "v1/library/English%20Songs",
                            "items[]",
                            song_object_fields(),
                            cached_songs,
                        ),
                    )
                ],
            )
        ]
        library_started = threading.Event()
        release_library = threading.Event()

        class Client:
            async def get_json(self, path):
                if path.startswith("library/"):
                    library_started.set()
                    while not release_library.is_set():
                        await asyncio.sleep(0.01)
                    return {"items": [{"name": "Amazing Grace", "uuid": "english-uuid"}]}
                return {"presentation": {"groups": [{"slides": [{"text": "refreshed lyrics"}]}]}}

        class Repository:
            def save_app_config(self, _config) -> None:
                pass

        context = SimpleNamespace(
            config=config,
            propresenter=SimpleNamespace(client=Client()),
            config_repository=Repository(),
            undo_manager=UndoManager(),
        )
        page = InputListsPage(context)
        page.load_list(input_list_by_key(config, "song_library"))
        checkbox = page.rows_table.cellWidget(0, 0).findChild(QCheckBox)
        self.assertIsNotNone(checkbox)

        # Simulate a scheduler checkpoint replacing the live definition while
        # the visible table still holds the previous cache snapshot.
        newest_cached_songs = [
            *cached_songs,
            {
                "name": "Newest Cached Song",
                "uuid": "newest-cache-uuid",
                "lyrics": "latest cache phrase",
            },
        ]
        newest_definition = InputListDefinition.from_dict(config.ui.input_lists[0].to_dict())
        newest_cell = newest_definition.rows[0].cells["songs"]
        newest_cell.value = newest_cached_songs
        newest_cell.preview = "2 objects: Amazing Grace, Newest Cached Song"
        newest_cell.object_enrichment_last_polled = {"lyrics": 12345}
        config.ui.input_lists[0] = newest_definition

        checkbox.setChecked(False)
        saved_row = context.config.ui.input_lists[0].rows[0]
        self.assertFalse(saved_row.enabled)
        self.assertEqual(newest_cached_songs, saved_row.cells["songs"].value)
        self.assertEqual("2 objects: Amazing Grace, Newest Cached Song", saved_row.cells["songs"].preview)
        self.assertEqual({"lyrics": 12345}, saved_row.cells["songs"].object_enrichment_last_polled)
        self.assertEqual([], search_song_library(context, "Amazing Grace"))

        checkbox.setChecked(True)
        self.assertEqual("english-uuid", search_song_library(context, "cached lyric phrase")[0]["uuid"])
        self.assertEqual("newest-cache-uuid", search_song_library(context, "latest cache phrase")[0]["uuid"])

        deadline = time.monotonic() + 2
        while not library_started.is_set() and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(0.01)
        self.assertTrue(library_started.is_set())
        release_library.set()
        deadline = time.monotonic() + 2
        while page._active_poll_keys and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(0.01)
        self.assertEqual(set(), page._active_poll_keys)


if __name__ == "__main__":
    unittest.main()
