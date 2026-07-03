from __future__ import annotations

from typing import Any


def active_presentation_has_single_colon_group(active_payload: dict[str, Any]) -> bool:
    presentation = active_payload.get("presentation") or {}
    groups = presentation.get("groups") or []
    if len(groups) != 1:
        return False
    name = str(groups[0].get("name") or "")
    return ":" in name


def changed_signature(*parts: object) -> str:
    return "|".join(str(part) for part in parts)

