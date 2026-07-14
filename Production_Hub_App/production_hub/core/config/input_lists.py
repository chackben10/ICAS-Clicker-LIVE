from __future__ import annotations

import json
import urllib.request
from typing import Any

from production_hub.core.config.models import InputListCell, InputListColumn, InputListDefinition, InputListItem, InputListRow


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


def row(enabled: bool, **cells: InputListCell) -> InputListRow:
    return InputListRow(enabled, cells)


def preview_text(value: object, limit: int = 80) -> str:
    if isinstance(value, dict):
        text = ", ".join(f"{key}: {item}" for key, item in value.items())
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
        description="Songs selectable by the clicker app.",
        polling_rate_seconds=3600,
        columns=[
            column("library_name", "Library Name", "string", "label"),
            column("uuid", "UUID", "string", "value"),
            column("songs", "Songs", "dictionary"),
        ],
        rows=[
            row(
                True,
                library_name=static_cell("Malayalam Songs"),
                uuid=static_cell(""),
                songs=polled_dictionary_cell(
                    "v1/library/Malayalam%20Songs",
                    "items[].name",
                    "items[].uuid",
                ),
            ),
            row(
                False,
                library_name=static_cell("English Songs"),
                uuid=static_cell(""),
                songs=polled_dictionary_cell(
                    "v1/library/English%20Songs",
                    "items[].name",
                    "items[].uuid",
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
    return ensure_song_library_dictionary(config) or changed


def ensure_song_library_dictionary(config: Any) -> bool:
    """Migrate the built-in Songs cell from a title array to title -> UUID data."""
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
        if songs_column.data_type != "dictionary":
            songs_column.data_type = "dictionary"
            changed = True
        for row_def in definition.rows:
            cell = row_def.cells.get("songs")
            if cell is None:
                continue
            library_name = str(row_def.cells.get("library_name", InputListCell()).value or "").strip()
            if library_name.casefold() == "english songs" and cell.url == "v1/library/English%Songs":
                cell.url = "v1/library/English%20Songs"
                changed = True
            if isinstance(cell.value, list):
                cell.value = {str(name): "" for name in cell.value if str(name).strip()}
                cell.preview = preview_text(cell.value)
                changed = True
            if cell.mode != "polled":
                continue
            key_path = cell.json_key_path or cell.json_path or "items[].name"
            value_path = cell.json_value_path or "items[].uuid"
            if cell.json_key_path != key_path or cell.json_value_path != value_path or cell.json_path:
                cell.json_key_path = key_path
                cell.json_value_path = value_path
                cell.json_path = ""
                changed = True
        if changed:
            definition.builtin = False
            config.ui.input_lists[index] = definition
        break
    return changed


def all_input_lists(config: Any) -> list[InputListDefinition]:
    return custom_input_lists(config)


def input_list_by_key(config: Any, key: str) -> InputListDefinition | None:
    for item in all_input_lists(config):
        if item.key == key:
            return item
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
    current = data
    for part in [item for item in path.split(".") if item]:
        if part.endswith("[]"):
            key = part[:-2]
            current = current.get(key, []) if isinstance(current, dict) else []
            continue
        if isinstance(current, list):
            current = [item.get(part) for item in current if isinstance(item, dict) and part in item]
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


async def _fetch_json(context: Any, url: str) -> dict[str, Any]:
    target = str(url or "").strip()
    if not target:
        return {}
    if target.startswith("http://") or target.startswith("https://"):
        def request() -> dict[str, Any]:
            with urllib.request.urlopen(target, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))

        import asyncio

        return await asyncio.to_thread(request)
    path = target.lstrip("/")
    if path.startswith("v1/"):
        path = path[3:]
    return await context.propresenter.client.get_json(path)


async def poll_input_list_definition(
    context: Any,
    definition: InputListDefinition,
    row_indices: set[int] | None = None,
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
            data = await _fetch_json(context, cell.url)
            if column_types.get(column_key) == "dictionary":
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


async def poll_input_list_by_key(context: Any, key: str) -> bool:
    for index, item in enumerate(context.config.ui.input_lists):
        definition = InputListDefinition.from_dict(item.to_dict() if hasattr(item, "to_dict") else item)
        if definition.key != key:
            continue
        changed = await poll_input_list_definition(context, definition)
        if changed:
            context.config.ui.input_lists[index] = definition
            context.config_repository.save_app_config(context.config)
        return changed
    return False


async def poll_input_list_row_by_key(context: Any, key: str, row_index: int) -> bool:
    for index, item in enumerate(context.config.ui.input_lists):
        definition = InputListDefinition.from_dict(item.to_dict() if hasattr(item, "to_dict") else item)
        if definition.key != key:
            continue
        if row_index < 0 or row_index >= len(definition.rows):
            return False
        changed = await poll_input_list_definition(context, definition, {row_index})
        if changed:
            context.config.ui.input_lists[index] = definition
            context.config_repository.save_app_config(context.config)
        return changed
    return False


async def poll_due_input_lists(context: Any, next_due: dict[str, float]) -> None:
    import time

    now = time.monotonic()
    for index, item in enumerate(list(context.config.ui.input_lists)):
        definition = InputListDefinition.from_dict(item.to_dict() if hasattr(item, "to_dict") else item)
        if definition.polling_rate_seconds <= 0:
            continue
        if not any(cell.mode == "polled" for row_def in definition.rows for cell in row_def.cells.values()):
            continue
        due = next_due.get(definition.key, 0)
        if now < due:
            continue
        next_due[definition.key] = now + definition.polling_rate_seconds
        try:
            changed = await poll_input_list_definition(context, definition)
            if changed:
                context.config.ui.input_lists[index] = definition
                context.config_repository.save_app_config(context.config)
        except Exception as exc:
            context.logger.warning("input_list_poll_failed", "Input list polling failed", list=definition.name, error=str(exc))
