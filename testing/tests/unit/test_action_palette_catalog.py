from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from production_hub.app.bootstrap import build_context
from production_hub.core.endpoints.catalog import ACTION_SPECS


class ActionPaletteCatalogTests(unittest.TestCase):
    def test_palette_and_registered_handlers_stay_in_sync(self) -> None:
        catalog_types = {spec.action_type for spec in ACTION_SPECS}
        with TemporaryDirectory() as tmp:
            context = build_context(Path(tmp))
            handler_types = set(context.endpoint_executor.router._handlers)

        missing_from_palette = sorted(handler_types - catalog_types)
        missing_handlers = sorted(catalog_types - handler_types - {"delay"})

        self.assertEqual(
            [],
            missing_from_palette,
            "New action handlers must be added to ACTION_SPECS so they appear in the shared action palette.",
        )
        self.assertEqual(
            [],
            missing_handlers,
            "New ACTION_SPECS entries must have handlers in register_action_handlers, except the built-in delay action.",
        )

    def test_palette_action_types_are_unique(self) -> None:
        action_types = [spec.action_type for spec in ACTION_SPECS]
        duplicates = sorted({action_type for action_type in action_types if action_types.count(action_type) > 1})
        self.assertEqual([], duplicates)


if __name__ == "__main__":
    unittest.main()
