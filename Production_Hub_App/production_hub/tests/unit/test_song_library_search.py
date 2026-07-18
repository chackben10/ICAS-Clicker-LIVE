from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace

from production_hub.core.config.input_lists import (
    column,
    ensure_default_input_lists,
    poll_due_input_lists,
    poll_input_list_by_key,
    poll_input_list_definition,
    poll_input_list_row_by_key,
    polled_cell,
    polled_dictionary_cell,
    polled_object_array_cell,
    row,
    song_object_fields,
    static_cell,
)
from production_hub.core.config.models import AppConfig, InputListDefinition, InputListItem, InputListObjectField
from production_hub.core.endpoints.search import normalized_text, phonetic_key, search_song_library


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

    def test_disabling_preserves_cached_objects_and_reenabling_reuses_them(self) -> None:
        cached_songs = [
            {
                "name": "Amazing Grace",
                "uuid": "english-uuid",
                "lyrics": "I once was lost but now am found",
            }
        ]
        definition = InputListDefinition(
            "song_library",
            "Song Library",
            columns=[column("songs", "Songs", "array_object")],
            rows=[row(True, songs=static_cell(cached_songs))],
        )
        context = SimpleNamespace(config=AppConfig())
        context.config.ui.input_lists = [definition]

        self.assertEqual("english-uuid", search_song_library(context, "Amazing Grace")[0]["uuid"])

        definition.rows[0].enabled = False
        restored = InputListDefinition.from_dict(definition.to_dict())
        context.config.ui.input_lists = [restored]
        self.assertEqual(cached_songs, restored.rows[0].cells["songs"].value)
        self.assertEqual([], search_song_library(context, "Amazing Grace"))
        self.assertEqual([], search_song_library(context, "once was lost"))

        restored.rows[0].enabled = True
        self.assertEqual("english-uuid", search_song_library(context, "Amazing Grace")[0]["uuid"])
        self.assertEqual("english-uuid", search_song_library(context, "once was lost")[0]["uuid"])

    def test_disabled_rows_do_not_fall_back_to_legacy_items(self) -> None:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                items=[InputListItem("Legacy Leaked Song", "legacy-uuid", enabled=True)],
                columns=[column("songs", "Songs", "array_object")],
                rows=[
                    row(
                        False,
                        songs=static_cell(
                            [{"name": "Cached Disabled Song", "uuid": "cached-uuid", "lyrics": ""}]
                        ),
                    )
                ],
            )
        ]

        self.assertEqual([], search_song_library(SimpleNamespace(config=config), "Legacy Leaked Song"))

    def test_search_handles_typos_and_phonetic_transliteration(self) -> None:
        results = search_song_library(self.context_with_songs(), "dyvame nin sneham")
        self.assertGreater(len(results), 0)
        self.assertEqual("Daivame Nin Sneham", results[0]["name"])
        self.assertEqual(phonetic_key("daivame"), phonetic_key("dyvame"))

    def test_phonetic_title_search_ignores_leading_song_number(self) -> None:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                rows=[
                    row(
                        True,
                        songs=static_cell(["43 - Daivame Nin Sneham"]),
                    )
                ],
            )
        ]

        results = search_song_library(SimpleNamespace(config=config), "dyvame nin snehem")

        self.assertEqual("43 - Daivame Nin Sneham", results[0]["name"])
        self.assertGreaterEqual(results[0]["score"], 0.9)

    def test_title_phonetics_do_not_drop_a_native_script_query_word(self) -> None:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                rows=[row(True, songs=static_cell(["Amazing Grace"]))],
            )
        ]

        results = search_song_library(SimpleNamespace(config=config), "സന്നിധി grace")

        self.assertEqual([], results)

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

    def test_search_caps_pathological_query_size(self) -> None:
        title = " ".join(["sneham"] * 12)
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                rows=[row(True, songs=static_cell([title]))],
            )
        ]

        results = search_song_library(SimpleNamespace(config=config), "sneham " * 1_000)

        self.assertTrue(results)
        self.assertEqual(title, results[0]["name"])

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
        self.assertNotIn("match_field", results[0])
        self.assertNotIn("lyric_preview", results[0])

    def test_existing_song_array_configuration_migrates_to_object_fields(self) -> None:
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
        self.assertEqual("array_object", definition.columns[0].data_type)
        cell = definition.rows[0].cells["songs"]
        self.assertEqual("items[]", cell.json_path)
        self.assertEqual("", cell.json_key_path)
        self.assertEqual("", cell.json_value_path)
        self.assertEqual(["name", "uuid", "lyrics"], [field.key for field in cell.object_fields])
        self.assertEqual("v1/presentation/{uuid}", cell.object_fields[2].url_template)
        self.assertEqual(
            [{"name": "Daivame Nin Sneham", "uuid": "", "lyrics": ""}],
            cell.value,
        )
        self.assertFalse(ensure_default_input_lists(config))

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
                            {"Amazing Grace": "english-uuid"},
                        ),
                    )
                ],
            )
        ]

        self.assertTrue(ensure_default_input_lists(config))
        songs = config.ui.input_lists[0].rows[0].cells["songs"]
        self.assertEqual("v1/library/English%20Songs", songs.url)
        self.assertEqual(
            [{"name": "Amazing Grace", "uuid": "english-uuid", "lyrics": ""}],
            songs.value,
        )

    def test_existing_custom_object_mapping_is_not_replaced(self) -> None:
        config = AppConfig()
        config.ui.input_lists_initialized = True
        custom_fields = [
            InputListObjectField("name", json_path="display_name"),
            InputListObjectField("uuid", json_path="identifier"),
            InputListObjectField(
                "lyrics",
                source="request",
                json_path="custom.lines[]",
                url_template="v1/custom/{uuid}",
                result_mode="join",
            ),
            InputListObjectField("category", json_path="category"),
        ]
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                columns=[column("songs", "Songs", "array_object")],
                rows=[
                    row(
                        True,
                        songs=polled_object_array_cell(
                            "v1/custom-library",
                            "records[]",
                            custom_fields,
                            [{"name": "Song", "uuid": "uuid", "lyrics": "Words", "category": "Custom"}],
                        ),
                    )
                ],
            )
        ]

        self.assertFalse(ensure_default_input_lists(config))
        songs = config.ui.input_lists[0].rows[0].cells["songs"]
        self.assertEqual("records[]", songs.json_path)
        self.assertEqual("v1/custom/{uuid}", songs.object_fields[2].url_template)
        self.assertEqual("category", songs.object_fields[3].key)

    def test_object_song_searches_title_and_lyrics_without_returning_lyrics(self) -> None:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                columns=[column("songs", "Songs", "array_object")],
                rows=[
                    row(
                        True,
                        library_name=static_cell("Malayalam Songs"),
                        songs=static_cell(
                            [
                                {
                                    "name": "Aanandam",
                                    "UUID": "song-uuid-1",
                                    "lyrics": "അവൻ സന്നിധി മതി എനിക്ക്",
                                },
                                {
                                    "name": "Different Title",
                                    "uuid": "song-uuid-2",
                                    "lyrics": "grace beyond measure",
                                },
                            ]
                        ),
                    )
                ],
            )
        ]
        context = SimpleNamespace(config=config)

        malayalam = search_song_library(context, "സന്നിധി")
        self.assertEqual("Aanandam", malayalam[0]["name"])
        self.assertEqual("song-uuid-1", malayalam[0]["uuid"])
        self.assertNotIn("lyrics", malayalam[0])
        self.assertEqual("lyrics", malayalam[0]["match_field"])
        self.assertIn("സന്നിധി", malayalam[0]["lyric_preview"])
        exact_lyrics = search_song_library(context, "beyond measure")[0]
        typo_lyrics = search_song_library(context, "mesure")[0]
        short_lyrics = search_song_library(context, "gr")[0]
        self.assertEqual("Different Title", exact_lyrics["name"])
        self.assertEqual("Different Title", typo_lyrics["name"])
        self.assertEqual("Different Title", short_lyrics["name"])
        self.assertIn("grace beyond measure", typo_lyrics["lyric_preview"])
        self.assertNotIn("lyrics", typo_lyrics)
        self.assertTrue(normalized_text("സന്നിധി"))

    def test_partial_lyric_word_stays_visible_while_searching_as_you_type(self) -> None:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                rows=[
                    row(
                        True,
                        songs=static_cell(
                            [
                                {
                                    "name": "Unrelated Presentation Title",
                                    "uuid": "partial-lyrics",
                                    "lyrics": "grace beyond measure sneham",
                                }
                            ]
                        ),
                    )
                ],
            )
        ]
        context = SimpleNamespace(config=config)

        for query in ("g", "gr", "gra", "grac", "grace", "s", "sn", "sne", "sneh", "sneha", "sneham", "mea", "meas", "measu", "measure"):
            with self.subTest(query=query):
                self.assertIn(
                    "partial-lyrics",
                    [result["uuid"] for result in search_song_library(context, query)],
                )

    def test_phonetic_lyric_search_returns_original_words_with_context(self) -> None:
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
                                {
                                    "name": "Unrelated Presentation Title",
                                    "uuid": "phonetic-lyrics",
                                    "lyrics": (
                                        "These are the words before. Daivame nin sneham ennum enne "
                                        "vazhi nadathum. These are the words after."
                                    ),
                                }
                            ]
                        ),
                    )
                ],
            )
        ]

        result = search_song_library(SimpleNamespace(config=config), "dyvame nin snehem")[0]

        self.assertEqual("phonetic-lyrics", result["uuid"])
        self.assertEqual("lyrics", result["match_field"])
        self.assertIn("Daivame nin sneham", result["lyric_preview"])
        self.assertIn("words before", result["lyric_preview"])
        self.assertIn("words after", result["lyric_preview"])
        self.assertNotIn("lyrics", result)

    def test_lyric_typo_match_is_stable_when_unrelated_songs_are_added(self) -> None:
        songs = [{"name": "First Song", "uuid": "intended", "lyrics": "Yeshuvin sneham ennum"}]
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                rows=[row(True, songs=static_cell(songs))],
            )
        ]
        context = SimpleNamespace(config=config)

        self.assertEqual("intended", search_song_library(context, "snekam")[0]["uuid"])

        songs.extend(
            [
                {"name": "Distractor One", "uuid": "one", "lyrics": "nekxxx"},
                {"name": "Distractor Two", "uuid": "two", "lyrics": "ekaxxx"},
                {"name": "Distractor Three", "uuid": "three", "lyrics": "kamxxx"},
            ]
        )
        self.assertEqual("intended", search_song_library(context, "snekam")[0]["uuid"])

    def test_short_exact_lyric_phrases_work_without_partial_missing_word_matches(self) -> None:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                rows=[
                    row(
                        True,
                        songs=static_cell(
                            [
                                {
                                    "name": "Short Words",
                                    "uuid": "short-words",
                                    "lyrics": "I am here and he is there with amazing grace",
                                }
                            ]
                        ),
                    )
                ],
            )
        ]
        context = SimpleNamespace(config=config)

        self.assertEqual("short-words", search_song_library(context, "i am")[0]["uuid"])
        self.assertEqual("short-words", search_song_library(context, "he is")[0]["uuid"])
        self.assertEqual([], search_song_library(context, "totallymissing grace"))
        self.assertEqual([], search_song_library(context, "സന്നിധി grace"))

    def test_lyric_preview_prefers_the_exact_word_over_an_earlier_phonetic_word(self) -> None:
        filler = " ".join(f"filler{index}" for index in range(30))
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                rows=[
                    row(
                        True,
                        songs=static_cell(
                            [
                                {
                                    "name": "Preview Accuracy",
                                    "uuid": "preview-accuracy",
                                    "lyrics": f"early future {filler} later exact father context",
                                }
                            ]
                        ),
                    )
                ],
            )
        ]

        result = search_song_library(SimpleNamespace(config=config), "father")[0]

        self.assertIn("father", result["lyric_preview"])
        self.assertNotIn("early future", result["lyric_preview"])

    def test_nonsense_query_does_not_create_short_phonetic_matches(self) -> None:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                rows=[
                    row(
                        True,
                        songs=static_cell(
                            [
                                {"name": "Rakshaka", "uuid": "rakshaka", "lyrics": "rakshaka enikku"},
                                {"name": "At the Cross", "uuid": "cross", "lyrics": ""},
                                {"name": "499 - Krushil kandu njan", "uuid": "numbered", "lyrics": ""},
                            ]
                        ),
                    )
                ],
            )
        ]

        self.assertEqual([], search_song_library(SimpleNamespace(config=config), "xyzzyq"))

    def test_lyric_preview_is_bounded_around_the_matching_phrase(self) -> None:
        before = " ".join(f"before{index}" for index in range(30))
        after = " ".join(f"after{index}" for index in range(30))
        full_lyrics = f"{before} Daivame nin sneham {after}"
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                rows=[
                    row(
                        True,
                        songs=static_cell(
                            [{"name": "Context Song", "uuid": "context", "lyrics": full_lyrics}]
                        ),
                    )
                ],
            )
        ]

        result = search_song_library(SimpleNamespace(config=config), "dyvame nin snehem")[0]

        self.assertLessEqual(len(result["lyric_preview"]), 180)
        self.assertTrue(result["lyric_preview"].startswith("… "))
        self.assertTrue(result["lyric_preview"].endswith(" …"))
        self.assertIn("Daivame nin sneham", result["lyric_preview"])
        self.assertNotEqual(full_lyrics, result["lyric_preview"])

    def test_title_match_ranks_above_lyrics_only_match(self) -> None:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                rows=[
                    row(
                        True,
                        songs=static_cell(
                            [
                                {"name": "Amazing Grace", "uuid": "title", "lyrics": ""},
                                {"name": "Another Song", "uuid": "lyrics", "lyrics": "Amazing Grace"},
                            ]
                        ),
                    )
                ],
            )
        ]
        results = search_song_library(SimpleNamespace(config=config), "Amazing Grace")
        self.assertEqual("title", results[0]["uuid"])
        self.assertGreater(results[0]["score"], results[1]["score"])
        self.assertEqual("title", results[0]["match_field"])
        self.assertNotIn("lyric_preview", results[0])
        self.assertEqual("lyrics", results[1]["match_field"])
        self.assertIn("Amazing Grace", results[1]["lyric_preview"])

    def test_numeric_search_does_not_match_verse_numbers_in_lyrics(self) -> None:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                rows=[
                    row(
                        True,
                        songs=static_cell(
                            [
                                {"name": "554 - Correct Song", "uuid": "correct", "lyrics": ""},
                                {"name": "Unrelated Song", "uuid": "wrong", "lyrics": "Verse 554"},
                            ]
                        ),
                    )
                ],
            )
        ]
        results = search_song_library(SimpleNamespace(config=config), "554")
        self.assertEqual("correct", results[0]["uuid"])
        self.assertNotIn("wrong", [item["uuid"] for item in results])

    def test_search_index_rebuilds_when_lyrics_change(self) -> None:
        config = AppConfig()
        songs = [{"name": "Indexed Song", "uuid": "indexed", "lyrics": "first phrase"}]
        config.ui.input_lists = [
            InputListDefinition("song_library", "Song Library", rows=[row(True, songs=static_cell(songs))])
        ]
        context = SimpleNamespace(config=config)
        self.assertEqual("indexed", search_song_library(context, "first phrase")[0]["uuid"])
        config.ui.input_lists[0].rows[0].cells["songs"].value[0]["lyrics"] = "second phrase"
        self.assertEqual("indexed", search_song_library(context, "second phrase")[0]["uuid"])


class SongLibraryPollingTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def disabled_cached_song_config() -> AppConfig:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "song_library",
                "Song Library",
                polling_rate_seconds=60,
                columns=[column("songs", "Songs", "array_object")],
                rows=[
                    row(
                        False,
                        songs=polled_object_array_cell(
                            "v1/library/English%20Songs",
                            "items[]",
                            song_object_fields(),
                            [
                                {
                                    "name": "Amazing Grace",
                                    "uuid": "english-uuid",
                                    "lyrics": "cached lyrics",
                                }
                            ],
                        ),
                    )
                ],
            )
        ]
        return config

    async def test_manual_poll_skips_disabled_song_row_without_clearing_cache(self) -> None:
        config = self.disabled_cached_song_config()

        class Client:
            def __init__(self) -> None:
                self.paths: list[str] = []

            async def get_json(self, path):
                self.paths.append(path)
                raise AssertionError("A disabled song row must not be polled")

        class Repository:
            def __init__(self) -> None:
                self.save_count = 0

            def save_app_config(self, _config) -> None:
                self.save_count += 1

        client = Client()
        repository = Repository()
        context = SimpleNamespace(
            config=config,
            propresenter=SimpleNamespace(client=client),
            config_repository=repository,
        )
        cached_songs = config.ui.input_lists[0].rows[0].cells["songs"].value

        self.assertFalse(await poll_input_list_by_key(context, "song_library"))
        self.assertFalse(await poll_input_list_row_by_key(context, "song_library", 0))
        self.assertEqual([], client.paths)
        self.assertEqual(0, repository.save_count)
        self.assertEqual(cached_songs, config.ui.input_lists[0].rows[0].cells["songs"].value)

    async def test_scheduler_does_not_start_list_when_all_polled_rows_are_disabled(self) -> None:
        config = self.disabled_cached_song_config()

        class Client:
            def __init__(self) -> None:
                self.paths: list[str] = []

            async def get_json(self, path):
                self.paths.append(path)
                raise AssertionError("A disabled song row must not be polled")

        class Repository:
            def save_app_config(self, _config) -> None:
                raise AssertionError("Disabled cached data must not be rewritten")

        class Logger:
            def warning(self, *_args, **_kwargs) -> None:
                pass

        client = Client()
        context = SimpleNamespace(
            config=config,
            propresenter=SimpleNamespace(client=client),
            config_repository=Repository(),
            logger=Logger(),
        )
        next_due: dict[str, float] = {}
        running: dict[str, asyncio.Task[None]] = {}

        await poll_due_input_lists(context, next_due, running)
        scheduled_keys = list(running)
        if running:
            await asyncio.gather(*running.values())

        self.assertEqual([], scheduled_keys)
        self.assertEqual({}, next_due)
        self.assertEqual([], client.paths)

    async def test_disabling_row_during_poll_preserves_its_existing_cache(self) -> None:
        config = self.disabled_cached_song_config()
        live_row = config.ui.input_lists[0].rows[0]
        live_row.enabled = True
        cached_songs = list(live_row.cells["songs"].value)
        library_started = asyncio.Event()
        release_library = asyncio.Event()

        class Client:
            async def get_json(self, path):
                if path.startswith("library/"):
                    library_started.set()
                    await release_library.wait()
                    return {"items": [{"name": "Replacement Song", "uuid": "replacement-uuid"}]}
                return {"presentation": {"groups": [{"slides": [{"text": "replacement lyrics"}]}]}}

        class Repository:
            def save_app_config(self, _config) -> None:
                pass

        context = SimpleNamespace(
            config=config,
            propresenter=SimpleNamespace(client=Client()),
            config_repository=Repository(),
        )
        task = asyncio.create_task(poll_input_list_row_by_key(context, "song_library", 0))
        await asyncio.wait_for(library_started.wait(), timeout=1)

        config.ui.input_lists[0].rows[0].enabled = False
        release_library.set()
        await task

        saved_row = config.ui.input_lists[0].rows[0]
        self.assertFalse(saved_row.enabled)
        self.assertEqual(cached_songs, saved_row.cells["songs"].value)

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

    async def test_object_polling_enriches_nested_slide_text_with_bounded_concurrency(self) -> None:
        fields = song_object_fields()
        fields[2].refresh_seconds = 0
        definition = InputListDefinition(
            "song_library",
            "Song Library",
            columns=[column("songs", "Songs", "array_object")],
            rows=[
                row(
                    True,
                    songs=polled_object_array_cell(
                        "v1/library/Malayalam%20Songs",
                        "items[]",
                        fields,
                        concurrency=2,
                    ),
                )
            ],
        )

        class Client:
            def __init__(self) -> None:
                self.paths: list[str] = []
                self.active = 0
                self.max_active = 0

            async def get_json(self, path):
                self.paths.append(path)
                if path.startswith("library/"):
                    return {
                        "items": [
                            {"name": "Song One", "uuid": "uuid-1"},
                            {"name": "Song Two", "uuid": "uuid-2"},
                            {"name": "Song Three", "uuid": "uuid-3"},
                        ]
                    }
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(0.01)
                self.active -= 1
                uuid = path.rsplit("/", 1)[-1]
                return {
                    "presentation": {
                        "groups": [
                            {"slides": [{"text": f"first  line for {uuid}\r\n"}]},
                            {"slides": [{"text": "second line"}]},
                        ]
                    }
                }

        client = Client()
        context = SimpleNamespace(propresenter=SimpleNamespace(client=client))
        self.assertTrue(await poll_input_list_definition(context, definition))
        songs = definition.rows[0].cells["songs"].value
        self.assertEqual(["name", "uuid", "lyrics"], list(songs[0]))
        self.assertEqual("first line for uuid-1 second line", songs[0]["lyrics"])
        self.assertEqual(2, client.max_active)
        self.assertEqual(
            {"presentation/uuid-1", "presentation/uuid-2", "presentation/uuid-3"},
            {path for path in client.paths if path.startswith("presentation/")},
        )

    async def test_base_song_objects_are_saved_before_slow_lyrics_finish(self) -> None:
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
        presentation_started = asyncio.Event()
        release_presentation = asyncio.Event()

        class Client:
            async def get_json(self, path):
                if path.startswith("library/"):
                    return {"items": [{"name": "Amazing Grace", "uuid": "english-uuid"}]}
                presentation_started.set()
                await release_presentation.wait()
                return {"presentation": {"groups": [{"slides": [{"text": "finished lyrics"}]}]}}

        class Repository:
            def __init__(self) -> None:
                self.save_count = 0

            def save_app_config(self, _config) -> None:
                self.save_count += 1

        repository = Repository()
        context = SimpleNamespace(
            config=config,
            propresenter=SimpleNamespace(client=Client()),
            config_repository=repository,
        )
        task = asyncio.create_task(poll_input_list_row_by_key(context, "song_library", 0))
        await asyncio.wait_for(presentation_started.wait(), timeout=1)

        saved_songs = context.config.ui.input_lists[0].rows[0].cells["songs"].value
        self.assertEqual([{"name": "Amazing Grace", "uuid": "english-uuid", "lyrics": ""}], saved_songs)
        self.assertGreaterEqual(repository.save_count, 1)
        self.assertEqual("english-uuid", search_song_library(context, "Amazing Grace")[0]["uuid"])

        release_presentation.set()
        self.assertTrue(await task)
        self.assertEqual(
            "finished lyrics",
            context.config.ui.input_lists[0].rows[0].cells["songs"].value[0]["lyrics"],
        )

    async def test_object_polling_preserves_previous_lyrics_on_partial_failure(self) -> None:
        fields = song_object_fields()
        fields[2].refresh_seconds = 0
        definition = InputListDefinition(
            "song_library",
            "Song Library",
            columns=[column("songs", "Songs", "array_object")],
            rows=[
                row(
                    True,
                    songs=polled_object_array_cell(
                        "v1/library/Malayalam%20Songs",
                        "items[]",
                        fields,
                        [
                            {"name": "Song One", "uuid": "uuid-1", "lyrics": "old one"},
                            {"name": "Song Two", "uuid": "uuid-2", "lyrics": "old two"},
                        ],
                    ),
                )
            ],
        )

        class Client:
            async def get_json(self, path):
                if path.startswith("library/"):
                    return {
                        "items": [
                            {"name": "Song One", "uuid": "uuid-1"},
                            {"name": "Song Two", "uuid": "uuid-2"},
                        ]
                    }
                if path.endswith("uuid-1"):
                    raise RuntimeError("presentation unavailable")
                return {"presentation": {"groups": [{"slides": [{"text": "new two"}]}]}}

        class Logger:
            def __init__(self) -> None:
                self.warnings = []

            def warning(self, *args, **kwargs) -> None:
                self.warnings.append((args, kwargs))

        logger = Logger()
        context = SimpleNamespace(propresenter=SimpleNamespace(client=Client()), logger=logger)
        self.assertTrue(await poll_input_list_definition(context, definition))
        songs = definition.rows[0].cells["songs"].value
        self.assertEqual("old one", songs[0]["lyrics"])
        self.assertEqual("new two", songs[1]["lyrics"])
        self.assertEqual(1, len(logger.warnings))

    async def test_object_polling_keeps_valid_songs_when_one_item_has_no_uuid(self) -> None:
        fields = song_object_fields()
        fields[2].refresh_seconds = 0
        definition = InputListDefinition(
            "song_library",
            "Song Library",
            columns=[column("songs", "Songs", "array_object")],
            rows=[
                row(
                    True,
                    songs=polled_object_array_cell("v1/library/Test", "items[]", fields),
                )
            ],
        )

        class Client:
            async def get_json(self, path):
                if path.startswith("library/"):
                    return {
                        "items": [
                            {"name": "Valid Song", "uuid": "valid-uuid"},
                            {"name": "Missing UUID"},
                        ]
                    }
                return {"presentation": {"groups": [{"slides": [{"text": "valid lyrics"}]}]}}

        class Logger:
            def __init__(self) -> None:
                self.warnings = []

            def warning(self, *args, **kwargs) -> None:
                self.warnings.append((args, kwargs))

        logger = Logger()
        context = SimpleNamespace(propresenter=SimpleNamespace(client=Client()), logger=logger)
        self.assertTrue(await poll_input_list_definition(context, definition))
        songs = definition.rows[0].cells["songs"].value
        self.assertEqual(2, len(songs))
        self.assertEqual("valid lyrics", songs[0]["lyrics"])
        self.assertEqual("", songs[1]["lyrics"])
        self.assertEqual(1, len(logger.warnings))

    async def test_empty_enrichment_response_does_not_erase_cached_lyrics(self) -> None:
        fields = song_object_fields()
        fields[2].refresh_seconds = 0
        definition = InputListDefinition(
            "song_library",
            "Song Library",
            columns=[column("songs", "Songs", "array_object")],
            rows=[
                row(
                    True,
                    songs=polled_object_array_cell(
                        "v1/library/Test",
                        "items[]",
                        fields,
                        [{"name": "Song", "uuid": "uuid", "lyrics": "keep me"}],
                    ),
                )
            ],
        )

        class Client:
            async def get_json(self, path):
                if path.startswith("library/"):
                    return {"items": [{"name": "Song", "uuid": "uuid"}]}
                return {}

        class Logger:
            def warning(self, *_args, **_kwargs) -> None:
                pass

        context = SimpleNamespace(propresenter=SimpleNamespace(client=Client()), logger=Logger())
        self.assertFalse(await poll_input_list_definition(context, definition))
        self.assertEqual("keep me", definition.rows[0].cells["songs"].value[0]["lyrics"])

    async def test_object_polling_reuses_enrichment_until_refresh_is_due(self) -> None:
        fields = song_object_fields()
        definition = InputListDefinition(
            "song_library",
            "Song Library",
            columns=[column("songs", "Songs", "array_object")],
            rows=[
                row(
                    True,
                    songs=polled_object_array_cell(
                        "v1/library/Malayalam%20Songs",
                        "items[]",
                        fields,
                        [{"name": "Song One", "uuid": "uuid-1", "lyrics": "cached lyrics"}],
                    ),
                )
            ],
        )
        definition.rows[0].cells["songs"].object_enrichment_last_polled = {"lyrics": time.time()}

        class Client:
            def __init__(self) -> None:
                self.paths = []

            async def get_json(self, path):
                self.paths.append(path)
                return {"items": [{"name": "Song One", "uuid": "uuid-1"}]}

        client = Client()
        context = SimpleNamespace(propresenter=SimpleNamespace(client=client))
        self.assertFalse(await poll_input_list_definition(context, definition))
        self.assertEqual(["library/Malayalam%20Songs"], client.paths)

    def test_object_field_configuration_round_trips(self) -> None:
        cell = polled_object_array_cell(
            "v1/items",
            "items[]",
            [
                InputListObjectField("name", json_path="name"),
                InputListObjectField(
                    "details",
                    source="request",
                    json_path="groups[].values[]",
                    url_template="v1/details/{uuid}",
                    result_mode="join",
                    normalize_whitespace=True,
                    refresh_seconds=120,
                ),
            ],
            concurrency=7,
        )
        definition = InputListDefinition(
            "objects",
            "Objects",
            columns=[column("items", "Items", "array_object")],
            rows=[row(True, items=cell)],
        )
        restored = InputListDefinition.from_dict(definition.to_dict())
        restored_cell = restored.rows[0].cells["items"]
        self.assertEqual(7, restored_cell.object_concurrency)
        self.assertEqual("details", restored_cell.object_fields[1].key)
        self.assertEqual(120, restored_cell.object_fields[1].refresh_seconds)

    async def test_scheduled_poll_runs_as_a_non_blocking_background_task(self) -> None:
        config = AppConfig()
        config.ui.input_lists = [
            InputListDefinition(
                "background_list",
                "Background List",
                polling_rate_seconds=60,
                columns=[column("items", "Items", "array_string")],
                rows=[row(True, items=polled_cell("v1/items", "items[]"))],
            )
        ]
        release = asyncio.Event()

        class Client:
            async def get_json(self, _path):
                await release.wait()
                return {"items": ["ready"]}

        class Repository:
            def __init__(self) -> None:
                self.saved = False

            def save_app_config(self, _config) -> None:
                self.saved = True

        class Logger:
            def warning(self, *_args, **_kwargs) -> None:
                pass

        repository = Repository()
        context = SimpleNamespace(
            config=config,
            propresenter=SimpleNamespace(client=Client()),
            config_repository=repository,
            logger=Logger(),
        )
        running: dict[str, asyncio.Task[None]] = {}
        await poll_due_input_lists(context, {}, running)
        self.assertIn("background_list", running)
        self.assertFalse(running["background_list"].done())
        release.set()
        await running["background_list"]
        self.assertTrue(repository.saved)


if __name__ == "__main__":
    unittest.main()
