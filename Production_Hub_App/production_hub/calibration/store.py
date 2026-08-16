from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CalibrationCuration:
    approval_status: str = "legacy_approved"
    excluded_marker_ids: tuple[int, ...] = ()
    updated_at: str = ""
    approved_at: str = ""

    @property
    def approved(self) -> bool:
        return self.approval_status in {"approved", "legacy_approved"}


class CalibrationRegistry:
    """Persist review decisions separately from immutable calibration output."""

    MINIMUM_RETAINED_MARKERS = 24
    MAXIMUM_HISTORY = 20
    AUTOMATIC_MAP_PURPOSE = "full multi-pose Audience-to-PTZ synchronization map"

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.expanduser().resolve()
        self.registry_directory = self.data_root / "calibration"
        self.active_manifest_path = self.registry_directory / "active-map.json"

    @staticmethod
    def curation_path(map_path: Path) -> Path:
        return map_path.with_name(f"{map_path.stem}.review.json")

    def curation(self, map_path: Path, payload: dict[str, Any] | None = None) -> CalibrationCuration:
        path = self._validated_map_path(map_path)
        sidecar = self.curation_path(path)
        if sidecar.is_file():
            raw = _load_json(sidecar)
            status = str(raw.get("approval_status", "pending_review"))
            excluded = tuple(
                sorted(
                    {
                        int(value)
                        for value in raw.get("excluded_marker_ids", ())
                        if int(value) > 0
                    }
                )
            )
            return CalibrationCuration(
                approval_status=status,
                excluded_marker_ids=excluded,
                updated_at=str(raw.get("updated_at", "")),
                approved_at=str(raw.get("approved_at", "")),
            )
        selected = payload if payload is not None else _load_json(path)
        status = str(selected.get("approval_status", "legacy_approved"))
        return CalibrationCuration(approval_status=status)

    def exclude_marker(self, map_path: Path, marker_id: int) -> CalibrationCuration:
        path, payload, curation = self._inputs(map_path)
        selected_id = int(marker_id)
        available_ids = self._marker_ids(payload)
        if selected_id not in available_ids:
            raise ValueError(f"Marker M{selected_id:03d} is not part of this calibration.")
        excluded = set(curation.excluded_marker_ids)
        excluded.add(selected_id)
        if len(available_ids - excluded) < self.MINIMUM_RETAINED_MARKERS:
            raise ValueError(
                f"At least {self.MINIMUM_RETAINED_MARKERS} calibration markers must remain enabled."
            )
        return self._write_curation(
            path,
            curation,
            excluded_marker_ids=excluded,
            approval_status="pending_review",
            approved_at="",
        )

    def restore_marker(self, map_path: Path, marker_id: int) -> CalibrationCuration:
        path, _payload, curation = self._inputs(map_path)
        excluded = set(curation.excluded_marker_ids)
        excluded.discard(int(marker_id))
        return self._write_curation(
            path,
            curation,
            excluded_marker_ids=excluded,
            approval_status="pending_review",
            approved_at="",
        )

    def approve_and_activate(self, map_path: Path) -> CalibrationCuration:
        path, payload, curation = self._inputs(map_path)
        if payload.get("status") != "accepted":
            raise ValueError("Only an accepted calibration map can be approved.")
        retained = self._marker_ids(payload) - set(curation.excluded_marker_ids)
        if len(retained) < self.MINIMUM_RETAINED_MARKERS:
            raise ValueError(
                f"At least {self.MINIMUM_RETAINED_MARKERS} enabled markers are required."
            )
        manifest = self._active_manifest()
        current = str(manifest.get("active_map", ""))
        if not current:
            fallback = self._latest_approved_map(excluding=path)
            current = str(fallback) if fallback is not None else ""
        approved_at = _now()
        result = self._write_curation(
            path,
            curation,
            approval_status="approved",
            approved_at=approved_at,
        )
        history = [str(item) for item in manifest.get("history", ()) if str(item)]
        selected = str(path)
        if current and current != selected:
            history.insert(0, current)
        history = [item for index, item in enumerate(history) if item != selected and item not in history[:index]]
        _atomic_json(
            self.active_manifest_path,
            {
                "schema_version": 1,
                "active_map": selected,
                "activated_at": approved_at,
                "history": history[: self.MAXIMUM_HISTORY],
            },
        )
        return result

    def activate_latest_automatic_map(self) -> Path | None:
        """Make the newest successful one-button calibration the runtime map.

        Schema-v2 maps are produced by the guarded automated workflow. They no
        longer require a separate marker-review action in the simplified UI.
        Older/manual review fixtures retain their explicit approval semantics.
        """

        candidates = sorted(
            self.data_root.glob("calibration-sweeps/*/full_sync.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            try:
                path = self._validated_map_path(candidate)
                payload = _load_json(path)
                if not self._is_automatic_runtime_candidate(payload):
                    continue
                current = self.active_map_path()
                if current == path and self.curation(path, payload).approved:
                    return path
                self.approve_and_activate(path)
                return path
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        active = self.active_map_path()
        if active is not None and self.curation(active).approved:
            return active
        return None

    def _is_automatic_runtime_candidate(self, payload: dict[str, Any]) -> bool:
        if int(payload.get("schema_version", 0)) < 2:
            return False
        if payload.get("purpose") != self.AUTOMATIC_MAP_PURPOSE:
            return False
        if payload.get("status") != "accepted":
            return False
        if len(self._marker_ids(payload)) < self.MINIMUM_RETAINED_MARKERS:
            return False
        return sum(
            1
            for pose in payload.get("poses", ())
            if isinstance(pose, dict) and pose.get("status") == "accepted"
        ) >= 4

    def _latest_approved_map(self, *, excluding: Path | None = None) -> Path | None:
        candidates = sorted(
            self.data_root.glob("calibration-sweeps/*/full_sync.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            path = candidate.resolve()
            if excluding is not None and path == excluding:
                continue
            try:
                payload = _load_json(path)
                if payload.get("status") == "accepted" and self.curation(path, payload).approved:
                    return path
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return None

    def active_map_path(self) -> Path | None:
        manifest = self._active_manifest()
        value = str(manifest.get("active_map", "")).strip()
        if not value:
            return None
        try:
            path = self._validated_map_path(Path(value))
        except ValueError:
            return None
        return path if path.is_file() else None

    def rollback(self) -> Path:
        manifest = self._active_manifest()
        current = str(manifest.get("active_map", "")).strip()
        history = [str(item) for item in manifest.get("history", ()) if str(item)]
        selected: Path | None = None
        remaining: list[str] = []
        for value in history:
            try:
                candidate = self._validated_map_path(Path(value))
            except ValueError:
                continue
            if selected is None and candidate.is_file() and self.curation(candidate).approved:
                selected = candidate
            else:
                remaining.append(str(candidate))
        if selected is None:
            raise ValueError("No earlier approved calibration is available to restore.")
        if current:
            remaining.insert(0, current)
        _atomic_json(
            self.active_manifest_path,
            {
                "schema_version": 1,
                "active_map": str(selected),
                "activated_at": _now(),
                "history": remaining[: self.MAXIMUM_HISTORY],
            },
        )
        return selected

    def _inputs(
        self,
        map_path: Path,
    ) -> tuple[Path, dict[str, Any], CalibrationCuration]:
        path = self._validated_map_path(map_path)
        payload = _load_json(path)
        return path, payload, self.curation(path, payload)

    def _write_curation(
        self,
        path: Path,
        existing: CalibrationCuration,
        *,
        excluded_marker_ids: set[int] | None = None,
        approval_status: str | None = None,
        approved_at: str | None = None,
    ) -> CalibrationCuration:
        now = _now()
        result = CalibrationCuration(
            approval_status=approval_status or existing.approval_status,
            excluded_marker_ids=tuple(
                sorted(
                    existing.excluded_marker_ids
                    if excluded_marker_ids is None
                    else excluded_marker_ids
                )
            ),
            updated_at=now,
            approved_at=approved_at if approved_at is not None else existing.approved_at,
        )
        _atomic_json(
            self.curation_path(path),
            {
                "schema_version": 1,
                "map_path": str(path),
                "approval_status": result.approval_status,
                "excluded_marker_ids": list(result.excluded_marker_ids),
                "updated_at": result.updated_at,
                "approved_at": result.approved_at,
            },
        )
        return result

    def _active_manifest(self) -> dict[str, Any]:
        if not self.active_manifest_path.is_file():
            return {}
        try:
            return _load_json(self.active_manifest_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _validated_map_path(self, map_path: Path) -> Path:
        path = map_path.expanduser().resolve()
        try:
            path.relative_to(self.data_root)
        except ValueError as exc:
            raise ValueError("Calibration map must be inside Production Hub data storage.") from exc
        return path

    @staticmethod
    def _marker_ids(payload: dict[str, Any]) -> set[int]:
        return {
            int(item["marker_id"])
            for item in payload.get("structural_markers", ())
            if isinstance(item, dict) and int(item.get("marker_id", 0)) > 0
        }


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _now() -> str:
    return datetime.now(UTC).isoformat()
