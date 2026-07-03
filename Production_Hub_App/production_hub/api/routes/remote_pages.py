from __future__ import annotations

from fastapi import APIRouter

from production_hub.core.config.remote_pages import discover_remote_pages


def router(context) -> APIRouter:
    api = APIRouter()

    @api.get("/remote-pages")
    async def remote_pages() -> dict:
        base = context.config.api.base_url
        pages = discover_remote_pages(context.workspace_root, context.config.remote_pages)
        return {
            "items": [
                {
                    **page,
                    "local_url": f"{base}/remote/{page['path']}",
                    "lan_url": "" if not context.config.api.lan_access_enabled else f"http://<lan-ip>:{context.config.api.port}/remote/{page['path']}",
                    "status": "Enabled" if page["enabled"] else "Disabled",
                }
                for page in pages
            ]
        }

    return api
