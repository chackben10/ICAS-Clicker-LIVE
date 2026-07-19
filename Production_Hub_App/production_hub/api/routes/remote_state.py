from __future__ import annotations

from fastapi import APIRouter, HTTPException


def _enabled(value: object) -> bool:
    return str(value).lower() in {"1", "true", "yes", "on"}


def router(context) -> APIRouter:
    api = APIRouter()

    @api.get("/auto-show")
    async def get_auto_show() -> dict:
        state = context.runtime_state_repo.load()
        return {"enabled": state.auto_show_enabled}

    @api.post("/auto-show")
    async def post_auto_show(payload: dict) -> dict:
        if "enabled" not in payload:
            state = context.runtime_state_repo.load()
            return {"enabled": state.auto_show_enabled}
        state = context.runtime_state_repo.update(
            lambda current: setattr(current, "auto_show_enabled", _enabled(payload["enabled"]))
        )
        return {"enabled": state.auto_show_enabled}

    @api.get("/clicker-presentation-activation")
    async def get_clicker_presentation_activation() -> dict:
        state = context.runtime_state_repo.load()
        return {"enabled": state.clicker_presentation_activation_enabled}

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

    return api
