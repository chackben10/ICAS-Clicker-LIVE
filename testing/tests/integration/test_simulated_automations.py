from __future__ import annotations

from production_hub.core.config.defaults import build_default_automations
from production_hub.core.automation.catalog import trigger_spec


def test_every_default_automation_runs_once_and_deduplicates() -> None:
    automations = build_default_automations()
    assert len({item.key for item in automations}) == len(automations)
    for automation in automations:
        assert automation.enabled
        assert automation.trigger
        assert automation.actions
        assert automation.debounce_seconds >= 0
        assert automation.cooldown_seconds >= 0
        if automation.trigger != "manual" and not trigger_spec(automation.trigger).event_driven:
            assert automation.interval_seconds > 0
