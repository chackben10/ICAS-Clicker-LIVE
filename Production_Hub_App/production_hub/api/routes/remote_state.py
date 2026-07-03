from __future__ import annotations

from fastapi import APIRouter


def router(context) -> APIRouter:
    api = APIRouter()

    @api.get("/auto-show")
    async def get_auto_show() -> dict:
        state = context.runtime_state_repo.load()
        return {"enabled": state.auto_show_enabled}

    @api.post("/auto-show")
    async def post_auto_show(payload: dict) -> dict:
        state = context.runtime_state_repo.load()
        if "enabled" in payload:
            state.auto_show_enabled = str(payload["enabled"]).lower() in {"1", "true", "yes", "on"}
            context.runtime_state_repo.save(state)
        return {"enabled": state.auto_show_enabled}

    return api

