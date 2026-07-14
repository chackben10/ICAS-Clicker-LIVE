from __future__ import annotations

from typing import Any

from production_hub.core.automation.catalog import condition_params
from production_hub.core.endpoints.variables import resolve_template


def boolish(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


async def propresenter_timer_running(context: Any, timer_name: str) -> bool:
    timer_q = context.propresenter.client.quote_segment(timer_name)
    data = await context.propresenter.client.get_json(f"/timer/{timer_q}")
    for key in ("running", "isRunning", "is_running"):
        if key in data:
            return boolish(data[key])
    state = str(data.get("state") or data.get("timerState") or "").lower()
    return state in {"running", "started", "play", "playing"}


async def evaluate_condition(context: Any, condition: dict[str, Any], action_context: dict[str, Any] | None = None) -> tuple[bool, str]:
    action_context = action_context or {}
    condition_type = str(condition.get("condition_type") or condition.get("type") or "always")
    params = resolve_template(condition_params(condition), action_context)

    if condition_type == "always":
        return True, "always"

    if condition_type == "runtime.auto_show_enabled":
        expected = boolish(params.get("enabled", True))
        actual = bool(context.runtime_state_repo.load().auto_show_enabled)
        return actual == expected, f"auto_show={actual}"

    if condition_type == "propresenter.current_look":
        wanted = str(params.get("look_name") or "").strip()
        matches = boolish(params.get("matches", True))
        actual = str(action_context.get("current_look") or "")
        if not actual:
            actual = await context.propresenter.current_look_name()
        result = actual == wanted
        return result == matches, f"look={actual}"

    if condition_type == "propresenter.timer_running":
        timer_name = str(params.get("timer_name") or context.config.integrations.propresenter.timer.timer_name)
        expected = boolish(params.get("running", True))
        actual = await propresenter_timer_running(context, timer_name)
        return actual == expected, f"timer_running={actual}"

    if condition_type == "obs.current_scene":
        wanted = str(params.get("scene") or "").strip()
        matches = boolish(params.get("matches", True))
        actual = await context.obs.get_current_scene()
        result = actual == wanted
        return result == matches, f"scene={actual}"

    if condition_type == "propresenter.active_presentation":
        active = action_context.get("active_presentation")
        if not isinstance(active, dict):
            active = await context.propresenter.active_presentation()
        presentation = active.get("presentation") if isinstance(active, dict) else {}
        groups = presentation.get("groups") if isinstance(presentation, dict) else []
        groups = groups if isinstance(groups, list) else []
        wanted_count = str(params.get("group_count") or "").strip()
        contains = str(params.get("first_group_contains") or "")
        first_name = str(groups[0].get("name") or "") if groups and isinstance(groups[0], dict) else ""
        if wanted_count and len(groups) != int(wanted_count):
            return False, f"group_count={len(groups)}"
        if contains and contains not in first_name:
            return False, f"first_group={first_name}"
        return True, f"group_count={len(groups)}; first_group={first_name}"

    if condition_type == "event.value":
        name = str(params.get("name") or "").strip()
        operator = str(params.get("operator") or "equals").strip().lower().replace("_", " ")
        expected = params.get("value", "")
        exists = name in action_context and action_context.get(name) is not None
        actual = action_context.get(name)
        if operator == "exists":
            result = exists
        elif operator == "missing":
            result = not exists
        elif operator == "is true":
            result = boolish(actual)
        elif operator == "is false":
            result = not boolish(actual)
        elif operator == "not equals":
            result = str(actual) != str(expected)
        elif operator == "contains":
            result = str(expected) in str(actual)
        elif operator == "starts with":
            result = str(actual).startswith(str(expected))
        elif operator == "ends with":
            result = str(actual).endswith(str(expected))
        elif operator in {"greater than", "less than"}:
            try:
                result = float(actual) > float(expected) if operator == "greater than" else float(actual) < float(expected)
            except (TypeError, ValueError):
                result = False
        else:
            result = str(actual) == str(expected)
        return result, f"{name}={actual}"

    return False, f"unknown_condition:{condition_type}"


async def evaluate_conditions(context: Any, conditions: list[dict[str, Any]], action_context: dict[str, Any] | None = None) -> tuple[bool, str]:
    if not conditions:
        return True, "no_conditions"
    messages: list[str] = []
    for condition in conditions:
        ok, message = await evaluate_condition(context, condition, action_context)
        messages.append(message)
        if not ok:
            return False, "; ".join(messages)
    return True, "; ".join(messages)


async def evaluate_rule_tree(
    context: Any,
    rule: dict[str, Any] | None,
    action_context: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Evaluate nested ALL/ANY/NONE groups and negated leaf rules."""
    action_context = action_context or {}
    rule = rule or {"operator": "and", "children": []}
    if "condition_type" in rule or ("type" in rule and "children" not in rule):
        ok, message = await evaluate_condition(context, rule, action_context)
        if bool(rule.get("negate", False)):
            return not ok, f"NOT ({message})"
        return ok, message

    operator = str(rule.get("operator") or "and").strip().lower()
    children = [child for child in (rule.get("children") or []) if isinstance(child, dict)]
    if not children:
        return True, "no_rules"

    results: list[bool] = []
    messages: list[str] = []
    for child in children:
        ok, message = await evaluate_rule_tree(context, child, action_context)
        results.append(ok)
        messages.append(message)
    if operator == "or":
        result = any(results)
        label = "ANY"
    elif operator in {"not", "none"}:
        result = not any(results)
        label = "NONE"
    else:
        result = all(results)
        label = "ALL"
    return result, f"{label} [" + "; ".join(messages) + "]"
