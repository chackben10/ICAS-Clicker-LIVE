from __future__ import annotations

import unittest
from types import SimpleNamespace

from production_hub.core.config.input_lists import row, static_cell
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


if __name__ == "__main__":
    unittest.main()
