from __future__ import annotations

import re
from typing import Any


CLICKER_PRESENTATION_ACTIVATION_DISABLED = "clicker_presentation_activation_disabled"
CLICKER_PRESENTATION_TRIGGER_PATH = re.compile(r"^/presentation/[^/]+/\d+/trigger/?$")


def is_clicker_presentation_trigger(method: str, path: str) -> bool:
    return str(method).upper() == "POST" and bool(
        CLICKER_PRESENTATION_TRIGGER_PATH.fullmatch(str(path))
    )


def presentation_activation_enabled(context: Any) -> bool:
    state = context.runtime_state_repo.load()
    return bool(state.clicker_presentation_activation_enabled)


def presentation_activation_disabled_detail() -> dict[str, object]:
    return {
        "ok": False,
        "error": CLICKER_PRESENTATION_ACTIVATION_DISABLED,
    }
