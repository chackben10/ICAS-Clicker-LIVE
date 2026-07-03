from __future__ import annotations

from fastapi import APIRouter

from production_hub.core.security.sanitize import redact_secrets


def router(context) -> APIRouter:
    api = APIRouter(prefix="/admin")

    @api.get("/config")
    async def config() -> dict:
        return redact_secrets(context.config.to_dict())

    @api.get("/endpoints")
    async def endpoints() -> dict:
        return {"items": [endpoint.to_dict() for endpoint in context.endpoint_registry.all()]}

    @api.get("/automations")
    async def automations() -> dict:
        return {
            "items": [definition.to_dict() for definition in context.automation_engine.definitions.values()],
            "states": [state.to_dict() for state in context.automation_engine.inspector_rows()],
        }

    @api.post("/automations/pause")
    async def pause_automations() -> dict:
        context.automation_engine.pause_all()
        return {"ok": True, "paused": True}

    @api.post("/automations/resume")
    async def resume_automations() -> dict:
        context.automation_engine.resume_all()
        return {"ok": True, "paused": False}

    @api.get("/logs")
    async def logs(component: str | None = None, limit: int = 200) -> dict:
        return {"items": context.log_repository.recent(limit=limit, component=component)}

    return api
