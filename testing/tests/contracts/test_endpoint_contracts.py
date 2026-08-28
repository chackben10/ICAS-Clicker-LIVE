from __future__ import annotations

import re
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from production_hub.api.server import create_app
from production_hub.app.bootstrap import build_context
from production_hub.core.config.defaults import build_default_endpoints
from production_hub.core.endpoints.catalog import ACTION_SPECS
from production_hub.core.endpoints.registry import EndpointRegistry


PATH_PARAMETER = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")


def _sample_path(route: str) -> str:
    with_integer_parameters = re.sub(r"\{([^}:]+):int\}", "1", route)
    return re.sub(r"\{([^}:]+)(?::[^}]+)?\}", "example", with_integer_parameters)


def _matching_seed(endpoint) -> dict[str, object]:
    seed: dict[str, object] = {}
    for rule in endpoint.match_rules:
        if rule.operator in {"equals", "contains"}:
            seed[rule.input_name] = rule.value
        elif rule.operator == "exists":
            seed[rule.input_name] = "value"
    for input_def in endpoint.inputs:
        if input_def.name in seed:
            continue
        if input_def.default != "":
            seed[input_def.name] = input_def.default
        elif input_def.required:
            seed[input_def.name] = 1 if input_def.kind in {"integer", "float"} else True if input_def.kind == "bool" else "example"
    return seed


def test_every_default_endpoint_has_a_valid_contract() -> None:
    endpoints = build_default_endpoints()
    known_actions = {item.action_type for item in ACTION_SPECS} | {"delay"}
    assert len({item.key for item in endpoints}) == len(endpoints)
    registry = EndpointRegistry(endpoints)

    for endpoint in endpoints:
        assert endpoint.route.startswith("/")
        assert endpoint.allowed_methods
        assert set(endpoint.allowed_methods) <= {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
        assert len({item.name for item in endpoint.inputs}) == len(endpoint.inputs)
        assert {item.action_type for item in endpoint.actions} <= known_actions
        path_inputs = set(PATH_PARAMETER.findall(endpoint.route))
        declared_inputs = {item.name for item in endpoint.inputs}
        assert path_inputs <= declared_inputs, endpoint.key
        method = endpoint.allowed_methods[0]
        match = registry.matching_endpoint(_sample_path(endpoint.route), method, _matching_seed(endpoint))
        assert match is not None, endpoint.key
        assert match[0].key == endpoint.key


def test_endpoint_validation_rejects_bad_inputs() -> None:
    with TemporaryDirectory() as temp_dir:
        context = build_context(Path(temp_dir))
        client = TestClient(create_app(context))
        try:
            assert client.get("/focus", params={"index": "not-an-integer"}).status_code == 422
            assert client.post("/camera/preset", json={}).status_code == 422
            assert client.get("/camera/preset/not-an-integer").status_code == 404
            assert client.put("/health").status_code == 405
        finally:
            context.video.shutdown()
