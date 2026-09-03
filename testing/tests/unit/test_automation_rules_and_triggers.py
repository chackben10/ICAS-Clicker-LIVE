from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from production_hub.app.bootstrap import build_context, ensure_builtin_automation_steps
from production_hub.app.lifecycle import _run_due_automations, register_generic_automation_handler
from production_hub.core.automation.engine import AutomationEngine
from production_hub.core.automation.evaluator import evaluate_rule_tree
from production_hub.core.automation.models import AutomationDefinition
from production_hub.core.automation.triggers import AutomationTriggerMonitor
from production_hub.core.config.defaults import (
    AUTO_SHOW_BIBLE_AUTOMATION_KEY,
    PRESENTATION_FOCUS_AUTOMATION_KEY,
    build_default_automations,
)
from production_hub.core.endpoints.models import ActionDefinition


class NestedRuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_and_or_and_not_rules(self) -> None:
        rules = {
            "operator": "and",
            "children": [
                {
                    "condition_type": "event.value",
                    "params": {"name": "slide_index", "operator": "greater than", "value": "0"},
                },
                {
                    "operator": "or",
                    "children": [
                        {
                            "condition_type": "event.value",
                            "params": {"name": "look", "operator": "equals", "value": "Bible"},
                        },
                        {
                            "condition_type": "event.value",
                            "params": {"name": "look", "operator": "equals", "value": "Lyrics"},
                        },
                    ],
                },
                {
                    "condition_type": "event.value",
                    "params": {"name": "blocked", "operator": "is true", "value": ""},
                    "negate": True,
                },
            ],
        }
        ok, _message = await evaluate_rule_tree(object(), rules, {"slide_index": 2, "look": "Lyrics", "blocked": False})
        self.assertTrue(ok)
        blocked, _message = await evaluate_rule_tree(object(), rules, {"slide_index": 2, "look": "Lyrics", "blocked": True})
        self.assertFalse(blocked)


class SlideTriggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_slide_trigger_primes_then_fires_for_index_change_once(self) -> None:
        snapshots = iter(
            [
                ("presentation-a", 0),
                ("presentation-a", 1),
                ("presentation-a", 1),
            ]
        )

        class ProPresenter:
            async def slide_index(self):
                uuid, index = next(snapshots)
                return {
                    "presentation_index": {
                        "index": index,
                        "presentation_id": {"uuid": uuid, "name": "Service"},
                        "total_cues": 3,
                        "remaining_cues": 2,
                    }
                }

        context = SimpleNamespace(
            propresenter=ProPresenter(),
            config=SimpleNamespace(
                integrations=SimpleNamespace(
                    propresenter=SimpleNamespace(polling_interval_seconds=0.1),
                )
            ),
        )
        definition = AutomationDefinition(
            key="auto-show",
            name="Auto Show",
            trigger="propresenter.slide_changed",
            interval_seconds=0.1,
            debounce_seconds=0,
        )
        monitor = AutomationTriggerMonitor(context)
        self.assertEqual(await monitor.due(definition, now=0.0), (False, {}))
        due, event = await monitor.due(definition, now=0.11)
        self.assertTrue(due)
        self.assertEqual(event["slide_index"], 1)
        self.assertEqual(event["presentation_uuid"], "presentation-a")
        self.assertEqual(await monitor.due(definition, now=0.22), (False, {}))

    async def test_bible_presentation_event_runs_auto_show_actions(self) -> None:
        bible_presentation = {
            "presentation": {
                "id": None,
                "groups": [{"uuid": "bible-group", "name": "John 3:1-21"}],
            }
        }
        regular_status = {
            "current": {"uuid": "service-cue", "text": "Sunday Service"}
        }
        bible_status = {
            "current": {"uuid": "bible-cue-16", "text": "John 3:16\rFor God so loved the world"}
        }

        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            context.runtime_state_repo.update(
                lambda state: setattr(state, "auto_show_enabled", True)
            )
            context.propresenter.slide_status = AsyncMock(
                side_effect=[regular_status, bible_status, bible_status]
            )
            context.propresenter.active_presentation = AsyncMock(
                return_value=bible_presentation
            )
            context.propresenter.clear_announcements = AsyncMock(return_value=True)
            context.obs.set_scene = AsyncMock(return_value=True)

            definition = context.automation_engine.definitions[AUTO_SHOW_BIBLE_AUTOMATION_KEY]
            register_generic_automation_handler(context, definition)
            monitor = AutomationTriggerMonitor(context)

            self.assertEqual(await monitor.due(definition, now=0.0), (False, {}))
            self.assertEqual(await monitor.due(definition, now=0.25), (False, {}))
            due, event = await monitor.due(definition, now=0.5)
            self.assertTrue(due)
            self.assertEqual(event["slide_uuid"], "bible-cue-16")
            self.assertTrue(event["slide_text"].startswith("John 3:16"))

            state = await context.automation_engine.run_once(definition.key, event)

            self.assertEqual(state.last_condition_result.split(" [", 1)[0], "ALL")
            self.assertEqual(state.last_action_result, "actions_complete")
            context.propresenter.clear_announcements.assert_awaited_once_with()
            context.obs.set_scene.assert_awaited_once_with(
                "ProPresenter Input",
                use_policy=True,
            )

    async def test_consecutive_bible_verses_with_reused_ids_each_auto_show(self) -> None:
        bible_presentation = {
            "presentation": {
                # This matches ProPresenter's actual dynamic Bible payload:
                # no presentation id, one range group, and a fixed slide list.
                "id": None,
                "groups": [
                    {
                        "uuid": "reused-bible-group",
                        "name": "John 3:1-21",
                        "slides": [
                            {"text": "John 3:16\rFor God so loved the world"},
                            {"text": "John 3:17\rFor God did not send his Son"},
                        ],
                    }
                ],
            }
        }
        regular_status = {"current": {"uuid": "service-cue", "text": "Service"}}
        verse_one_status = {
            "current": {"uuid": "bible-cue-16", "text": "John 3:16\rFor God so loved the world"}
        }
        verse_two_status = {
            "current": {"uuid": "bible-cue-17", "text": "John 3:17\rFor God did not send his Son"}
        }

        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            context.runtime_state_repo.update(
                lambda state: setattr(state, "auto_show_enabled", True)
            )
            context.propresenter.slide_status = AsyncMock(
                side_effect=[
                    regular_status,
                    verse_one_status,
                    verse_one_status,
                    verse_two_status,
                    verse_two_status,
                ]
            )
            context.propresenter.active_presentation = AsyncMock(
                return_value=bible_presentation
            )
            context.propresenter.clear_announcements = AsyncMock(return_value=True)
            context.obs.set_scene = AsyncMock(return_value=True)

            definition = context.automation_engine.definitions[AUTO_SHOW_BIBLE_AUTOMATION_KEY]
            register_generic_automation_handler(context, definition)
            monitor = AutomationTriggerMonitor(context)

            self.assertEqual(await monitor.due(definition, now=0.0), (False, {}))
            self.assertEqual(await monitor.due(definition, now=0.25), (False, {}))
            first_due, first_event = await monitor.due(definition, now=0.5)
            self.assertTrue(first_due)
            await context.automation_engine.run_once(definition.key, first_event)

            self.assertEqual(await monitor.due(definition, now=0.75), (False, {}))
            second_due, second_event = await monitor.due(definition, now=1.0)
            self.assertTrue(second_due)
            await context.automation_engine.run_once(definition.key, second_event)

            self.assertEqual(2, context.propresenter.clear_announcements.await_count)
            self.assertEqual(2, context.obs.set_scene.await_count)
            self.assertEqual(2, context.propresenter.active_presentation.await_count)

    async def test_matching_trigger_snapshots_are_shared_per_scheduler_cycle(self) -> None:
        context = SimpleNamespace(
            propresenter=SimpleNamespace(
                slide_index=AsyncMock(
                    side_effect=[
                        {
                            "presentation_index": {
                                "index": 0,
                                "presentation_id": {"uuid": "service", "name": "Service"},
                            }
                        },
                        {
                            "presentation_index": {
                                "index": 1,
                                "presentation_id": {"uuid": "service", "name": "Service"},
                            }
                        },
                    ]
                )
            ),
            config=SimpleNamespace(
                integrations=SimpleNamespace(
                    propresenter=SimpleNamespace(polling_interval_seconds=0.75),
                )
            ),
        )
        definitions = [
            AutomationDefinition(
                key=key,
                name=key,
                trigger="propresenter.slide_changed",
                interval_seconds=0.25,
            )
            for key in ("watcher-a", "watcher-b")
        ]
        monitor = AutomationTriggerMonitor(context)

        for definition in definitions:
            self.assertEqual(await monitor.due(definition, now=0.0), (False, {}))
        results = [await monitor.due(definition, now=0.25) for definition in definitions]

        self.assertEqual([True, True], [due for due, _event in results])
        self.assertEqual(2, context.propresenter.slide_index.await_count)

    async def test_rendered_cue_trigger_uses_live_bible_status_uuid(self) -> None:
        context = SimpleNamespace(
            propresenter=SimpleNamespace(
                slide_status=AsyncMock(
                    side_effect=[
                        {
                            "current": {
                                "uuid": "79AC38BD-2480-4CA4-920E-7475643B2534",
                                "text": "2 Chronicles 20:4\rThe people of Judah came together",
                            }
                        },
                        {
                            "current": {
                                "uuid": "F63D58E8-B5B1-4749-BA21-EB3C44AB04CC",
                                "text": "2 Chronicles 20:5\rThen Jehoshaphat stood up",
                            }
                        },
                        {
                            "current": {
                                "uuid": "F63D58E8-B5B1-4749-BA21-EB3C44AB04CC",
                                "text": "2 Chronicles 20:5\rThen Jehoshaphat stood up",
                            }
                        },
                    ]
                )
            ),
            config=SimpleNamespace(
                integrations=SimpleNamespace(
                    propresenter=SimpleNamespace(polling_interval_seconds=0.75),
                )
            ),
        )
        definition = AutomationDefinition(
            key="bible-cue",
            name="Bible Cue",
            trigger="propresenter.cue_changed",
            interval_seconds=0.25,
            debounce_seconds=0.1,
        )
        monitor = AutomationTriggerMonitor(context)

        self.assertEqual(await monitor.due(definition, now=0.0), (False, {}))
        self.assertEqual(await monitor.due(definition, now=0.25), (False, {}))
        due, event = await monitor.due(definition, now=0.5)

        self.assertTrue(due)
        self.assertEqual(
            "F63D58E8-B5B1-4749-BA21-EB3C44AB04CC",
            event["slide_uuid"],
        )
        self.assertTrue(event["slide_text"].startswith("2 Chronicles 20:5"))

    async def test_auto_show_off_skips_its_propresenter_poll(self) -> None:
        definition = AutomationDefinition(
            key="auto_show_slides",
            name="Auto Show Slides",
            trigger="propresenter.slide_changed",
            interval_seconds=0.25,
        )
        propresenter = SimpleNamespace(slide_index=AsyncMock())
        context = SimpleNamespace(
            automation_engine=AutomationEngine([definition]),
            runtime_state_repo=SimpleNamespace(auto_show_enabled=lambda: False),
            propresenter=propresenter,
            config=SimpleNamespace(
                integrations=SimpleNamespace(
                    propresenter=SimpleNamespace(polling_interval_seconds=0.75),
                )
            ),
        )

        await _run_due_automations(context, AutomationTriggerMonitor(context))

        propresenter.slide_index.assert_not_awaited()

    async def test_presentation_macro_default_waits_ten_seconds_for_a_stable_slide(self) -> None:
        snapshots = iter(
            [
                ("presentation-a", 0),
                ("presentation-a", 1),
                ("presentation-a", 1),
                ("presentation-a", 1),
            ]
        )

        class ProPresenter:
            async def slide_index(self):
                uuid, index = next(snapshots)
                return {
                    "presentation_index": {
                        "index": index,
                        "presentation_id": {"uuid": uuid, "name": "Service"},
                    }
                }

        context = SimpleNamespace(
            propresenter=ProPresenter(),
            config=SimpleNamespace(
                integrations=SimpleNamespace(
                    propresenter=SimpleNamespace(polling_interval_seconds=0.1),
                )
            ),
        )
        definition = next(
            item
            for item in build_default_automations()
            if item.key == "presentation_macro_after_presentation_look"
        )
        monitor = AutomationTriggerMonitor(context)

        self.assertEqual(await monitor.due(definition, now=0.0), (False, {}))
        self.assertEqual(await monitor.due(definition, now=0.5), (False, {}))
        self.assertEqual(await monitor.due(definition, now=10.49), (False, {}))
        due, event = await monitor.due(definition, now=11.0)
        self.assertTrue(due)
        self.assertEqual(event["slide_index"], 1)

    def test_default_automations_have_no_connection_watchdog(self) -> None:
        defaults = build_default_automations()
        self.assertNotIn("obs_connection_watchdog", {item.key for item in defaults})
        auto_show = next(item for item in defaults if item.key == "auto_show_slides")
        self.assertEqual(auto_show.trigger, "propresenter.slide_changed")
        self.assertEqual([action.action_type for action in auto_show.actions], ["propresenter.clear_announcements", "obs.set_scene"])

        auto_show_bible = next(
            item for item in defaults if item.key == AUTO_SHOW_BIBLE_AUTOMATION_KEY
        )
        self.assertEqual(auto_show_bible.trigger, "propresenter.cue_changed")
        self.assertEqual(
            [condition["condition_type"] for condition in auto_show_bible.conditions],
            ["runtime.auto_show_enabled", "propresenter.active_presentation"],
        )
        self.assertEqual(
            [action.action_type for action in auto_show_bible.actions],
            ["propresenter.clear_announcements", "obs.set_scene"],
        )

        presentation_macro = next(
            item for item in defaults if item.key == PRESENTATION_FOCUS_AUTOMATION_KEY
        )
        self.assertEqual(presentation_macro.trigger, "propresenter.slide_changed")
        self.assertEqual(presentation_macro.interval_seconds, 0.5)
        self.assertEqual(presentation_macro.debounce_seconds, 10.0)
        self.assertEqual(presentation_macro.rules["operator"], "or")
        self.assertEqual(
            {
                child["params"]["look_name"]
                for child in presentation_macro.rules["children"]
            },
            {
                "Presentation Streamer",
                "Presentation Picture-in-Picture",
                "Presentation Fullscreen",
            },
        )
        self.assertEqual(
            [action.action_type for action in presentation_macro.actions],
            ["propresenter.trigger_macro"],
        )
        self.assertEqual(
            presentation_macro.actions[0].params,
            {"macro_name": "Presentation Macro"},
        )

    def test_legacy_watchdog_is_removed_and_auto_show_trigger_is_migrated(self) -> None:
        migrated = ensure_builtin_automation_steps(
            [
                AutomationDefinition(
                    key="obs_connection_watchdog",
                    name="OBS Connection Watchdog",
                    trigger="interval",
                    actions=[ActionDefinition("obs.reconnect")],
                ),
                AutomationDefinition(
                    key="auto_show_slides",
                    name="Auto Show Slides",
                    trigger="presentation_state_changed",
                    actions=[ActionDefinition("obs.set_scene", {"scene": "ProPresenter Input"})],
                ),
            ]
        )
        self.assertEqual(
            [item.key for item in migrated],
            [
                "auto_show_slides",
                PRESENTATION_FOCUS_AUTOMATION_KEY,
                AUTO_SHOW_BIBLE_AUTOMATION_KEY,
            ],
        )
        self.assertEqual(migrated[0].trigger, "propresenter.slide_changed")
        self.assertEqual(migrated[0].interval_seconds, 0.25)

    def test_legacy_bible_look_polling_is_removed(self) -> None:
        migrated = ensure_builtin_automation_steps(
            [
                AutomationDefinition(
                    key="bible_look_enforcement",
                    name="Bible Look Enforcement",
                    trigger="propresenter.presentation_changed",
                ),
                AutomationDefinition(
                    key=AUTO_SHOW_BIBLE_AUTOMATION_KEY,
                    name="Auto Show Bible Slides",
                    trigger="propresenter.presentation_changed",
                ),
            ]
        )

        by_key = {item.key: item for item in migrated}
        self.assertNotIn("bible_look_enforcement", by_key)
        self.assertEqual(
            "propresenter.cue_changed",
            by_key[AUTO_SHOW_BIBLE_AUTOMATION_KEY].trigger,
        )


if __name__ == "__main__":
    unittest.main()
