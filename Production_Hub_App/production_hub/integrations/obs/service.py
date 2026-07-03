from __future__ import annotations

from typing import Any

from production_hub.core.config.models import ObsConfig
from production_hub.integrations.obs.client import ObsClient
from production_hub.integrations.obs.models import ObsSceneItem
from production_hub.integrations.obs.scene_mapping import find_rule, rule_payload, rule_signature, transition_for_scene


class ObsService:
    def __init__(self, config: ObsConfig) -> None:
        self.config = config
        self.client = ObsClient(config)
        self.scene_item_cache: dict[tuple[str, str], int] = {}
        self.last_scene_items: dict[str, list[ObsSceneItem]] = {}
        self.current_scene = ""
        self.last_visibility_signature = ""

    async def connect(self) -> bool:
        return await self.client.connect()

    async def get_current_scene(self) -> str:
        data = await self.client.call("get_current_program_scene")
        scene = getattr(data, "current_program_scene_name", "") or getattr(data, "currentProgramSceneName", "")
        self.current_scene = str(scene or self.current_scene)
        return self.current_scene

    async def set_transition(self, transition_name: str, duration_ms: int | None = None) -> None:
        if transition_name:
            await self.client.call("set_current_scene_transition", transition_name)
        if duration_ms is not None:
            try:
                await self.client.call("set_current_scene_transition_duration", duration_ms)
            except Exception:
                pass

    async def set_scene(self, scene_name: str, use_policy: bool = True) -> bool:
        transition = self.config.default_transition
        duration: int | None = None
        if use_policy:
            transition, duration = transition_for_scene(self.config, scene_name, self.current_scene)
        try:
            await self.set_transition(transition, duration)
            await self.client.call("set_current_program_scene", scene_name)
            self.current_scene = scene_name
            return True
        except Exception:
            if transition == self.config.special_transition:
                await self.set_transition(self.config.fallback_transition, self.config.fallback_duration_ms)
                await self.client.call("set_current_program_scene", scene_name)
                self.current_scene = scene_name
                return True
            raise

    async def get_scene_items(self, scene_name: str | None = None) -> list[ObsSceneItem]:
        scene_name = scene_name or self.config.main_layout_scene
        data = await self.client.call("get_scene_item_list", scene_name)
        raw_items = getattr(data, "scene_items", None) or getattr(data, "sceneItems", None) or []
        items: list[ObsSceneItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raw = raw.__dict__
            item = ObsSceneItem(
                scene_item_id=int(raw.get("sceneItemId") or raw.get("scene_item_id")),
                source_name=str(raw.get("sourceName") or raw.get("source_name") or ""),
                source_uuid=str(raw.get("sourceUuid") or raw.get("source_uuid") or ""),
                source_type=str(raw.get("sourceType") or raw.get("source_type") or ""),
                input_kind=str(raw.get("inputKind") or raw.get("input_kind") or ""),
                enabled=bool(raw.get("sceneItemEnabled") or raw.get("scene_item_enabled")),
                raw=dict(raw),
            )
            items.append(item)
        self.last_scene_items[scene_name] = items
        return items

    async def set_scene_item_enabled(self, scene_name: str, scene_item_id: int, enabled: bool) -> None:
        await self.client.call("set_scene_item_enabled", scene_name, int(scene_item_id), bool(enabled))

    async def apply_scene_item_visibility(self, scene_name: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        operations: list[dict[str, Any]] = []
        for scene_item_id in payload.get("show", []):
            operations.append({"sceneItemId": int(scene_item_id), "enabled": True})
        for scene_item_id in payload.get("hide", []):
            operations.append({"sceneItemId": int(scene_item_id), "enabled": False})
        for item in payload.get("items", []):
            scene_item_id = int(item.get("sceneItemId") or item.get("id") or item.get("scene_item_id"))
            operations.append({"sceneItemId": scene_item_id, "enabled": bool(item.get("enabled", item.get("sceneItemEnabled")))})

        for operation in operations:
            await self.set_scene_item_enabled(scene_name, operation["sceneItemId"], operation["enabled"])
        return operations

    async def apply_look_rule(self, look_name: str, force: bool = False) -> dict[str, Any] | None:
        rule = find_rule(self.config, look_name)
        if not rule:
            return None
        payload = rule_payload(rule)
        signature = rule_signature(look_name, payload)
        if not force and signature == self.last_visibility_signature:
            return {"ok": True, "skipped": True, "signature": signature}
        applied = await self.apply_scene_item_visibility(str(payload["sceneName"]), payload)
        self.last_visibility_signature = signature
        return {"ok": True, "signature": signature, "applied": applied}

