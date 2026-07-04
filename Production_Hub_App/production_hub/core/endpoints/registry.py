from __future__ import annotations

import re

from production_hub.core.endpoints.models import EndpointDefinition


def _route_pattern(route: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    for match in re.finditer(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::(int|str))?\}", route):
        parts.append(re.escape(route[index : match.start()]))
        name = match.group(1)
        kind = match.group(2) or "str"
        if kind == "int":
            parts.append(fr"(?P<{name}>-?\d+)")
        else:
            parts.append(fr"(?P<{name}>[^/]+)")
        index = match.end()
    parts.append(re.escape(route[index:]))
    return re.compile("^" + "".join(parts) + "$")


class EndpointRegistry:
    def __init__(self, endpoints: list[EndpointDefinition] | None = None) -> None:
        self._by_key: dict[str, EndpointDefinition] = {}
        self._by_route: dict[str, list[EndpointDefinition]] = {}
        for endpoint in endpoints or []:
            self.register(endpoint)

    def register(self, endpoint: EndpointDefinition) -> None:
        self._by_key[endpoint.key] = endpoint
        self._by_route.setdefault(endpoint.route, [])
        self._by_route[endpoint.route] = [item for item in self._by_route[endpoint.route] if item.key != endpoint.key]
        self._by_route[endpoint.route].append(endpoint)

    def replace_all(self, endpoints: list[EndpointDefinition]) -> None:
        self._by_key.clear()
        self._by_route.clear()
        for endpoint in endpoints:
            self.register(endpoint)

    def get(self, key: str) -> EndpointDefinition | None:
        return self._by_key.get(key)

    def remove(self, key: str) -> None:
        endpoint = self._by_key.pop(key, None)
        if endpoint is None:
            return
        self._by_route[endpoint.route] = [item for item in self._by_route.get(endpoint.route, []) if item.key != key]
        if not self._by_route[endpoint.route]:
            self._by_route.pop(endpoint.route, None)

    def by_route(self, route: str) -> list[EndpointDefinition]:
        return list(self._by_route.get(route, []))

    def matches(self, path: str, method: str) -> list[tuple[EndpointDefinition, dict[str, object]]]:
        method = method.upper()
        matched: list[tuple[EndpointDefinition, dict[str, object]]] = []
        for endpoint in self._by_key.values():
            if method not in {item.upper() for item in endpoint.allowed_methods}:
                continue
            match = _route_pattern(endpoint.route).match(path)
            if not match:
                continue
            params: dict[str, object] = {}
            for key, value in match.groupdict().items():
                params[key] = int(value) if value.lstrip("-").isdigit() else value
            matched.append((endpoint, params))
        return matched

    def all(self) -> list[EndpointDefinition]:
        return list(self._by_key.values())
