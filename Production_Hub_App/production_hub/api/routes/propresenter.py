from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Response


def router(context) -> APIRouter:
    api = APIRouter()

    @api.api_route("/next", methods=["GET", "POST"])
    async def next_slide() -> Response:
        await context.propresenter.next_slide()
        return Response("OK\n", media_type="text/plain")

    @api.api_route("/previous", methods=["GET", "POST"])
    @api.api_route("/prev", methods=["GET", "POST"])
    async def previous_slide() -> Response:
        await context.propresenter.previous_slide()
        return Response("OK\n", media_type="text/plain")

    @api.api_route("/focus", methods=["GET", "POST"])
    async def focus(index: int = Query(...)) -> Response:
        await context.propresenter.focus_slide(index)
        return Response("OK\n", media_type="text/plain")

    @api.get("/active-presentation")
    async def active_presentation() -> dict:
        return await context.propresenter.full_presentation()

    @api.get("/slide-index")
    async def slide_index() -> dict:
        return await context.propresenter.slide_index()

    @api.get("/current-base")
    async def current_base() -> dict:
        base = await context.propresenter.refresh_presentation_base()
        mode = "active" if base.endswith("/active") else "focused"
        return {"mode": mode, "base_url": f"{context.config.integrations.propresenter.base_url}{base}"}

    @api.get("/thumbnail")
    async def thumbnail(uuid: str, index: int, tier: str = "low") -> Response:
        entry = await context.propresenter.thumbnails.fetch(uuid, index, tier)
        return Response(entry.body, media_type=entry.content_type)

    @api.get("/service_logos")
    async def service_logos() -> dict:
        return {"items": [item.to_dict() for item in context.config.integrations.propresenter.service_logos]}

    @api.get("/macros")
    async def macros() -> dict:
        return {"items": [{"name": item.macro_name} for item in context.config.integrations.propresenter.macros]}

    @api.post("/macro")
    async def macro(payload: dict) -> dict:
        macro_name = str(payload.get("name") or payload.get("macro_name") or "").strip()
        if not macro_name:
            raise HTTPException(status_code=400, detail={"ok": False, "error": "missing_macro_name"})
        try:
            await context.propresenter.trigger_macro(macro_name)
        except ValueError:
            raise HTTPException(status_code=400, detail={"ok": False, "error": "macro_not_in_list", "name": macro_name})
        return {"ok": True, "name": macro_name}

    @api.get("/audio/playlists")
    async def audio_playlists() -> dict:
        return {"items": await context.propresenter.audio.playlists()}

    @api.get("/audio/tracks")
    async def audio_tracks(playlist: str) -> dict:
        return {"items": await context.propresenter.audio.playlist_tracks(playlist)}

    @api.post("/audio/trigger")
    async def audio_trigger(payload: dict) -> dict:
        playlist = str(payload.get("playlist") or "").strip()
        track = str(payload.get("track") or "").strip()
        if not playlist or not track:
            raise HTTPException(status_code=400, detail={"ok": False, "error": "missing_params"})
        await context.propresenter.audio.trigger(playlist, track)
        return {"ok": True}

    @api.api_route("/audio/clear", methods=["GET", "POST"])
    async def audio_clear() -> dict:
        await context.propresenter.audio.clear()
        return {"ok": True}

    @api.get("/audio/active")
    async def audio_active() -> Response:
        return Response(await context.propresenter.audio.active_text(), media_type="text/plain; charset=utf-8")

    @api.api_route("/timer/start", methods=["GET", "POST"])
    async def timer_start() -> Response:
        await context.propresenter.timer_start()
        return Response("OK\n", media_type="text/plain")

    @api.api_route("/timer/stop-reset", methods=["GET", "POST"])
    async def timer_stop_reset() -> Response:
        await context.propresenter.timer_stop()
        await asyncio.sleep(context.config.integrations.propresenter.timer.stop_reset_delay_seconds)
        await context.propresenter.timer_reset()
        return Response("OK\n", media_type="text/plain")

    return api
