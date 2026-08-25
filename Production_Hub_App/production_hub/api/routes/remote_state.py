from __future__ import annotations

import math

from fastapi import APIRouter, HTTPException

from production_hub.core.config.defaults import PRESENTATION_FOCUS_AUTOMATION_KEY


def _enabled(value: object) -> bool:
    return str(value).lower() in {"1", "true", "yes", "on"}


def router(context) -> APIRouter:
    api = APIRouter()

    def presentation_focus_payload() -> dict:
        definition = context.automation_engine.definitions.get(PRESENTATION_FOCUS_AUTOMATION_KEY)
        if definition is None:
            raise HTTPException(status_code=404, detail="Presentation Focus Time automation is unavailable")
        return {
            "enabled": definition.enabled,
            "debounceSeconds": definition.debounce_seconds,
        }

    @api.get("/auto-show")
    async def get_auto_show() -> dict:
        return {"enabled": context.runtime_state_repo.auto_show_enabled()}

    @api.post("/auto-show")
    async def post_auto_show(payload: dict) -> dict:
        if "enabled" not in payload:
            return {"enabled": context.runtime_state_repo.auto_show_enabled()}
        state = context.runtime_state_repo.update(
            lambda current: setattr(current, "auto_show_enabled", _enabled(payload["enabled"]))
        )
        return {"enabled": state.auto_show_enabled}

    @api.get("/clicker-presentation-activation")
    async def get_clicker_presentation_activation() -> dict:
        return {
            "enabled": context.runtime_state_repo.clicker_presentation_activation_enabled()
        }

    @api.post("/clicker-presentation-activation")
    async def post_clicker_presentation_activation(payload: dict) -> dict:
        if "enabled" not in payload:
            raise HTTPException(status_code=422, detail="Missing required input: enabled")
        state = context.runtime_state_repo.update(
            lambda current: setattr(
                current,
                "clicker_presentation_activation_enabled",
                _enabled(payload["enabled"]),
            )
        )
        return {"enabled": state.clicker_presentation_activation_enabled}

    @api.get("/presentation-focus-time")
    async def get_presentation_focus_time() -> dict:
        return presentation_focus_payload()

    @api.post("/presentation-focus-time")
    async def post_presentation_focus_time(payload: dict) -> dict:
        definition = context.automation_engine.definitions.get(PRESENTATION_FOCUS_AUTOMATION_KEY)
        if definition is None:
            raise HTTPException(status_code=404, detail="Presentation Focus Time automation is unavailable")
        if "enabled" not in payload and "debounceSeconds" not in payload:
            raise HTTPException(status_code=422, detail="Supply enabled or debounceSeconds")

        enabled = definition.enabled
        debounce_seconds = definition.debounce_seconds
        if "enabled" in payload:
            enabled = _enabled(payload["enabled"])
        if "debounceSeconds" in payload:
            try:
                debounce_seconds = float(payload["debounceSeconds"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="debounceSeconds must be a number")
            if not math.isfinite(debounce_seconds) or not 0 <= debounce_seconds <= 3600:
                raise HTTPException(status_code=422, detail="debounceSeconds must be between 0 and 3600")

        definition.enabled = enabled
        definition.debounce_seconds = debounce_seconds
        state = context.automation_engine.states.get(PRESENTATION_FOCUS_AUTOMATION_KEY)
        if state is not None:
            state.enabled = enabled
        context.config_repository.save_automations(list(context.automation_engine.definitions.values()))
        return presentation_focus_payload()

    return api
