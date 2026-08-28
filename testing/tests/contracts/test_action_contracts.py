from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from production_hub.app.bootstrap import build_context
from production_hub.core.endpoints.catalog import ACTION_SPECS


def test_every_action_has_an_executable_contract() -> None:
    with TemporaryDirectory() as temp_dir:
        context = build_context(Path(temp_dir))
        try:
            handlers = set(context.endpoint_executor.router._handlers)
            catalog = {item.action_type for item in ACTION_SPECS}
            assert catalog - {"delay"} == handlers
            assert len(catalog) == len(ACTION_SPECS)
            for spec in ACTION_SPECS:
                assert spec.label.strip()
                assert spec.category.strip()
                assert spec.description.strip()
        finally:
            context.video.shutdown()

