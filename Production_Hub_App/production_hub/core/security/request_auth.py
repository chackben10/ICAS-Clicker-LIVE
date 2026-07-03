from __future__ import annotations

from production_hub.core.config.models import ApiServerConfig
from production_hub.core.security.tokens import constant_time_equal


def is_authorized(headers: dict[str, str], config: ApiServerConfig, privileged: bool = False) -> bool:
    if not privileged:
        return True
    if not config.require_token_for_privileged:
        return True
    if not config.access_token:
        return not config.lan_access_enabled

    auth = headers.get("authorization", "")
    bearer = "Bearer "
    if auth.startswith(bearer):
        return constant_time_equal(auth[len(bearer) :], config.access_token)
    token = headers.get("x-production-hub-token", "")
    return bool(token) and constant_time_equal(token, config.access_token)

