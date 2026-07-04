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
)


def condition_spec(condition_type: str) -> ConditionSpec:
    for spec in CONDITION_SPECS:
        if spec.condition_type == condition_type:
            return spec
    return ConditionSpec(condition_type, condition_type, "Custom", "Custom condition type.", ())


def condition_params(condition: dict[str, Any]) -> dict[str, Any]:
    return dict(condition.get("params") or {})

