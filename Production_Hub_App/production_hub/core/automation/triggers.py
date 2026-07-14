from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from production_hub.core.automation.catalog import normalize_trigger
from production_hub.core.automation.models import AutomationDefinition


def _presentation_index(data: dict[str, Any]) -> tuple[int | None, str, str, int | None, int | None]:
    payload = data.get("presentation_index")
    if not isinstance(payload, dict):
        return None, "", "", None, None
    raw_index = payload.get("index")
    index = int(raw_index) if raw_index is not None else None
    presentation_id = payload.get("presentation_id")
    presentation_id = presentation_id if isinstance(presentation_id, dict) else {}
    return (
        index,
        str(presentation_id.get("uuid") or ""),
        str(presentation_id.get("name") or ""),
        int(payload["total_cues"]) if payload.get("total_cues") is not None else None,
        int(payload["remaining_cues"]) if payload.get("remaining_cues") is not None else None,
    )


async def trigger_snapshot(context: Any, trigger_type: str) -> tuple[str, dict[str, Any]]:
    trigger_type = normalize_trigger(trigger_type)
    if trigger_type == "propresenter.look_changed":
        look = await context.propresenter.current_look_name()
        return look, {"current_look": look, "trigger": trigger_type}

    if trigger_type == "propresenter.presentation_changed":
        active = await context.propresenter.active_presentation()
        presentation = active.get("presentation") if isinstance(active, dict) else {}
        presentation = presentation if isinstance(presentation, dict) else {}
        identifier = presentation.get("id")
        identifier = identifier if isinstance(identifier, dict) else {}
        groups = presentation.get("groups")
        groups = groups if isinstance(groups, list) else []
        group_signature = ";".join(
            f"{group.get('uuid', '')}:{group.get('name', '')}" for group in groups if isinstance(group, dict)
        )
        uuid = str(identifier.get("uuid") or "")
        return f"{uuid}|{group_signature}", {
            "trigger": trigger_type,
            "presentation_uuid": uuid,
            "presentation_name": str(identifier.get("name") or ""),
            "group_count": len(groups),
            "first_group_name": str(groups[0].get("name") or "") if groups and isinstance(groups[0], dict) else "",
            "active_presentation": active,
            "bible_macro": context.config.integrations.propresenter.bible_macro_trigger_uuid,
            "bible_look": context.config.integrations.propresenter.bible_look_name,
        }

    if trigger_type == "propresenter.slide_changed":
        data = await context.propresenter.slide_index()
        index, uuid, name, total, remaining = _presentation_index(data)
        # An empty signature means ProPresenter currently has no active slide. It is
        # still observed so the next real slide is treated as a change.
        signature = f"{uuid}:{index}" if uuid and index is not None else "no-active-slide"
        return signature, {
            "trigger": trigger_type,
            "slide_index": index,
            "presentation_uuid": uuid,
            "presentation_name": name,
            "total_slides": total,
            "remaining_slides": remaining,
            "has_active_slide": bool(uuid and index is not None),
        }

    return "", {"trigger": trigger_type}


@dataclass
class TriggerState:
    observed_signature: str | None = None
    pending_signature: str | None = None
    pending_context: dict[str, Any] = field(default_factory=dict)
    pending_since: float = 0.0
    next_interval_at: float = 0.0
    next_poll_at: float = 0.0


class AutomationTriggerMonitor:
    """Turns trigger module snapshots into one-shot, debounced automation events."""

    def __init__(self, context: Any) -> None:
        self.context = context
        self.states: dict[str, TriggerState] = {}

    def forget_missing(self, keys: set[str]) -> None:
        self.states = {key: value for key, value in self.states.items() if key in keys}

    async def due(
        self,
        definition: AutomationDefinition,
        now: float | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        now = time.monotonic() if now is None else now
        state = self.states.setdefault(definition.key, TriggerState())
        trigger_type = normalize_trigger(definition.trigger)

        if trigger_type == "manual":
            return False, {}
        if trigger_type == "interval":
            interval = float(definition.interval_seconds)
            if interval <= 0:
                return False, {}
            if state.next_interval_at <= 0:
                state.next_interval_at = now + interval
                return False, {}
            if now < state.next_interval_at:
                return False, {}
            state.next_interval_at = now + interval
            return True, {"trigger": trigger_type}

        poll_interval = float(definition.interval_seconds) or float(
            self.context.config.integrations.propresenter.polling_interval_seconds
        )
        if now < state.next_poll_at:
            return False, {}
        state.next_poll_at = now + max(0.1, poll_interval)
        signature, event_context = await trigger_snapshot(self.context, trigger_type)
        if state.observed_signature is None:
            state.observed_signature = signature
            return False, {}
        if signature != state.observed_signature:
            state.observed_signature = signature
            state.pending_signature = signature
            state.pending_context = event_context
            state.pending_since = now
        if state.pending_signature != signature:
            return False, {}
        if (now - state.pending_since) < float(definition.debounce_seconds):
            return False, {}
        context = dict(state.pending_context)
        state.pending_signature = None
        state.pending_context = {}
        if trigger_type == "propresenter.slide_changed" and not context.get("has_active_slide"):
            return False, {}
        return True, context
