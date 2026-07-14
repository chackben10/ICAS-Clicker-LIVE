from __future__ import annotations

import unittest
from types import SimpleNamespace

from production_hub.app.bootstrap import ensure_builtin_automation_steps
from production_hub.core.automation.evaluator import evaluate_rule_tree
from production_hub.core.automation.models import AutomationDefinition
from production_hub.core.automation.triggers import AutomationTriggerMonitor
from production_hub.core.config.defaults import build_default_automations
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

    def test_default_automations_have_no_connection_watchdog(self) -> None:
        defaults = build_default_automations()
        self.assertNotIn("obs_connection_watchdog", {item.key for item in defaults})
        auto_show = next(item for item in defaults if item.key == "auto_show_slides")
        self.assertEqual(auto_show.trigger, "propresenter.slide_changed")
        self.assertEqual([action.action_type for action in auto_show.actions], ["propresenter.clear_announcements", "obs.set_scene"])

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
        self.assertEqual([item.key for item in migrated], ["auto_show_slides"])
        self.assertEqual(migrated[0].trigger, "propresenter.slide_changed")
        self.assertEqual(migrated[0].interval_seconds, 0.2)


if __name__ == "__main__":
    unittest.main()
