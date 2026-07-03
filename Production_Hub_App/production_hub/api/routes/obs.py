from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response


def router(context) -> APIRouter:
    api = APIRouter()

    @api.get("/scene/current")
    async def scene_current() -> dict:
        scene = await context.obs.get_current_scene()
        return {"ok": True, "currentProgramSceneName": scene}

    @api.get("/scene/set")
    async def scene_set(name: str, transition: str = "", duration: int | None = None) -> dict:
        if transition:
            await context.obs.set_transition(transition, duration)
            await context.obs.client.call("set_current_program_scene", name)
            context.obs.current_scene = name
        else:
            await context.obs.set_scene(name, use_policy=True)
        return {"ok": True, "scene": name, "transition": transition or None, "durationMs": duration}

    @api.get("/scene/items")
    async def scene_items(scene: str = Query(default="")) -> dict:
        scene_name = scene or context.config.integrations.obs.main_layout_scene
        items = await context.obs.get_scene_items(scene_name)
        return {"ok": True, "sceneName": scene_name, "items": [item.to_dict() for item in items]}

    @api.get("/scene/items/text")
    async def scene_items_text(scene: str = Query(default="")) -> Response:
        scene_name = scene or context.config.integrations.obs.main_layout_scene
        items = await context.obs.get_scene_items(scene_name)
        lines = [
            "------------------------------------------------------------",
            f"OBS scene items for: {scene_name}",
            "Use sceneItemId in Production Hub look rules.",
            "------------------------------------------------------------",
        ]
        for item in items:
            lines.append(
                f"sceneItemId={item.scene_item_id} | enabled={item.enabled} "
                f"| sourceName={item.source_name} | sourceType={item.source_type} | sourceUuid={item.source_uuid}"
            )
        lines.append("------------------------------------------------------------")
        return Response("\n".join(lines) + "\n", media_type="text/plain; charset=utf-8")

    @api.post("/scene/items/apply")
    async def scene_items_apply(payload: dict) -> dict:
        scene_name = str(payload.get("sceneName") or payload.get("scene") or context.config.integrations.obs.main_layout_scene)
        applied = await context.obs.apply_scene_item_visibility(scene_name, payload)
        return {"ok": True, "sceneName": scene_name, "applied": applied}

    @api.get("/set")
    async def legacy_set(
        mode: str,
        scene: str = "ProPresenter Slides",
        srcAnn: str = "Audience Camera",
        srcCam: str = "PTZ Camera",
    ) -> Response:
        mode = mode.lower()
        if mode not in {"none", "ann", "cam"}:
            return Response("mode must be none|ann|cam\n", status_code=400, media_type="text/plain")
        items = await context.obs.get_scene_items(scene)
        by_name = {item.source_name: item.scene_item_id for item in items}
        payload = {"show": [], "hide": []}
        if mode == "none":
            payload["hide"] = [by_name[name] for name in (srcAnn, srcCam) if name in by_name]
        elif mode == "ann":
            payload["show"] = [by_name[srcAnn]] if srcAnn in by_name else []
            payload["hide"] = [by_name[srcCam]] if srcCam in by_name else []
        elif mode == "cam":
            payload["show"] = [by_name[srcCam]] if srcCam in by_name else []
            payload["hide"] = [by_name[srcAnn]] if srcAnn in by_name else []
        await context.obs.apply_scene_item_visibility(scene, payload)
        return Response("OK\n", media_type="text/plain")

    @api.get("/obs/propresenter-input/items")
    async def propresenter_input_items() -> dict:
        scene_name = context.config.integrations.obs.main_layout_scene
        items = await context.obs.get_scene_items(scene_name)
        return {"ok": True, "sceneName": scene_name, "items": [item.to_dict() for item in items]}

    @api.get("/obs/look/refresh")
    async def obs_look_refresh() -> dict:
        try:
            look_name = await context.propresenter.current_look_name()
            result = await context.obs.apply_look_rule(look_name, force=True)
            return result or {"ok": True, "skipped": True, "reason": "no_matching_rule"}
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"ok": False, "error": str(exc)})

    return api
