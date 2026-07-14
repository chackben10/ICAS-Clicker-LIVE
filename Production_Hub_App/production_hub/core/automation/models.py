from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from production_hub.core.config.models import JsonModel, ValidationError
from production_hub.core.endpoints.models import ActionDefinition


def conditions_to_rule_tree(conditions: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert the original flat, implicit-AND condition list to a rule tree."""
    return {
        "operator": "and",
        "children": [
            {
                "condition_type": str(condition.get("condition_type") or condition.get("type") or "always"),
                "params": dict(condition.get("params") or {}),
                "negate": bool(condition.get("negate", False)),
            }
            for condition in conditions
        ],
    }


def rule_tree_leaves(rule: dict[str, Any]) -> list[dict[str, Any]]:
    if "condition_type" in rule or ("type" in rule and "children" not in rule):
        return [rule]
    leaves: list[dict[str, Any]] = []
    for child in rule.get("children") or []:
        if isinstance(child, dict):
            leaves.extend(rule_tree_leaves(child))
    return leaves


@dataclass
class AutomationDefinition(JsonModel):
    key: str
    name: str
    trigger: str
    enabled: bool = True
    interval_seconds: float = 0
    cooldown_seconds: float = 0
    debounce_seconds: float = 0
    conditions: list[dict[str, Any]] = field(default_factory=list)
    rules: dict[str, Any] = field(default_factory=dict)
    actions: list[ActionDefinition] = field(default_factory=list)
    repeated_error_disable_threshold: int = 5
    description: str = ""

    def __post_init__(self) -> None:
        self.key = str(self.key or "").strip()
        self.name = str(self.name or "").strip()
        self.trigger = str(self.trigger or "").strip()
        if not self.key or not self.name or not self.trigger:
            raise ValidationError("Automation key, name, and trigger are required")
        self.interval_seconds = max(0, float(self.interval_seconds))
        self.cooldown_seconds = max(0, float(self.cooldown_seconds))
        self.debounce_seconds = max(0, float(self.debounce_seconds))
        if not self.rules:
            self.rules = conditions_to_rule_tree(self.conditions)
        if not isinstance(self.rules, dict):
            raise ValidationError("Automation rules must be a rule group")
        # Keep the legacy field populated so older exports and diagnostics remain useful.
        self.conditions = [dict(item) for item in rule_tree_leaves(self.rules)]


@dataclass
class AutomationRunState(JsonModel):
    key: str
    enabled: bool = True
    last_execution_at: str = ""
    last_condition_result: str = ""
    last_action_result: str = ""
    last_error: str = ""
    run_count: int = 0
    consecutive_errors: int = 0

    def mark_success(self, result: str) -> None:
        self.last_execution_at = datetime.now(UTC).isoformat()
        self.last_action_result = result
        self.last_error = ""
        self.run_count += 1
        self.consecutive_errors = 0

    def mark_error(self, error: str) -> None:
        self.last_execution_at = datetime.now(UTC).isoformat()
        self.last_error = error
        self.run_count += 1
        self.consecutive_errors += 1
