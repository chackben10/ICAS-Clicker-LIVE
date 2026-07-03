from __future__ import annotations

from production_hub.core.endpoints.models import EndpointDefinition


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

    def all(self) -> list[EndpointDefinition]:
        return list(self._by_key.values())
