from __future__ import annotations

import asyncio
import copy
import json
import re
import time
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from production_hub.core.config.models import (
    InputListCell,
    InputListColumn,
    InputListDefinition,
    InputListItem,
    InputListObjectField,
    InputListRow,
)


def column(key: str, title: str, data_type: str = "string", role: str = "") -> InputListColumn:
    return InputListColumn(key, title, data_type, role)


def static_cell(value: object = "") -> InputListCell:
    return InputListCell("static", value)


def polled_cell(url: str, json_path: str, preview: object = "") -> InputListCell:
    return InputListCell(
        mode="polled",
        value=preview,
        url=url,
        json_path=json_path,
        preview=preview_text(preview),
    )


def polled_dictionary_cell(
    url: str,
    json_key_path: str,
    json_value_path: str,
    preview: dict[str, Any] | None = None,
) -> InputListCell:
    value = dict(preview or {})
    return InputListCell(
        mode="polled",
        value=value,
        url=url,
        preview=preview_text(value),
        json_key_path=json_key_path,
        json_value_path=json_value_path,
    )


def song_object_fields() -> list[InputListObjectField]:
    return [
        InputListObjectField("name", json_path="name"),
        InputListObjectField("uuid", json_path="uuid"),
        InputListObjectField(
            "lyrics",
            source="request",
            json_path="presentation.groups[].slides[].text",
            url_template="v1/presentation/{uuid}",
            result_mode="join",
            separator=" ",
            normalize_whitespace=True,
            refresh_seconds=21600,
        ),
    ]


def polled_object_array_cell(
    url: str,
    items_json_path: str,
    fields: list[InputListObjectField],
    preview: list[dict[str, Any]] | None = None,
    *,
    identity_field: str = "uuid",
    concurrency: int = 4,
) -> InputListCell:
    value = [dict(item) for item in (preview or [])]
    return InputListCell(
        mode="polled",
        value=value,
        url=url,
        json_path=items_json_path,
        preview=preview_text(value),
        object_fields=fields,
        object_identity_field=identity_field,
        object_concurrency=concurrency,
    )


def row(enabled: bool, **cells: InputListCell) -> InputListRow:
    return InputListRow(enabled, cells)


def preview_text(value: object, limit: int = 80) -> str:
    if isinstance(value, dict):
        text = ", ".join(f"{key}: {item}" for key, item in value.items())
    elif isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        names = [str(item.get("name") or item.get("label") or "").strip() for item in value]
        names = [name for name in names if name]
        sample = ", ".join(names[:3])
        text = f"{len(value)} objects" + (f": {sample}" if sample else "")
        if len(names) > 3:
            text += ", …"
    elif isinstance(value, list):
        text = ", ".join(str(item) for item in value)
    else:
        text = str(value if value is not None else "")
    return text if len(text) <= limit else f"{text[: limit - 1]}..."


def audio_track_preview(playlist: str) -> list[str]:
    notes = ("A", "A#", "B", "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#")
    if playlist == "Major Pads":
        return [f"{note} Major Pads" for note in notes]
    if playlist == "Minor Pads":
        return [f"{note} Minor Pads" for note in notes]
    if playlist == "Neutral Pads":
        return [f"{note} Neutral Pads" for note in notes]
    return []


def default_song_library_list() -> InputListDefinition:
    return InputListDefinition(
        key="song_library",
        name="Song Library",
        description="ProPresenter song titles, UUIDs, and searchable lyrics used by the clicker app.",
        polling_rate_seconds=3600,
        columns=[
            column("library_name", "Library Name", "string", "label"),
            column("uuid", "UUID", "string", "value"),
            column("songs", "Songs", "array_object"),
        ],
        rows=[
            row(
                True,
                library_name=static_cell("Malayalam Songs"),
                uuid=static_cell(""),
                songs=polled_object_array_cell(
                    "v1/library/Malayalam%20Songs",
                    "items[]",
                    song_object_fields(),
                    concurrency=6,
                ),
            ),
            row(
                False,
                library_name=static_cell("English Songs"),
                uuid=static_cell(""),
                songs=polled_object_array_cell(
                    "v1/library/English%20Songs",
                    "items[]",
                    song_object_fields(),
                    concurrency=6,
                ),
            ),
        ],
    )


def row_cell(row_def: InputListRow, key: str) -> InputListCell:
    return row_def.cells.get(key, InputListCell())


def display_cell(cell: InputListCell, max_lines: int = 2) -> str:
    value = preview_text(cell.value)
    if cell.mode == "polled":
        parts = [cell.url or "Polled request not configured"]
        if value:
            parts.append(value)
        return "\n".join(parts[:max_lines])
    return value


def list_items(definition: InputListDefinition) -> list[InputListItem]:
    label_column = next((item.key for item in definition.columns if item.role == "label"), "")
    value_column = next((item.key for item in definition.columns if item.role == "value"), label_column)
    if not label_column and definition.columns:
        label_column = definition.columns[0].key
        value_column = label_column
    items: list[InputListItem] = []
    for row_def in definition.rows:
        label = row_cell(row_def, label_column).value
        value = row_cell(row_def, value_column).value
        if isinstance(label, dict):
            value_map = value if isinstance(value, dict) else label
            for dictionary_key in label:
                label_text = str(dictionary_key or "").strip()
                if not label_text:
                    continue
                dictionary_value = value_map.get(dictionary_key, label_text)
                items.append(
                    InputListItem(
                        label_text,
                        str(dictionary_value if dictionary_value is not None and dictionary_value != "" else label_text),
                        enabled=row_def.enabled,
                    )
                )
            continue
        elif isinstance(label, list) and any(isinstance(item, dict) for item in label):
            for object_item in label:
                if not isinstance(object_item, dict):
                    continue
                label_text = str(
                    object_item.get("name")
                    or object_item.get("label")
                    or object_item.get("title")
                    or ""
                ).strip()
                if not label_text:
                    continue
                object_value = object_item.get("uuid", object_item.get("UUID", object_item.get("value", label_text)))
                items.append(
                    InputListItem(
                        label_text,
                        str(object_value if not _is_empty(object_value) else label_text),
                        enabled=row_def.enabled,
                    )
                )
            continue
        elif isinstance(label, list):
            label = ", ".join(str(item) for item in label)
        if isinstance(value, dict):
            value = ", ".join(str(item) for item in value.values())
        elif isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        label_text = str(label or "").strip()
        if not label_text:
            continue
        items.append(
            InputListItem(
                label_text,
                str(value if value is not None and value != "" else label_text),
                enabled=row_def.enabled,
            )
        )
    if items:
        return items
    return list(definition.items)


def default_input_lists(config: Any) -> list[InputListDefinition]:
    propresenter = config.integrations.propresenter
    obs = config.integrations.obs
    base_url = str(config.api.base_url).rstrip("/")
    return [
        InputListDefinition(
            "audio_playlists",
            "Audio Playlists",
            description="ProPresenter audio playlists. Track lists are polled from ProPresenter through Production Hub.",
            polling_rate_seconds=propresenter.audio.cache_ttl_seconds,
            columns=[
                column("playlist_name", "Playlist Name", "string", "label"),
                column("tracks", "Tracks", "array_string"),
            ],
            rows=[
                row(
                    True,
                    playlist_name=static_cell(name),
                    tracks=polled_cell(
                        f"{base_url}/api/propresenter/audio/tracks?playlist={name}",
                        "items[]",
                        audio_track_preview(name),
                    ),
                )
                for name in propresenter.audio.playlists
            ],
        ),
        InputListDefinition(
            "service_logos",
            "Service Logos",
            description="Configured ProPresenter service logo presentations.",
            columns=[
                column("name", "Name", "string", "label"),
                column("uuid", "UUID", "string", "value"),
            ],
            rows=[row(True, name=static_cell(item.name), uuid=static_cell(item.uuid)) for item in propresenter.service_logos],
        ),
        InputListDefinition(
            "macros",
            "Macros",
            description="Allow-listed ProPresenter macros.",
            columns=[column("macro", "Macro", "string", "label")],
            rows=[row(True, macro=static_cell(item.macro_name)) for item in propresenter.macros],
        ),
        InputListDefinition(
            "obs_looks",
            "OBS Look Rules",
            description="Configured OBS source visibility rules.",
            columns=[
                column("macro", "Macro", "string", "label"),
                column("enabled_sources", "Enabled Sources", "array_int"),
            ],
            rows=[
                row(True, macro=static_cell(item.look_name), enabled_sources=static_cell(item.show_ids))
                for item in obs.look_rules
            ],
        ),
        InputListDefinition(
            "obs_scenes",
            "OBS Scenes",
            description="Known OBS scenes.",
            columns=[column("scene", "Scene", "string", "label")],
            rows=[row(True, scene=static_cell(name)) for name in obs.known_scenes],
        ),
        default_song_library_list(),
    ]


def custom_input_lists(config: Any) -> list[InputListDefinition]:
    lists = [InputListDefinition.from_dict(item.to_dict() if hasattr(item, "to_dict") else item) for item in config.ui.input_lists]
    for item in lists:
        item.builtin = False
    legacy_keys = {item.key for item in lists}
    for key, values in sorted(config.ui.endpoint_option_lists.items()):
        if key in legacy_keys:
            continue
        lists.append(
            InputListDefinition(
                key=key,
                name=key.replace("_", " ").title(),
                columns=[column("value", "Value", "string", "label")],
                rows=[row(True, value=static_cell(value)) for value in values],
            )
        )
    return lists


def ensure_default_input_lists(config: Any) -> bool:
    changed = False
    if not getattr(config.ui, "input_lists_initialized", False):
        existing = custom_input_lists(config)
        existing_keys = {item.key for item in existing}
        seeded = [*existing]
        for item in default_input_lists(config):
            if item.key not in existing_keys:
                seeded.append(item)
        for item in seeded:
            item.builtin = False
        config.ui.input_lists = seeded
        config.ui.input_lists_initialized = True
        changed = True
    return ensure_song_library_objects(config) or changed


def _collection_path(path: str, fallback_field: str) -> tuple[str, str]:
    parts = [part for part in str(path or "").split(".") if part]
    for index, part in enumerate(parts):
        if part.endswith("[]"):
            root = ".".join(parts[: index + 1])
            relative = ".".join(parts[index + 1 :]) or fallback_field
            return root, relative
    return "items[]", fallback_field


def _song_objects(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [
            {"name": str(name), "uuid": str(uuid or ""), "lyrics": ""}
            for name, uuid in value.items()
            if str(name).strip()
        ]
    if not isinstance(value, list):
        return []
    objects: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            record = dict(item)
            if "uuid" not in record and "UUID" in record:
                record["uuid"] = record.pop("UUID")
            record.setdefault("name", "")
            record.setdefault("uuid", "")
            record.setdefault("lyrics", "")
            if str(record.get("name") or "").strip():
                objects.append(record)
            continue
        name = str(item or "").strip()
        if name:
            objects.append({"name": name, "uuid": "", "lyrics": ""})
    return objects


def ensure_song_library_objects(config: Any) -> bool:
    """Migrate Song Library rows to configurable name/UUID/lyrics objects."""
    changed = False
    for index, raw_definition in enumerate(list(config.ui.input_lists)):
        definition = InputListDefinition.from_dict(
            raw_definition.to_dict() if hasattr(raw_definition, "to_dict") else raw_definition
        )
        if definition.key != "song_library":
            continue
        songs_column = next((item for item in definition.columns if item.key == "songs"), None)
        if songs_column is None:
            continue
        migrate_schema = songs_column.data_type != "array_object"
        if migrate_schema:
            songs_column.data_type = "array_object"
            changed = True
        for row_def in definition.rows:
            cell = row_def.cells.get("songs")
            if cell is None:
                continue
            library_name = str(row_def.cells.get("library_name", InputListCell()).value or "").strip()
            if library_name.casefold() == "english songs" and cell.url == "v1/library/English%Songs":
                cell.url = "v1/library/English%20Songs"
                changed = True
            if migrate_schema or isinstance(cell.value, dict) or (
                isinstance(cell.value, list) and any(not isinstance(item, dict) for item in cell.value)
            ):
                migrated_value = _song_objects(cell.value)
                if cell.value != migrated_value:
                    cell.value = migrated_value
                    cell.preview = preview_text(cell.value)
                    changed = True
            elif isinstance(cell.value, list):
                normalized_value = _song_objects(cell.value)
                if cell.value != normalized_value:
                    cell.value = normalized_value
                    cell.preview = preview_text(cell.value)
                    changed = True
            if migrate_schema or not cell.object_fields:
                key_path = cell.json_key_path or cell.json_path or "items[].name"
                value_path = cell.json_value_path or "items[].uuid"
                key_root, name_path = _collection_path(key_path, "name")
                value_root, uuid_path = _collection_path(value_path, "uuid")
                cell.json_path = key_root if key_root == value_root else "items[]"
                fields = song_object_fields()
                fields[0].json_path = name_path
                fields[1].json_path = uuid_path
                cell.object_fields = fields
                cell.object_identity_field = "uuid"
                cell.object_concurrency = 6
                cell.object_enrichment_last_polled = {}
                changed = True
            if cell.mode != "polled":
                continue
            if cell.json_key_path or cell.json_value_path:
                cell.json_key_path = ""
                cell.json_value_path = ""
                changed = True
        if changed:
            definition.builtin = False
            config.ui.input_lists[index] = definition
        break
    return changed


def ensure_song_library_dictionary(config: Any) -> bool:
    """Backward-compatible alias for the pre-object-array migration hook."""
    return ensure_song_library_objects(config)


def all_input_lists(config: Any) -> list[InputListDefinition]:
    return custom_input_lists(config)


def input_list_by_key(config: Any, key: str) -> InputListDefinition | None:
    for item in config.ui.input_lists:
        if isinstance(item, InputListDefinition):
            if item.key == key:
                return item
            continue
        if str(item.get("key", "")) == key:
            return InputListDefinition.from_dict(item)
    values = config.ui.endpoint_option_lists.get(key)
    if values is not None:
        return InputListDefinition(
            key=key,
            name=key.replace("_", " ").title(),
            columns=[column("value", "Value", "string", "label")],
            rows=[row(True, value=static_cell(value)) for value in values],
        )
    return None


def input_list_choices(config: Any, key: str) -> list[str]:
    item = input_list_by_key(config, key)
    if item is None:
        return []
    choices = []
    for list_item in list_items(item):
        if not list_item.enabled:
            continue
        if list_item.value and list_item.value != list_item.label:
            choices.append(f"{list_item.label} | {list_item.value}")
        else:
            choices.append(list_item.label)
    return choices


def normalize_list_choice(value: str) -> str:
    if "|" in value:
        return value.rsplit("|", 1)[-1].strip()
    return value.strip()


def source_labels(config: Any) -> list[tuple[str, str]]:
    return [(item.key, item.name) for item in all_input_lists(config)]


def _json_path(data: Any, path: str) -> Any:
    parts = [item for item in str(path or "").split(".") if item]
    if not parts:
        return data
    nodes = [data]
    plural = isinstance(data, list)
    for part in parts:
        expand = part.endswith("[]")
        key = part[:-2] if expand else part
        next_nodes: list[Any] = []
        for node in nodes:
            candidates = node if isinstance(node, list) else [node]
            if isinstance(node, list):
                plural = True
            for candidate in candidates:
                if key:
                    if not isinstance(candidate, dict) or key not in candidate:
                        continue
                    value = candidate[key]
                else:
                    value = candidate
                if expand:
                    plural = True
                    if isinstance(value, list):
                        next_nodes.extend(value)
                else:
                    next_nodes.append(value)
        nodes = next_nodes
        if not nodes:
            return [] if plural or expand else None
    return nodes if plural else nodes[0]


def _flatten_values(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return [value]
    flattened: list[Any] = []
    for item in value:
        if isinstance(item, list):
            flattened.extend(_flatten_values(item))
        else:
            flattened.append(item)
    return flattened


def _coerce_object_field(field: InputListObjectField, raw_value: Any) -> Any:
    values = _flatten_values(raw_value)
    if field.result_mode == "join":
        value: Any = field.separator.join(str(item) for item in values if item is not None)
    elif field.result_mode == "all":
        value = [item for item in values if item is not None]
    else:
        value = values[0] if values and values[0] is not None else ""

    if field.normalize_whitespace:
        if isinstance(value, list):
            value = [re.sub(r"\s+", " ", str(item)).strip() for item in value]
        else:
            value = re.sub(r"\s+", " ", str(value)).strip()

    if field.data_type == "json":
        return value
    if field.data_type == "array_string":
        return [str(item) for item in _flatten_values(value) if item is not None]
    if field.data_type == "array_int":
        converted = []
        for item in _flatten_values(value):
            try:
                converted.append(int(item))
            except (TypeError, ValueError):
                continue
        return converted
    if field.data_type == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    if field.data_type == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    if field.data_type == "bool":
        return value if isinstance(value, bool) else str(value).strip().casefold() in {"1", "true", "yes", "on"}
    return str(value if value is not None else "")


def _empty_object_field(field: InputListObjectField) -> Any:
    if field.data_type in {"array_string", "array_int"} or field.result_mode == "all":
        return []
    if field.data_type == "int":
        return 0
    if field.data_type == "float":
        return 0.0
    if field.data_type == "bool":
        return False
    return ""


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


_OBJECT_URL_FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _object_request_url(template: str, record: dict[str, Any]) -> str:
    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in record or _is_empty(record[key]):
            missing.add(key)
            return ""
        return quote(str(record[key]), safe="")

    rendered = _OBJECT_URL_FIELD.sub(replace, str(template or "").strip())
    if missing:
        fields = ", ".join(sorted(missing))
        raise ValueError(f"Object request URL requires missing field(s): {fields}")
    if not rendered:
        raise ValueError("Object request URL cannot be empty")
    return rendered


def _record_identity(record: dict[str, Any], identity_field: str) -> str:
    identity = str(record.get(identity_field) or "").strip()
    if identity:
        return identity
    return str(record.get("name") or record.get("label") or "").strip()


async def _poll_object_array_cell(
    context: Any,
    cell: InputListCell,
    data: Any,
    progress_callback: Callable[[list[dict[str, Any]], dict[str, float]], None] | None = None,
    active_callback: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    if active_callback is not None and not active_callback():
        raise _InputListRowDisabled
    items = _json_path(data, cell.json_path) if cell.json_path else data
    if not isinstance(items, list):
        raise ValueError("Object-array polling items JSON path must return an array")
    field_keys = [field.key for field in cell.object_fields]
    if len(field_keys) != len(set(field_keys)):
        raise ValueError("Object-array field names must be unique")

    base_fields = [field for field in cell.object_fields if field.source == "base"]
    request_fields = [field for field in cell.object_fields if field.source == "request"]
    records: list[dict[str, Any]] = []
    for item in items:
        if not base_fields and isinstance(item, dict):
            record = dict(item)
        else:
            record = {
                field.key: _coerce_object_field(field, _json_path(item, field.json_path) if field.json_path else item)
                for field in base_fields
            }
        if record and any(not _is_empty(value) for value in record.values()):
            records.append(record)

    previous_records = cell.value if isinstance(cell.value, list) else []
    previous_by_identity = {
        _record_identity(record, cell.object_identity_field): record
        for record in previous_records
        if isinstance(record, dict) and _record_identity(record, cell.object_identity_field)
    }
    previous_identities = set(previous_by_identity)
    for record in records:
        previous = previous_by_identity.get(_record_identity(record, cell.object_identity_field), {})
        for field in request_fields:
            if field.key in previous:
                record[field.key] = previous[field.key]

    request_groups: dict[str, list[InputListObjectField]] = {}
    for field in request_fields:
        if not field.url_template:
            raise ValueError(f"Request-backed object field {field.key!r} needs a request URL template")
        request_groups.setdefault(field.url_template, []).append(field)

    last_polled = dict(cell.object_enrichment_last_polled)
    for record in records:
        for field in request_fields:
            record.setdefault(field.key, _empty_object_field(field))
    if active_callback is not None and not active_callback():
        raise _InputListRowDisabled
    if progress_callback is not None:
        progress_callback(records, last_polled)

    now = time.time()
    semaphore = asyncio.Semaphore(cell.object_concurrency)
    for url_template, fields in request_groups.items():
        full_refresh = any(
            field.refresh_seconds <= 0
            or last_polled.get(field.key, 0) <= 0
            or now - last_polled.get(field.key, 0) >= field.refresh_seconds
            for field in fields
        )
        targets = [
            (index, record)
            for index, record in enumerate(records)
            if full_refresh
            or _record_identity(record, cell.object_identity_field) not in previous_identities
            or any(field.key not in record or _is_empty(record[field.key]) for field in fields)
        ]
        rendered_targets: list[tuple[int, dict[str, Any], str]] = []
        failures = 0
        first_error = ""
        for index, record in targets:
            try:
                rendered_targets.append((index, record, _object_request_url(url_template, record)))
            except ValueError as exc:
                failures += 1
                first_error = first_error or str(exc)

        async def request_one(url: str) -> Any:
            async with semaphore:
                return await _fetch_json(context, url)

        successes = 0
        batch_size = max(8, cell.object_concurrency * 4)
        for offset in range(0, len(rendered_targets), batch_size):
            if active_callback is not None and not active_callback():
                raise _InputListRowDisabled
            batch = rendered_targets[offset : offset + batch_size]
            results = await asyncio.gather(
                *(request_one(url) for _index, _record, url in batch),
                return_exceptions=True,
            )
            if active_callback is not None and not active_callback():
                raise _InputListRowDisabled
            for (index, _record, _url), result in zip(batch, results):
                if isinstance(result, BaseException):
                    failures += 1
                    first_error = first_error or str(result)
                    continue
                updates: dict[str, Any] = {}
                missing_fields: list[str] = []
                for field in fields:
                    raw_value = _json_path(result, field.json_path) if field.json_path else result
                    field_value = _coerce_object_field(field, raw_value)
                    if _is_empty(field_value):
                        missing_fields.append(field.key)
                    else:
                        updates[field.key] = field_value
                records[index].update(updates)
                if missing_fields:
                    failures += 1
                    first_error = first_error or f"Response did not contain: {', '.join(missing_fields)}"
                    continue
                successes += 1
            if progress_callback is not None:
                progress_callback(records, last_polled)
        if successes:
            for field in fields:
                last_polled[field.key] = now
        if failures and hasattr(context, "logger"):
            context.logger.warning(
                "input_list_enrichment_partial_failure",
                "Some object-array enrichment requests failed",
                url_template=url_template,
                failed=failures,
                total=len(targets),
                error=first_error,
            )

    for record in records:
        for field in request_fields:
            record.setdefault(field.key, _empty_object_field(field))
    return records, last_polled


async def _fetch_json(context: Any, url: str) -> Any:
    target = str(url or "").strip()
    if not target:
        return {}
    if target.startswith("http://") or target.startswith("https://"):
        def request() -> Any:
            with urllib.request.urlopen(target, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))

        return await asyncio.to_thread(request)
    path = target.lstrip("/")
    if path.startswith("v1/"):
        path = path[3:]
    return await context.propresenter.client.get_json(path)


class _InputListRowDisabled(Exception):
    """Stop a row poll when the live row is disabled while work is in progress."""


async def poll_input_list_definition(
    context: Any,
    definition: InputListDefinition,
    row_indices: set[int] | None = None,
    progress_callback: Callable[[InputListDefinition, int, str], None] | None = None,
    row_active_callback: Callable[[int], bool] | None = None,
) -> bool:
    changed = False
    column_types = {item.key: item.data_type for item in definition.columns}
    for row_index, row_def in enumerate(definition.rows):
        if row_indices is not None and row_index not in row_indices:
            continue
        if not row_def.enabled:
            continue
        for column_key, cell in row_def.cells.items():
            if cell.mode != "polled":
                continue
            if row_active_callback is not None and not row_active_callback(row_index):
                break
            data = await _fetch_json(context, cell.url)
            if row_active_callback is not None and not row_active_callback(row_index):
                break
            data_type = column_types.get(column_key)
            if data_type == "array_object":
                old_value = cell.value
                old_last_polled = dict(cell.object_enrichment_last_polled)

                def object_progress(records: list[dict[str, Any]], last_polled: dict[str, float]) -> None:
                    cell.value = records
                    cell.preview = preview_text(records)
                    cell.object_enrichment_last_polled = dict(last_polled)
                    if progress_callback is not None:
                        progress_callback(definition, row_index, column_key)

                try:
                    value, last_polled = await _poll_object_array_cell(
                        context,
                        cell,
                        data,
                        object_progress,
                        (
                            (lambda row_index=row_index: row_active_callback(row_index))
                            if row_active_callback is not None
                            else None
                        ),
                    )
                except _InputListRowDisabled:
                    break
                cell.object_enrichment_last_polled = last_polled
                if old_last_polled != last_polled:
                    changed = True
                if old_value != value:
                    changed = True
            elif data_type == "dictionary":
                key_path = cell.json_key_path or cell.json_path
                value_path = cell.json_value_path
                if key_path and value_path:
                    keys = _json_path(data, key_path)
                    values = _json_path(data, value_path)
                    if not isinstance(keys, list) or not isinstance(values, list):
                        raise ValueError(
                            f"Dictionary polling paths for {definition.name}.{column_key} must both return arrays"
                        )
                    if len(keys) != len(values):
                        raise ValueError(
                            f"Dictionary polling paths for {definition.name}.{column_key} returned different lengths"
                        )
                    value = {
                        str(key): item
                        for key, item in zip(keys, values)
                        if key is not None and str(key).strip()
                    }
                else:
                    value = _json_path(data, cell.json_path) if cell.json_path else data
                    if not isinstance(value, dict):
                        raise ValueError(
                            f"Dictionary polling for {definition.name}.{column_key} requires key/value paths or an object"
                        )
            else:
                value = _json_path(data, cell.json_path) if cell.json_path else data
            if value is None:
                value = []
            if cell.value != value:
                cell.value = value
                cell.preview = preview_text(value)
                changed = True
    return changed


def _cell_poll_signature(cell: InputListCell) -> str:
    data = cell.to_dict()
    for key in ("value", "preview", "object_enrichment_last_polled"):
        data.pop(key, None)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _merge_polled_values(
    live_definition: InputListDefinition,
    polled_definition: InputListDefinition,
    row_indices: set[int] | None = None,
) -> tuple[InputListDefinition, bool]:
    merged = InputListDefinition.from_dict(live_definition.to_dict())
    changed = False
    for row_index, polled_row in enumerate(polled_definition.rows):
        if row_indices is not None and row_index not in row_indices:
            continue
        if row_index >= len(merged.rows):
            continue
        live_row = merged.rows[row_index]
        if not live_row.enabled:
            continue
        for column_key, polled_cell in polled_row.cells.items():
            live_cell = live_row.cells.get(column_key)
            if live_cell is None or polled_cell.mode != "polled" or live_cell.mode != "polled":
                continue
            if _cell_poll_signature(live_cell) != _cell_poll_signature(polled_cell):
                continue
            if (
                live_cell.value == polled_cell.value
                and live_cell.preview == polled_cell.preview
                and live_cell.object_enrichment_last_polled == polled_cell.object_enrichment_last_polled
            ):
                continue
            live_cell.value = copy.deepcopy(polled_cell.value)
            live_cell.preview = polled_cell.preview
            live_cell.object_enrichment_last_polled = dict(polled_cell.object_enrichment_last_polled)
            changed = True
    return merged, changed


def _definition_from_config(value: Any) -> InputListDefinition:
    return InputListDefinition.from_dict(value.to_dict() if hasattr(value, "to_dict") else value)


def _find_input_list_index(config: Any, key: str) -> int:
    for index, item in enumerate(config.ui.input_lists):
        item_key = item.key if isinstance(item, InputListDefinition) else str(item.get("key", ""))
        if item_key == key:
            return index
    return -1


def _input_list_row_is_enabled(context: Any, key: str, row_index: int) -> bool:
    live_index = _find_input_list_index(context.config, key)
    if live_index < 0:
        return False
    raw_definition = context.config.ui.input_lists[live_index]
    if isinstance(raw_definition, InputListDefinition):
        return 0 <= row_index < len(raw_definition.rows) and raw_definition.rows[row_index].enabled
    rows = raw_definition.get("rows", []) if isinstance(raw_definition, dict) else []
    if row_index < 0 or row_index >= len(rows):
        return False
    raw_row = rows[row_index]
    if isinstance(raw_row, InputListRow):
        return raw_row.enabled
    return bool(raw_row.get("enabled", True)) if isinstance(raw_row, dict) else False


def _save_polled_config(context: Any) -> None:
    save = getattr(context.config_repository, "save_runtime_app_config", None)
    if callable(save):
        save(context.config)
    else:
        context.config_repository.save_app_config(context.config)


def _persist_poll_progress(
    context: Any,
    key: str,
    definition: InputListDefinition,
    row_index: int,
) -> None:
    live_index = _find_input_list_index(context.config, key)
    if live_index < 0:
        return
    live_definition = _definition_from_config(context.config.ui.input_lists[live_index])
    merged, changed = _merge_polled_values(live_definition, definition, {row_index})
    if changed:
        context.config.ui.input_lists[live_index] = merged
        _save_polled_config(context)


async def poll_input_list_by_key(context: Any, key: str) -> bool:
    index = _find_input_list_index(context.config, key)
    if index < 0:
        return False
    definition = _definition_from_config(context.config.ui.input_lists[index])
    if not await poll_input_list_definition(
        context,
        definition,
        progress_callback=lambda updated, row_index, _column: _persist_poll_progress(
            context, key, updated, row_index
        ),
        row_active_callback=lambda row_index: _input_list_row_is_enabled(context, key, row_index),
    ):
        return False
    live_index = _find_input_list_index(context.config, key)
    if live_index < 0:
        return False
    live_definition = _definition_from_config(context.config.ui.input_lists[live_index])
    merged, changed = _merge_polled_values(live_definition, definition)
    if changed:
        context.config.ui.input_lists[live_index] = merged
        _save_polled_config(context)
    return changed


async def poll_input_list_row_by_key(context: Any, key: str, row_index: int) -> bool:
    index = _find_input_list_index(context.config, key)
    if index < 0:
        return False
    definition = _definition_from_config(context.config.ui.input_lists[index])
    if row_index < 0 or row_index >= len(definition.rows):
        return False
    if not await poll_input_list_definition(
        context,
        definition,
        {row_index},
        progress_callback=lambda updated, progress_row, _column: _persist_poll_progress(
            context, key, updated, progress_row
        ),
        row_active_callback=lambda progress_row: _input_list_row_is_enabled(context, key, progress_row),
    ):
        return False
    live_index = _find_input_list_index(context.config, key)
    if live_index < 0:
        return False
    live_definition = _definition_from_config(context.config.ui.input_lists[live_index])
    merged, changed = _merge_polled_values(live_definition, definition, {row_index})
    if changed:
        context.config.ui.input_lists[live_index] = merged
        _save_polled_config(context)
    return changed


async def poll_due_input_lists(
    context: Any,
    next_due: dict[str, float],
    running_tasks: dict[str, asyncio.Task[None]] | None = None,
) -> None:
    now = time.monotonic()
    if running_tasks is not None:
        for key, task in list(running_tasks.items()):
            if task.done():
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    context.logger.warning(
                        "input_list_poll_task_failed",
                        "Input list background task failed",
                        list=key,
                        error=str(exc),
                    )
                running_tasks.pop(key, None)

    async def poll_one(key: str, name: str) -> None:
        try:
            await poll_input_list_by_key(context, key)
        except Exception as exc:
            context.logger.warning("input_list_poll_failed", "Input list polling failed", list=name, error=str(exc))

    for item in list(context.config.ui.input_lists):
        definition = _definition_from_config(item)
        if definition.polling_rate_seconds <= 0:
            continue
        if not any(
            row_def.enabled and any(cell.mode == "polled" for cell in row_def.cells.values())
            for row_def in definition.rows
        ):
            next_due.pop(definition.key, None)
            continue
        due = next_due.get(definition.key, 0)
        if now < due:
            continue
        if running_tasks is not None and definition.key in running_tasks:
            continue
        next_due[definition.key] = now + definition.polling_rate_seconds
        if running_tasks is None:
            await poll_one(definition.key, definition.name)
        else:
            running_tasks[definition.key] = asyncio.create_task(
                poll_one(definition.key, definition.name),
                name=f"input-list:{definition.key}",
            )
