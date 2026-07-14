from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from production_hub.core.endpoints.catalog import FieldSpec


@dataclass(frozen=True)
class ConditionSpec:
    condition_type: str
    label: str
    category: str
    description: str
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TriggerSpec:
    trigger_type: str
    label: str
    description: str
    event_driven: bool = False


TRIGGER_SPECS: tuple[TriggerSpec, ...] = (
    TriggerSpec("manual", "Manual only", "Run from the builder or another explicit caller."),
    TriggerSpec("interval", "On an interval", "Run repeatedly after the configured interval."),
    TriggerSpec("propresenter.look_changed", "ProPresenter look changes", "Run once when the active look changes.", True),
    TriggerSpec(
        "propresenter.presentation_changed",
        "ProPresenter presentation changes",
        "Run once when the active presentation or its group structure changes.",
        True,
    ),
    TriggerSpec(
        "propresenter.slide_changed",
        "ProPresenter slide changes",
        "Run once for each new presentation UUID and slide index.",
        True,
    ),
)


LEGACY_TRIGGER_ALIASES = {
    "look_changed_or_poll": "propresenter.look_changed",
    "active_slide_changed": "propresenter.slide_changed",
    "presentation_state_changed": "propresenter.slide_changed",
}


def normalize_trigger(trigger_type: str) -> str:
    return LEGACY_TRIGGER_ALIASES.get(str(trigger_type or "").strip(), str(trigger_type or "").strip())


def trigger_spec(trigger_type: str) -> TriggerSpec:
    normalized = normalize_trigger(trigger_type)
    for spec in TRIGGER_SPECS:
        if spec.trigger_type == normalized:
            return spec
    return TriggerSpec(normalized, normalized, "Custom trigger module.")


CONDITION_SPECS: tuple[ConditionSpec, ...] = (
    ConditionSpec("always", "Always", "Utility", "Always allow the automation to run."),
    ConditionSpec(
        "propresenter.timer_running",
        "ProPresenter Timer Running",
        "ProPresenter",
        "Check whether a named ProPresenter timer is running.",
        (
            FieldSpec("timer_name", "Timer name", "text", "Service Countdown"),
            FieldSpec("running", "Should be running", "bool", True),
        ),
    ),
    ConditionSpec(
        "propresenter.current_look",
        "Current ProPresenter Look",
        "ProPresenter",
        "Check the active ProPresenter look name.",
        (
            FieldSpec("look_name", "Look name", "text", "Bible"),
            FieldSpec("matches", "Must match", "bool", True),
        ),
    ),
    ConditionSpec(
        "obs.current_scene",
        "Current OBS Scene",
        "OBS",
        "Check the current OBS program scene.",
        (
            FieldSpec("scene", "Scene", "select", "", context_options="obs_scenes"),
            FieldSpec("matches", "Must match", "bool", True),
        ),
    ),
    ConditionSpec(
        "runtime.auto_show_enabled",
        "Auto Show Enabled",
        "Runtime",
        "Check the runtime Auto Show setting.",
        (FieldSpec("enabled", "Should be enabled", "bool", True),),
    ),
    ConditionSpec(
        "propresenter.active_presentation",
        "Active Presentation Structure",
        "ProPresenter",
        "Match the active presentation's group count or first group name.",
        (
            FieldSpec("group_count", "Group count", "text", "", "Leave blank to ignore."),
            FieldSpec("first_group_contains", "First group contains", "text", "", "Leave blank to ignore."),
        ),
    ),
    ConditionSpec(
        "event.value",
        "Trigger Value",
        "Trigger",
        "Compare any value supplied by the selected trigger module.",
        (
            FieldSpec("name", "Value name", "text", "slide_index"),
            FieldSpec(
                "operator",
                "Comparison",
                "select",
                "equals",
                options=("equals", "not equals", "contains", "starts with", "ends with", "greater than", "less than", "exists", "missing", "is true", "is false"),
            ),
            FieldSpec("value", "Value", "text", ""),
        ),
    ),
)


def condition_spec(condition_type: str) -> ConditionSpec:
    for spec in CONDITION_SPECS:
        if spec.condition_type == condition_type:
            return spec
    return ConditionSpec(condition_type, condition_type, "Custom", "Custom condition type.", ())


def condition_params(condition: dict[str, Any]) -> dict[str, Any]:
    return dict(condition.get("params") or {})
