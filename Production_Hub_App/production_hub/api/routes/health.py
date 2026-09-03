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

    @api.get("/admin/performance")
    async def admin_performance() -> dict:
        """Press-to-cue distribution and ProPresenter's memory curve.

        These are the two numbers a change is judged against. Hub-to-ProPresenter
        HTTP latency is deliberately not reported here: it is a much smaller,
        much less meaningful number and was previously mistaken for this one.
        """

        recorder = getattr(context, "press_to_cue", None)
        sampler = getattr(context, "propresenter_resources", None)
        return {
            "pressToCue": recorder.summary() if recorder is not None else {"samples": 0},
            "recentPresses": recorder.recent(25) if recorder is not None else [],
            "propresenter": sampler.summary() if sampler is not None else {"samples": 0},
        }

    return api

