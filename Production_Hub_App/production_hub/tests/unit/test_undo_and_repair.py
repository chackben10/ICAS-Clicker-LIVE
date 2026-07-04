from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from production_hub.app.bootstrap import build_context
from production_hub.core.automation.models import AutomationDefinition
from production_hub.core.config.repository import ConfigRepository
from production_hub.core.config.models import AppPaths
from production_hub.core.endpoints.models import ActionDefinition, EndpointDefinition
from production_hub.state.undo_manager import UndoManager


class UndoAndRepairTests(unittest.TestCase):
    def test_undo_manager_keeps_at_least_100_items_and_redoes(self) -> None:
        value = {"n": 0}
        manager = UndoManager(max_items=100)
        for index in range(105):
            before = index
            after = index + 1
            manager.record(
                f"change {index}",
                lambda before=before: value.update(n=before),
                lambda after=after: value.update(n=after),
            )
        self.assertTrue(manager.can_undo())
        self.assertEqual(len(manager._undo_stack), 100)
        self.assertEqual(manager.undo(), "Undid: change 104")
        self.assertEqual(value["n"], 104)
        self.assertTrue(manager.can_redo())
        self.assertEqual(manager.redo(), "Redid: change 104")
        self.assertEqual(value["n"], 105)

    def test_build_context_repairs_missing_audio_clear_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp))
            repo = ConfigRepository(paths)
            repo.save_endpoints(
                [
                    EndpointDefinition(
                        "next_slide",
                        "Next Slide",
                        "/next",
                        [ActionDefinition("propresenter.next_slide")],
                    )
                ]
            )
            context = build_context(Path(tmp))
            self.assertIsNotNone(context.endpoint_registry.get("audio_clear"))
            reloaded = ConfigRepository(paths).load_endpoints()
            self.assertTrue(any(endpoint.key == "audio_clear" for endpoint in reloaded))

    def test_build_context_repairs_blank_builtin_automation_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp))
            repo = ConfigRepository(paths)
            repo.save_automations(
                [
                    AutomationDefinition(
                        key="auto_show_slides",
                        name="Auto Show Slides",
                        trigger="presentation_state_changed",
                        conditions=[],
                        actions=[],
                    )
                ]
            )
            context = build_context(Path(tmp))
            repaired = context.automation_engine.definitions["auto_show_slides"]
            self.assertGreater(len(repaired.conditions), 0)
            self.assertGreater(len(repaired.actions), 0)
            reloaded = ConfigRepository(paths).load_automations()[0]
            self.assertGreater(len(reloaded.conditions), 0)
            self.assertGreater(len(reloaded.actions), 0)


if __name__ == "__main__":
    unittest.main()
