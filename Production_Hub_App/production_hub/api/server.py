import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from production_hub.api.clicker_policy import (
    is_clicker_presentation_trigger,
    presentation_activation_disabled_detail,
    presentation_activation_enabled,
)
from production_hub.state.runtime_state import RequestRecord
from production_hub.core.security.sanitize import redact_secrets


STATIC_EXTENSIONS = {".html", ".json", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js"}


def create_app(context):
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import FileResponse, JSONResponse, Response
    except Exception as exc:  # pragma: no cover - exercised only without dependencies
        raise RuntimeError("FastAPI is required to run the embedded API server. Install requirements.txt.") from exc

    from production_hub.api.routes import admin, health, obs, presets, propresenter, remote_pages, remote_state, scoreboard

    app = FastAPI(title="Production Hub API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=context.config.api.cors_allow_origins or ["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            "X-Production-Hub-Token",
            "Access-Control-Request-Private-Network",
        ],
        allow_private_network=True,
    )

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
                        data["body"] = payload
                else:
                    data["body"] = raw.decode("utf-8", errors="replace")
        return data

    def apply_endpoint_inputs(endpoint, data: dict[str, object]) -> dict[str, object]:
        for input_def in getattr(endpoint, "inputs", []):
            name = input_def.name
            value = data.get(name)
            if name not in data or value is None or value == "":
                if input_def.default != "":
                    data[name] = input_def.default
                elif input_def.required:
                    raise HTTPException(status_code=422, detail=f"Missing required input: {name}")
            if name in data and input_def.kind == "integer":
                try:
                    data[name] = int(data[name])
                except Exception:
                    raise HTTPException(status_code=422, detail=f"Input {name} must be an integer")
            if name in data and input_def.kind == "float":
                try:
                    data[name] = float(data[name])
                except Exception:
                    raise HTTPException(status_code=422, detail=f"Input {name} must be a float")
            if name in data and input_def.kind == "bool":
                data[name] = str(data[name]).lower() in {"1", "true", "yes", "on"}
            if name in data and input_def.kind in {"integer", "float"}:
                if input_def.min_value not in {"", None} and float(data[name]) < float(input_def.min_value):
                    raise HTTPException(status_code=422, detail=f"Input {name} must be at least {input_def.min_value}")
                if input_def.max_value not in {"", None} and float(data[name]) > float(input_def.max_value):
                    raise HTTPException(status_code=422, detail=f"Input {name} must be at most {input_def.max_value}")
        return data

    def static_file_exists(asset_path: str) -> bool:
        path = (Path(context.workspace_root) / asset_path).resolve()
        root = Path(context.workspace_root).resolve()
        return path.is_file() and (path == root or root in path.parents)

    def is_remote_page_request(request: Request) -> bool:
        if request.method != "GET":
            return False
        normalized = request.url.path.strip("/")
        if normalized.startswith("remote/"):
            return True
        if normalized in page_aliases:
            return True
        suffix = Path(normalized).suffix.lower()
        if suffix in STATIC_EXTENSIONS and static_file_exists(normalized):
            return True
        if normalized and not suffix and static_file_exists(f"{normalized}.html"):
            return True
        return False

    async def configured_endpoint_response(request: Request) -> Response | None:
        if is_remote_page_request(request):
            return None
        if (
            is_clicker_presentation_trigger(request.method, request.url.path)
            and not presentation_activation_enabled(context)
        ):
            return JSONResponse(
                {"detail": presentation_activation_disabled_detail()},
                status_code=403,
            )
        seed = await dynamic_context(request, {})
        match = context.endpoint_registry.matching_endpoint(request.url.path, request.method, seed)
        if not match:
            return None
        endpoint, path_params = match
        try:
            execution_context = apply_endpoint_inputs(endpoint, await dynamic_context(request, path_params))
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        execution = await context.endpoint_executor.execute(endpoint, execution_context)
        payload = context.endpoint_executor.response_payload(endpoint, execution, execution_context)
        status_code = 200 if execution.ok else 500
        if endpoint.response.response_type == "binary":
            data = {}
            for action_result in reversed(execution.action_results):
                if action_result.data:
                    data = action_result.data
                    break
            return Response(
                data.get("body", b""),
                status_code=status_code,
                media_type=str(data.get("media_type") or endpoint.response.media_type or "application/octet-stream"),
            )
        if endpoint.response.response_type == "plain_text":
            return Response(str(payload), status_code=status_code, media_type=endpoint.response.media_type or "text/plain")
        return JSONResponse(payload, status_code=status_code)

    @app.middleware("http")
    async def record_requests(request: Request, call_next):
        request_id = uuid4().hex
        started = time.perf_counter()
        error = ""
        status_code = 500
        try:
            response = await configured_endpoint_response(request)
            if response is None:
                response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            allowed_origins = context.config.api.cors_allow_origins or []
            origin = request.headers.get("origin", "")
            if "*" in allowed_origins or origin in allowed_origins:
                response.headers["Access-Control-Allow-Origin"] = "*" if "*" in allowed_origins else origin
                response.headers["Access-Control-Allow-Private-Network"] = "true"
                vary = response.headers.get("Vary", "")
                if "*" not in allowed_origins and "origin" not in vary.lower():
                    response.headers["Vary"] = f"{vary}, Origin" if vary else "Origin"
            if request.url.path == "/clicker-presentation-activation":
                response.headers["Cache-Control"] = "no-store"
            return response
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            try:
                record = RequestRecord(
                    timestamp=datetime.now(UTC).isoformat(),
                    method=request.method,
                    route=request.url.path,
                    status_code=status_code,
                    caller_ip=request.client.host if request.client else "",
                    duration_ms=duration_ms,
                    request_id=request_id,
                    error=error,
                )
                context.runtime_state_repo.update(lambda state: state.add_request(record))
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

    @app.get("/api/debug")
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
        "current-audio": "displays/current-audio.html",
        "current-audio.html": "displays/current-audio.html",
        "pads-control": "pads-control.html",
        "pads-control.html": "pads-control.html",
        "picker": "picker.html",
        "picker.html": "picker.html",
        "ipad-control": "ipad-control.html",
        "ipad-control.html": "ipad-control.html",
        "ipad.html": "ipad-control.html",
        "debug": "debug.html",
        "debug.html": "debug.html",
        "score.html": "score.html",
    }

    @app.get("/")
    async def root_remote_page():
        return remote_file_response(page_aliases[""])

    @app.api_route("/{page_path:path}", methods=["GET", "POST"])
    async def remote_page_alias(page_path: str, request: Request):
        normalized = page_path.strip("/")
        response = await configured_endpoint_response(request)
        if response is not None:
            return response

        if request.method != "GET":
            raise HTTPException(status_code=404, detail="Not found")
        if normalized in page_aliases:
            return remote_file_response(page_aliases[normalized])
        if normalized and not Path(normalized).suffix:
            try:
                return remote_file_response(f"{normalized}.html")
            except HTTPException:
                pass
        if normalized and Path(normalized).suffix.lower() in STATIC_EXTENSIONS:
            return remote_file_response(normalized)
        raise HTTPException(status_code=404, detail="Not found")

    return app
