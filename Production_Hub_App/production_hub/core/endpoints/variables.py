from __future__ import annotations

import re
from typing import Any

TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.-]*)\s*\}\}")


def resolve_template(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: resolve_template(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_template(item, context) for item in value]
    if not isinstance(value, str):
        return value

    match = TOKEN_RE.fullmatch(value.strip())
    if match:
        return context.get(match.group(1), value)

    def replace(token: re.Match[str]) -> str:
        resolved = context.get(token.group(1), token.group(0))
        return str(resolved)

    return TOKEN_RE.sub(replace, value)


def context_value(context: dict[str, Any], key: str, default: Any = "") -> Any:
    if key in context:
        return context[key]
    return default

