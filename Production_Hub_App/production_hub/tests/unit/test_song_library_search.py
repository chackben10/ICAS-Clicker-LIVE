from __future__ import annotations

import unittest
from types import SimpleNamespace

from production_hub.core.config.input_lists import (
    column,
    ensure_default_input_lists,
    poll_input_list_definition,
    poll_input_list_row_by_key,
    polled_cell,
    polled_dictionary_cell,
    row,
    static_cell,
)
from production_hub.core.config.models import AppConfig, InputListDefinition
from production_hub.core.endpoints.search import phonetic_key, search_song_library


class SongLibrarySearchTests(unittest.TestCase):
    def context_with_songs(self) -> SimpleNamespace:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                columns=[],
                rows=[
                    row(
                        True,
                        library_name=static_cell("Malayalam Songs"),
                        songs=static_cell(
                            [
                                "Daivame Nin Sneham",
                                "Ente Daivam Swarga Simhasanam",
                                "Yeshuve Nin Sneham",
                            ]
                        ),
                    ),
                    row(
                        False,
                        library_name=static_cell("Disabled Songs"),
                        songs=static_cell(["Hidden Disabled Song"]),
                    ),
                ],
            )
        ]
        return SimpleNamespace(config=config)

    def test_search_uses_enabled_song_rows_only(self) -> None:
        results = search_song_library(self.context_with_songs(), "hidden")
        self.assertEqual([], results)

    def test_search_handles_typos_and_phonetic_transliteration(self) -> None:
        results = search_song_library(self.context_with_songs(), "dyvame nin sneham")
        self.assertGreater(len(results), 0)
        self.assertEqual("Daivame Nin Sneham", results[0]["name"])
        self.assertEqual(phonetic_key("daivame"), phonetic_key("dyvame"))

    def test_search_limits_results_to_25(self) -> None:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                rows=[
                    row(
                        True,
                        library_name=static_cell("Malayalam Songs"),
                        songs=static_cell([f"Sneham Song {index}" for index in range(40)]),
                    )
                ],
            )
        ]
        results = search_song_library(SimpleNamespace(config=config), "sneham", limit=100)
        self.assertEqual(25, len(results))

    def test_exact_song_number_ranks_above_larger_number_containing_query(self) -> None:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                rows=[
                    row(
                        True,
                        library_name=static_cell("Malayalam Songs"),
                        songs=static_cell(
                            [
                                "2554 - Senayin adhipan devanil athiyaay",
                                "554 - Devadhi devan nee rajadhirajan (nee ennum)",
                                "1054 - Ethra nallavan enneshu naayakan",
                            ]
                        ),
                    )
                ],
            )
        ]
        results = search_song_library(SimpleNamespace(config=config), "554")
        self.assertEqual("554 - Devadhi devan nee rajadhirajan (nee ennum)", results[0]["name"])
        self.assertGreater(results[0]["score"], results[1]["score"])

    def test_search_uses_dictionary_keys_and_returns_song_uuid(self) -> None:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                columns=[
                    column("library_name", "Library Name"),
                    column("songs", "Songs", "dictionary"),
                ],
                rows=[
                    row(
                        True,
                        library_name=static_cell("Malayalam Songs"),
                        songs=static_cell(
                            {
                                "Daivame Nin Sneham": "song-uuid-1",
                                "Yeshuve Nin Sneham": "song-uuid-2",
                            }
                        ),
                    )
                ],
            )
        ]
        results = search_song_library(SimpleNamespace(config=config), "daivame")
        self.assertEqual("Daivame Nin Sneham", results[0]["name"])
        self.assertEqual("song-uuid-1", results[0]["uuid"])

    def test_empty_search_returns_alphabetized_song_results(self) -> None:
        results = search_song_library(self.context_with_songs(), "")
        self.assertEqual(3, len(results))
        self.assertEqual("Daivame Nin Sneham", results[0]["name"])

    def test_existing_song_array_configuration_migrates_to_dictionary_paths(self) -> None:
        config = AppConfig()
        config.ui.input_lists_initialized = True
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                columns=[column("songs", "Songs", "array_string")],
                rows=[
                    row(
                        True,
                        songs=polled_cell(
                            "v1/library/Malayalam%20Songs",
                            "items[].name",
                            ["Daivame Nin Sneham"],
                        ),
                    )
                ],
            )
        ]
        self.assertTrue(ensure_default_input_lists(config))
        definition = config.ui.input_lists[0]
        self.assertEqual("dictionary", definition.columns[0].data_type)
        cell = definition.rows[0].cells["songs"]
        self.assertEqual("items[].name", cell.json_key_path)
        self.assertEqual("items[].uuid", cell.json_value_path)
        self.assertEqual("", cell.json_path)
        self.assertEqual({"Daivame Nin Sneham": ""}, cell.value)

    def test_existing_english_song_row_repairs_malformed_polling_url(self) -> None:
        config = AppConfig()
        config.ui.input_lists_initialized = True
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                columns=[
                    column("library_name", "Library Name"),
                    column("songs", "Songs", "dictionary"),
                ],
                rows=[
                    row(
                        True,
                        library_name=static_cell("English Songs"),
                        songs=polled_dictionary_cell(
                            "v1/library/English%Songs",
                            "items[].name",
                            "items[].uuid",
                        ),
                    )
                ],
            )
        ]

        self.assertTrue(ensure_default_input_lists(config))
        songs = config.ui.input_lists[0].rows[0].cells["songs"]
        self.assertEqual("v1/library/English%20Songs", songs.url)


class SongLibraryPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_dictionary_polling_zips_key_and_value_json_paths(self) -> None:
        definition = InputListDefinition(
            "song_library",
            "Song Library",
            columns=[column("songs", "Songs", "dictionary")],
            rows=[
                row(
                    True,
                    songs=polled_dictionary_cell(
                        "v1/library/Malayalam%20Songs",
                        "items[].name",
                        "items[].uuid",
                    ),
                )
            ],
        )

        class Client:
            async def get_json(self, _path):
                return {
                    "items": [
                        {"name": "Song One", "uuid": "uuid-1"},
                        {"name": "Song Two", "uuid": "uuid-2"},
                    ]
                }

        context = SimpleNamespace(propresenter=SimpleNamespace(client=Client()))
        self.assertTrue(await poll_input_list_definition(context, definition))
        self.assertEqual(
            {"Song One": "uuid-1", "Song Two": "uuid-2"},
            definition.rows[0].cells["songs"].value,
        )

    async def test_polling_newly_enabled_row_updates_search_without_repolling_other_rows(self) -> None:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                columns=[
                    column("library_name", "Library Name"),
                    column("songs", "Songs", "dictionary"),
                ],
                rows=[
                    row(
                        True,
                        library_name=static_cell("Malayalam Songs"),
                        songs=polled_dictionary_cell(
                            "v1/library/Malayalam%20Songs",
                            "items[].name",
                            "items[].uuid",
                            {"Existing Song": "existing-uuid"},
                        ),
                    ),
                    row(
                        True,
                        library_name=static_cell("English Songs"),
                        songs=polled_dictionary_cell(
                            "v1/library/English%20Songs",
                            "items[].name",
                            "items[].uuid",
                        ),
                    ),
                ],
            )
        ]

        class Client:
            def __init__(self) -> None:
                self.paths = []

            async def get_json(self, path):
                self.paths.append(path)
                return {"items": [{"name": "Amazing Grace", "uuid": "english-uuid"}]}

        class Repository:
            def __init__(self) -> None:
                self.saved = False

            def save_app_config(self, _config) -> None:
                self.saved = True

        client = Client()
        repository = Repository()
        context = SimpleNamespace(
            config=config,
            propresenter=SimpleNamespace(client=client),
            config_repository=repository,
        )

        self.assertTrue(await poll_input_list_row_by_key(context, "song_library", 1))
        self.assertEqual(["library/English%20Songs"], client.paths)
        self.assertTrue(repository.saved)
        results = search_song_library(context, "Amazing Grace")
        self.assertEqual("english-uuid", results[0]["uuid"])
        self.assertEqual("English Songs", results[0]["library"])


if __name__ == "__main__":
    unittest.main()
