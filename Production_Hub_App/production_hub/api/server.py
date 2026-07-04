import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from production_hub.state.runtime_state import RequestRecord
from production_hub.core.security.sanitize import redact_secrets


def create_app(context):
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import FileResponse
    except Exception as exc:  # pragma: no cover - exercised only without dependencies
        raise RuntimeError("FastAPI is required to run the embedded API server. Install requirements.txt.") from exc

    from production_hub.api.routes import admin, health, obs, presets, propresenter, remote_pages, remote_state, scoreboard

    app = FastAPI(title="Production Hub API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=context.config.api.cors_allow_origins or ["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Production-Hub-Token"],
    )

    @app.middleware("http")
    async def record_requests(request: Request, call_next):
        request_id = uuid4().hex
        started = time.perf_counter()
        error = ""
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            try:
                state = context.runtime_state_repo.load()
                state.add_request(
                    RequestRecord(
                        timestamp=datetime.now(UTC).isoformat(),
                        method=request.method,
                        route=request.url.path,
                        status_code=status_code,
                        caller_ip=request.client.host if request.client else "",
                        duration_ms=duration_ms,
                        request_id=request_id,
                        error=error,
                    )
                )
                context.runtime_state_repo.save(state)
            except Exception:
                pass

    for route_factory in (
        health.router,
        propresenter.router,
        presets.router,
        scoreboard.router,
        obs.router,
        remote_state.router,
        remote_pages.router,
        admin.router,
    ):
        app.include_router(route_factory(context))

    @app.get("/debug")
    async def debug() -> dict:
        return {
            "config": redact_secrets(context.config.to_dict()),
            "obs": {
                "connected": context.obs.client.connected,
                "lastError": context.obs.client.last_error,
                "currentScene": context.obs.current_scene,
                "sceneItems": {
                    scene: [item.to_dict() for item in items] for scene, items in context.obs.last_scene_items.items()
                },
            },
            "runtime": context.runtime_state_repo.load().to_dict(),
        }

    def remote_file_response(asset_path: str):
        path = (Path(context.workspace_root) / asset_path).resolve()
        root = Path(context.workspace_root).resolve()
        if not path.is_file() or root not in path.parents:
            raise HTTPException(status_code=404, detail="Remote page not found")
        return FileResponse(path)

    @app.get("/remote/{asset_path:path}")
    async def remote_asset(asset_path: str):
        return remote_file_response(asset_path)

    page_aliases = {
        "": "index.html",
        "index": "index.html",
        "index.html": "index.html",
        "control": "control.html",
        "control.html": "control.html",
        "pads-control": "pads-control.html",
        "pads-control.html": "pads-control.html",
        "picker": "picker.html",
        "picker.html": "picker.html",
        "ipad-control": "ipad-control.html",
        "ipad-control.html": "ipad-control.html",
        "debug.html": "debug.html",
        "score.html": "score.html",
    }

    @app.get("/")
    async def root_remote_page():
        return remote_file_response(page_aliases[""])

    async def dynamic_context(request: Request, path_params: dict[str, object]) -> dict[str, object]:
        data: dict[str, object] = {
            "path": request.url.path,
            "method": request.method,
            **path_params,
        }
        for key, value in request.query_params.items():
            data[key] = value
        if request.method in {"POST", "PUT", "PATCH"}:
            raw = await request.body()
            if raw:
                content_type = request.headers.get("content-type", "")
                if "application/json" in content_type:
                    payload = json.loads(raw.decode("utf-8"))
                    if isinstance(payload, dict):
                        data.update(payload)
                else:
                    data["body"] = raw.decode("utf-8", errors="replace")
        return data

    @app.api_route("/{page_path:path}", methods=["GET", "POST"])
    async def remote_page_alias(page_path: str, request: Request):
        normalized = page_path.strip("/")
        dynamic_matches = context.endpoint_registry.matches("/" + normalized, request.method)
        if dynamic_matches:
            endpoint, path_params = dynamic_matches[0]
            execution = await context.endpoint_executor.execute(endpoint, await dynamic_context(request, path_params))
            return execution.to_dict()

        if request.method != "GET":
            raise HTTPException(status_code=404, detail="Not found")
        if normalized in page_aliases:
            return remote_file_response(page_aliases[normalized])
        if normalized and Path(normalized).suffix.lower() in {".html", ".json", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"}:
            return remote_file_response(normalized)
        raise HTTPException(status_code=404, detail="Not found")

    return app
