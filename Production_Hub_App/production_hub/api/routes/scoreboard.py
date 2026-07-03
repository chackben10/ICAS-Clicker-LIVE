from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from production_hub.integrations.scoreboard.service import ScoreboardConflict


def router(context) -> APIRouter:
    api = APIRouter()

    @api.get("/score")
    async def get_score() -> dict:
        return context.scoreboard.get_state().legacy_payload()

    @api.post("/score")
    async def post_score(request: Request, payload: dict) -> dict:
        expected = payload.get("expected_revision")
        if expected is not None:
            expected = int(expected)
        writer = {
            "caller_ip": request.client.host if request.client else "",
            "user_agent": request.headers.get("user-agent", ""),
        }
        try:
            return context.scoreboard.update_state(payload, writer=writer, expected_revision=expected).legacy_payload()
        except ScoreboardConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={"ok": False, "error": "revision_conflict", "expected": exc.expected, "actual": exc.actual},
            )

    return api

