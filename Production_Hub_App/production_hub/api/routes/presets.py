from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request


def _boolish(value: object) -> bool:
    return str(value or "").lower() in {"1", "true", "yes", "on"}


def router(context) -> APIRouter:
    api = APIRouter()

    @api.post("/preset")
    async def preset(request: Request, payload: dict) -> dict:
        preset_name = str(payload.get("preset") or "").lower()
        clear_slide = _boolish(request.query_params.get("clearslide"))
        clear_delay = context.config.integrations.propresenter.clear_slide_delay_seconds
        logo_uuid = str(payload.get("service_logo_uuid") or payload.get("serviceLogoUuid") or "").strip()

        try:
            if preset_name == "stream_beginning":
                await context.propresenter.trigger_presentation_label("Starting Announcements")
                await context.obs.set_scene("Stream Start", use_policy=True)
                return {"ok": True}
            if preset_name == "camera":
                await context.propresenter.trigger_presentation_label("PTZ Camera")
                await context.obs.set_scene("PTZ Camera", use_policy=True)
                if clear_slide:
                    await context.propresenter.clear_slide(clear_delay)
                return {"ok": True, "clearslide": clear_slide}
            if preset_name == "show_slides":
                await context.propresenter.clear_announcements()
                await context.obs.set_scene("ProPresenter Input", use_policy=True)
                return {"ok": True}
            if preset_name == "service_logo":
                if not logo_uuid:
                    raise HTTPException(status_code=400, detail={"ok": False, "error": "missing_service_logo_uuid"})
                await context.propresenter.trigger_service_logo(logo_uuid)
                await context.obs.set_scene("Audience Camera", use_policy=True)
                if clear_slide:
                    await context.propresenter.clear_slide(clear_delay)
                return {"ok": True, "clearslide": clear_slide}
            if preset_name == "testimonies":
                if not logo_uuid:
                    raise HTTPException(status_code=400, detail={"ok": False, "error": "missing_service_logo_uuid"})
                await context.propresenter.trigger_service_logo(logo_uuid)
                await context.obs.set_scene("Testimonies", use_policy=True)
                return {"ok": True}
            if preset_name == "ending_stream":
                await context.propresenter.trigger_presentation_label("Ending Announcements")
                await context.obs.set_scene("Thanks Screen", use_policy=True)
                return {"ok": True}
            if preset_name in {"clear_slide", "safely_clear_slide"}:
                await context.propresenter.clear_slide()
                return {"ok": True, "clearslide": True}
            if preset_name == "nsc_setup":
                await context.propresenter.clear_announcements()
                await context.propresenter.trigger_presentation_label("iMac Screen")
                return {"ok": True}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"ok": False, "error": str(exc)})

        raise HTTPException(status_code=400, detail={"ok": False, "error": "unknown_preset"})

    return api
