from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from production_hub.api.server import create_app
from production_hub.app.bootstrap import build_context
from production_hub.core.endpoints.models import ActionResult
from testing.harness.inventory import load_feature_matrix


def _route_path(path: str) -> str:
    return (
        path.replace("{source_name}", "audience")
        .replace("{asset_path}", "index.html")
        .replace("{page_path}", "missing-contract-page")
        .replace("{playlist_uuid}", "fixture-playlist")
        .replace("{item_index}", "0")
        .replace("{slide_index}", "0")
        .replace("{uuid}", "fixture-presentation")
        .replace("{index}", "0")
    )


def _request_kwargs(method: str, path: str) -> dict:
    payloads = {
        "/api/camera/automation/configure": {"mode": "off"},
        "/api/camera/automation/click-target": {"x": 0.5, "y": 0.5},
        "/auto-show": {"enabled": True},
        "/clicker-presentation-activation": {"enabled": True},
        "/presentation-focus-time": {"enabled": True},
        "/score": {"rows": []},
        "/scene/items/apply": {"sceneName": "Fixture", "items": []},
        "/preset": {"preset": "show_slides"},
        "/macro": {"macro": "Fixture"},
        "/audio/trigger": {"playlist": "Fixture", "track": "Fixture"},
    }
    params: dict[str, object] = {}
    if path == "/focus":
        params["index"] = 0
    elif path == "/thumbnail":
        params.update(uuid="fixture-presentation", index=0, tier="low")
    elif path in {"/audio/tracks"}:
        params["playlist"] = "Fixture"
    elif path in {"/scene/set", "/scene/items", "/scene/items/text"}:
        params["name" if path == "/scene/set" else "scene"] = "Fixture"
    elif path == "/set":
        params.update(scene="Fixture", inputs="1")
    kwargs: dict = {"params": params}
    if method == "POST":
        kwargs["json"] = payloads.get(path, {})
    return kwargs


def test_every_explicit_fastapi_route_has_a_contract_scenario() -> None:
    with TemporaryDirectory() as temp_dir:
        context = build_context(Path(temp_dir))

        async def successful_action(action, _inputs):
            return ActionResult(action.action_type, True, "contract fixture", {"ok": True, "items": []})

        async def empty_scene_items(_scene=None):
            return []

        for action_type in list(context.endpoint_executor.router._handlers):
            context.endpoint_executor.router._handlers[action_type] = successful_action
        context.obs.get_scene_items = empty_scene_items
        context.config.api.require_token_for_privileged = False
        app = create_app(context)
        client = TestClient(app)
        try:
            operations = [
                operation
                for definition in app.openapi()["paths"].values()
                for operation in definition.values()
            ]
            operation_ids = [operation["operationId"] for operation in operations]
            assert len(operation_ids) == len(set(operation_ids))
            schema_routes = {
                f"{method.upper()} {path}"
                for path, definition in app.openapi()["paths"].items()
                for method in definition
            }
            declared = set(load_feature_matrix()["catalogs"]["api_routes"]["features"])
            assert schema_routes == declared
            for operation in sorted(schema_routes):
                method, schema_path = operation.split(" ", 1)
                path = _route_path(schema_path)
                response = client.request(method, path, **_request_kwargs(method, schema_path))
                assert response.status_code < 500, f"{operation}: {response.status_code} {response.text}"
                assert response.headers.get("x-request-id"), operation
                unsupported = client.request("DELETE", path)
                assert unsupported.status_code in {404, 405}, operation
        finally:
            context.video.shutdown()


def test_auth_cors_logging_and_secret_redaction_contracts() -> None:
    with TemporaryDirectory() as temp_dir:
        context = build_context(Path(temp_dir))
        context.config.api.access_token = "contract-token-value"
        context.config.api.require_token_for_privileged = True
        context.config.api.lan_access_enabled = True
        context.config.api.cors_allow_origins = ["https://allowed.example"]
        context.config.integrations.obs.password = "obs-contract-password"
        client = TestClient(create_app(context))
        try:
            denied = client.post("/api/camera/automation/configure", json={"mode": "off"})
            assert denied.status_code == 401
            allowed = client.post(
                "/api/camera/automation/configure",
                json={"mode": "off"},
                headers={"X-Production-Hub-Token": "contract-token-value"},
            )
            assert allowed.status_code == 200

            cors = client.get("/health", headers={"Origin": "https://allowed.example"})
            assert cors.headers["access-control-allow-origin"] == "https://allowed.example"
            assert cors.headers["access-control-allow-private-network"] == "true"
            assert "origin" in cors.headers.get("vary", "").lower()

            debug_text = client.get("/api/debug").text
            assert "contract-token-value" not in debug_text
            assert "obs-contract-password" not in debug_text
            assert "[REDACTED]" in debug_text

            requests = context.runtime_state_repo.load().endpoint_request_history
            assert requests
            assert any(item.route == "/health" and item.request_id for item in requests)
        finally:
            context.video.shutdown()


def test_invalid_missing_conflicting_and_malformed_route_inputs() -> None:
    with TemporaryDirectory() as temp_dir:
        context = build_context(Path(temp_dir))
        client = TestClient(create_app(context))
        try:
            assert client.post("/clicker-presentation-activation", json={}).status_code == 422
            assert client.post("/presentation-focus-time", json={}).status_code == 422
            assert client.post("/api/camera/automation/configure", json={"mode": "unknown"}).status_code in {401, 422}
            assert client.get("/api/camera/tracking/not-a-source").status_code == 404
            assert client.post("/score", content=b"not-json", headers={"content-type": "application/json"}).status_code == 400
            current_revision = client.get("/score").json().get("revision", 0)
            conflict = client.post("/score", json={"expected_revision": current_revision + 10, "rows": []})
            assert conflict.status_code == 409
        finally:
            context.video.shutdown()


def test_camera_api_warms_analysis_without_granting_motion_authority() -> None:
    with TemporaryDirectory() as temp_dir:
        context = build_context(Path(temp_dir))
        context.config.api.require_token_for_privileged = False
        tracking_activity: list[tuple[bool, str]] = []
        calibration_activity: list[tuple[bool, str]] = []
        context.video.set_tracking_activity = lambda active, *, owner: tracking_activity.append((active, owner))
        context.video.set_calibration_activity = lambda active, *, owner: calibration_activity.append((active, owner))
        client = TestClient(create_app(context))
        try:
            configured = client.post("/api/camera/automation/configure", json={"mode": "click"})
            assert configured.status_code == 200
            assert configured.json()["motionAuthority"] is False
            assert tracking_activity[-1][0] is True
            assert calibration_activity[-1][0] is True

            disarmed = client.post("/api/camera/automation/disarm", json={})
            assert disarmed.status_code == 200
            assert tracking_activity[-1][0] is False
            assert calibration_activity[-1][0] is False
        finally:
            context.video.shutdown()
