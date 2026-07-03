from __future__ import annotations

from typing import Any

CURRENT_SCHEMA_VERSION = 1


def migrate_config_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Apply forward-only config migrations.

    The first public profile format is schema version 1, so this currently
    normalizes missing schema metadata without changing user values.
    """

    migrated = dict(data)
    migrated.setdefault("schema_version", CURRENT_SCHEMA_VERSION)
    return migrated

