from __future__ import annotations

import asyncio
import base64
import json
import urllib.error

import pytest

from production_hub.core.config.defaults import build_default_config
from production_hub.core.config.models import ObsConfig, PanasonicConfig, ProPresenterConfig
from production_hub.core.health.status_models import STATUS_CONNECTED, STATUS_OFFLINE
from production_hub.integrations.obs.client import ObsClient
from production_hub.integrations.panasonic_awp.client import PanasonicAwpClient
from production_hub.integrations.propresenter.client import ProPresenterClient
from testing.harness.fake_servers import (
    ScriptedHttpServer,
    ScriptedObsResponse,
    ScriptedObsWebSocketServer,
)


SCENARIOS = {
    "propresenter": {"offline", "timeout", "malformed", "recovery"},
    "obs": {"offline", "timeout", "partial_failure", "recovery"},
    "panasonic": {"offline", "timeout", "rejected_command", "recovery"},
    "visca": {"port_conflict", "malformed", "raw", "visca_over_ip", "recovery"},
    "midi": {"missing_device", "unmapped", "mapped", "recovery"},
    "scoreboard": {"empty", "conflict", "persistence", "recovery"},
    "video": {"missing_runtime", "stale", "disconnect", "recovery"},
    "camera_tracking": {"no_subject", "dropout", "manual_override", "recovery"},
}


def test_all_integrations_have_offline_and_recovery_scenarios() -> None:
    configured = build_default_config().integrations.to_dict()
    assert set(configured) == set(SCENARIOS)
    for name, scenarios in SCENARIOS.items():
        assert "recovery" in scenarios, name
        assert scenarios & {"offline", "missing_device", "missing_runtime", "port_conflict", "empty", "no_subject"}, name


def test_health_states_distinguish_offline_from_recovered() -> None:
    assert STATUS_OFFLINE != STATUS_CONNECTED


@pytest.mark.asyncio
async def test_propresenter_real_http_contract_malformed_timeout_and_recovery() -> None:
    with ScriptedHttpServer() as server:
        server.enqueue("/v1/presentation/active", body={"presentation": {"id": {"uuid": "fixture"}}})
        server.enqueue("/v1/malformed", body=b"{not-json")
        server.enqueue("/v1/slow", body={"late": True}, delay_seconds=0.15)
        server.enqueue("/v1/recovered", body={"ok": True})
        client = ProPresenterClient(
            ProPresenterConfig(host="127.0.0.1", port=server.port, request_timeout_seconds=0.03)
        )

        payload = await client.get_json("/presentation/active")
        assert payload["presentation"]["id"]["uuid"] == "fixture"
        assert client.health().status == STATUS_CONNECTED
        assert server.requests[-1].headers["accept"] == "application/json"

        with pytest.raises(json.JSONDecodeError):
            await client.get_json("/malformed")
        assert client.health().status == STATUS_OFFLINE
        assert "Malformed ProPresenter JSON" in client.last_error

        with pytest.raises((TimeoutError, urllib.error.URLError, OSError)):
            await client.get_json("/slow")
        assert client.health().status == STATUS_OFFLINE

        assert await client.get_json("/recovered") == {"ok": True}
        assert client.health().status == STATUS_CONNECTED


@pytest.mark.asyncio
async def test_propresenter_trigger_quotes_path_segments_exactly() -> None:
    with ScriptedHttpServer() as server:
        server.enqueue("/v1/macro/Logo%20%2F%20Wide/trigger", body={"ok": True})
        client = ProPresenterClient(ProPresenterConfig(host="127.0.0.1", port=server.port))
        assert await client.trigger_macro("Logo / Wide")
        assert server.requests[-1].path == "/v1/macro/Logo%20%2F%20Wide/trigger"


@pytest.mark.asyncio
async def test_panasonic_real_http_contract_auth_rejection_timeout_and_recovery() -> None:
    with ScriptedHttpServer() as server:
        endpoint = "/cgi-bin/aw_ptz"
        server.enqueue(endpoint, body="s")
        server.enqueue(endpoint, body="ER3")
        server.enqueue(endpoint, body="s", delay_seconds=0.15)
        server.enqueue(endpoint, body="s")
        client = PanasonicAwpClient(
            PanasonicConfig(
                camera_ip=server.host_port,
                username="operator",
                password="secret",
                request_timeout_seconds=0.03,
            )
        )

        assert await client.send("#PTS5050")
        request = server.requests[-1]
        assert request.path == endpoint
        assert "cmd=%23PTS5050" in request.query
        expected = base64.b64encode(b"operator:secret").decode("ascii")
        assert request.headers["authorization"] == f"Basic {expected}"

        with pytest.raises(RuntimeError, match="Panasonic rejected"):
            await client.send("#Z75")
        assert client.health().status == STATUS_OFFLINE

        with pytest.raises((TimeoutError, urllib.error.URLError, OSError)):
            await client.send("#F55")
        assert client.health().status == STATUS_OFFLINE

        assert await client.send("#D11")
        assert client.health().status == STATUS_CONNECTED


@pytest.mark.asyncio
async def test_obs_real_websocket_contract_failure_and_automatic_reconnect() -> None:
    with ScriptedObsWebSocketServer() as server:
        client = ObsClient(
            ObsConfig(
                host="127.0.0.1",
                port=server.port,
                password="",
                automatic_reconnect=True,
                connection_timeout_seconds=0.5,
            )
        )
        assert await client.connect()
        version = await client.call("get_version")
        assert version.obs_version == "31.0.0-test"
        assert client.health().status == STATUS_CONNECTED
        assert any(request["requestType"] == "GetVersion" for request in server.requests)

        server.enqueue(
            "GetCurrentProgramScene",
            ScriptedObsResponse(success=False, code=500, comment="scripted partial failure"),
        )
        server.enqueue(
            "GetCurrentProgramScene",
            ScriptedObsResponse(data={"currentProgramSceneName": "Recovered Scene"}),
        )
        current = await client.call("get_current_program_scene")
        assert current.current_program_scene_name == "Recovered Scene"
        assert client.connected
        assert client.health().status == STATUS_CONNECTED


@pytest.mark.asyncio
async def test_obs_offline_connection_is_bounded_and_reported() -> None:
    # Bind and release a local port so the test has a deterministic connection-
    # refused target without depending on any production network address.
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    client = ObsClient(
        ObsConfig(host="127.0.0.1", port=port, connection_timeout_seconds=0.05)
    )
    assert not await asyncio.wait_for(client.connect(), timeout=1)
    assert client.health().status == STATUS_OFFLINE
    assert client.last_error
