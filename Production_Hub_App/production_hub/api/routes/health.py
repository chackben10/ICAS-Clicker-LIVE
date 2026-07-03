from __future__ import annotations

from fastapi import APIRouter, Request, Response


def router(context) -> APIRouter:
    api = APIRouter()

    @api.get("/health")
    async def health() -> Response:
        return Response("OK", media_type="text/plain")

    @api.get("/admin/health")
    async def admin_health(request: Request) -> dict:
        snapshot = context.health_monitor.snapshot(
            context.endpoint_registry.all(),
            context.automation_engine.definitions.values(),
        )
        return snapshot.to_dict()

    return api

