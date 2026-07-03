from __future__ import annotations

from production_hub.core.config.models import ObsConfig, ObsLookRuleConfig


def rule_payload(rule: ObsLookRuleConfig) -> dict[str, object]:
    return {
        "sceneName": rule.target_scene,
        "show": [int(item) for item in rule.show_ids],
        "hide": [int(item) for item in rule.hide_ids],
    }


def rule_signature(look_name: str, payload: dict[str, object]) -> str:
    show = ",".join(str(item) for item in payload.get("show", []))
    hide = ",".join(str(item) for item in payload.get("hide", []))
    return f"{look_name}|{payload.get('sceneName', '')}|show={show}|hide={hide}"


def find_rule(config: ObsConfig, look_name: str) -> ObsLookRuleConfig | None:
    for rule in config.look_rules:
        if rule.enabled and rule.look_name == look_name:
            return rule
    return None


def transition_for_scene(config: ObsConfig, target_scene: str, current_scene: str = "") -> tuple[str, int | None]:
    special = set(config.special_transition_scenes)
    if target_scene in special or current_scene in special:
        return config.special_transition, None
    return config.default_transition, None

