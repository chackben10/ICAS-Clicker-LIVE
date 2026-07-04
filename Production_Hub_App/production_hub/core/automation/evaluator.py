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

