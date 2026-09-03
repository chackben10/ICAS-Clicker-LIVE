from __future__ import annotations

import hashlib
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


def _slide_status(data: dict[str, Any]) -> tuple[str, str, str]:
    current = data.get("current")
    if not isinstance(current, dict):
        return "", "", ""
    identifier = current.get("id")
    identifier = identifier if isinstance(identifier, dict) else {}
    uuid = str(identifier.get("uuid") or current.get("uuid") or "").strip()
    text = str(current.get("text") or "")
    notes = str(current.get("notes") or "")
    return uuid, text, notes


async def trigger_snapshot(context: Any, trigger_type: str) -> tuple[str, dict[str, Any]]:
    trigger_type = normalize_trigger(trigger_type)
    if trigger_type == "propresenter.look_changed":
        monitored = getattr(context.propresenter, "monitored_current_look_name", None)
        look = await monitored() if monitored is not None else await context.propresenter.current_look_name()
        return look, {"current_look": look, "trigger": trigger_type}

    if trigger_type == "propresenter.presentation_changed":
        monitored = getattr(context.propresenter, "monitored_active_presentation", None)
        active = await monitored() if monitored is not None else await context.propresenter.active_presentation()
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
        name = str(identifier.get("name") or "")
        return f"{uuid}:{name}|{group_signature}", {
            "trigger": trigger_type,
            "presentation_uuid": uuid,
            "presentation_name": name,
            "group_count": len(groups),
            "first_group_name": str(groups[0].get("name") or "") if groups and isinstance(groups[0], dict) else "",
            "active_presentation": active,
            "bible_macro": context.config.integrations.propresenter.bible_macro_trigger_uuid,
            "bible_look": context.config.integrations.propresenter.bible_look_name,
        }

    if trigger_type == "propresenter.slide_changed":
        monitored = getattr(context.propresenter, "monitored_slide_index", None)
        data = await monitored() if monitored is not None else await context.propresenter.slide_index()
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

    if trigger_type == "propresenter.cue_changed":
        monitored = getattr(context.propresenter, "monitored_slide_status", None)
        data = await monitored() if monitored is not None else await context.propresenter.slide_status()
        uuid, text, notes = _slide_status(data if isinstance(data, dict) else {})
        # Cue UUIDs are stable for normal slides and unique for every verse in
        # ProPresenter's dynamic Bible tab.  Text is included as a defensive
        # fallback for older ProPresenter builds that omit or reuse the UUID.
        text_fingerprint = hashlib.blake2s(
            f"{text}\0{notes}".encode("utf-8"),
            digest_size=10,
        ).hexdigest() if text or notes else ""
        signature = f"{uuid}:{text_fingerprint}" if uuid or text_fingerprint else "no-active-cue"
        return signature, {
            "trigger": trigger_type,
            "slide_uuid": uuid,
            "slide_text": text,
            "slide_notes": notes,
            "slide_status": data,
            "has_active_slide": bool(uuid or text),
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
        self._snapshot_cycle_at: float | None = None
        self._snapshot_cache: dict[str, tuple[str, dict[str, Any]]] = {}

    def forget_missing(self, keys: set[str]) -> None:
        self.states = {key: value for key, value in self.states.items() if key in keys}

    def forget(self, key: str) -> None:
        self.states.pop(key, None)

    def reset(self) -> None:
        self.states.clear()
        self._snapshot_cache.clear()
        self._snapshot_cycle_at = None

    def _start_snapshot_cycle(self, now: float) -> None:
        if self._snapshot_cycle_at != now:
            self._snapshot_cycle_at = now
            self._snapshot_cache = {}

    async def _snapshot(self, trigger_type: str, now: float) -> tuple[str, dict[str, Any]]:
        self._start_snapshot_cycle(now)
        cached = self._snapshot_cache.get(trigger_type)
        if cached is None:
            signature, event_context = await trigger_snapshot(self.context, trigger_type)
            cached = (signature, dict(event_context))
            self._snapshot_cache[trigger_type] = cached
        signature, event_context = cached
        return signature, dict(event_context)

    def enrich_from_snapshot_cycle(
        self,
        action_context: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        """Reuse data already fetched during this scheduler cycle for conditions."""
        self._start_snapshot_cycle(now)
        enriched = dict(action_context)
        for _signature, cached_context in self._snapshot_cache.values():
            for key in ("current_look", "active_presentation"):
                if key in cached_context:
                    enriched.setdefault(key, cached_context[key])
        return enriched

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
        signature, event_context = await self._snapshot(trigger_type, now)
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
        if trigger_type in {
            "propresenter.slide_changed",
            "propresenter.cue_changed",
        } and not context.get("has_active_slide"):
            return False, {}
        return True, context
