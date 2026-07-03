from __future__ import annotations

from production_hub.core.config.models import AppConfig, ValidationError


def validate_app_config(config: AppConfig) -> None:
    routes = [page.path for page in config.remote_pages]
    if len(routes) != len(set(routes)):
        raise ValidationError("Remote page paths must be unique")
    if config.api.lan_access_enabled and config.api.require_token_for_privileged and not config.api.access_token:
        raise ValidationError("LAN privileged access requires an access token")

