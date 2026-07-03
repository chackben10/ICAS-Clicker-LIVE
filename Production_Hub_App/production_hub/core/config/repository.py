from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Generic, TypeVar

from production_hub.core.automation.models import AutomationDefinition
from production_hub.core.config.defaults import (
    build_default_automations,
    build_default_config,
    build_default_endpoints,
)
from production_hub.core.config.models import AppConfig, AppPaths, JsonModel, ValidationError
from production_hub.core.endpoints.models import EndpointDefinition

T = TypeVar("T", bound=JsonModel)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def default_app_root() -> Path:
    override = os.environ.get("PRODUCTION_HUB_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "Production Hub"


class AtomicJsonRepository(Generic[T]):
    def __init__(self, path: Path, factory: Callable[[], T], model_type: type[T], backup_dir: Path) -> None:
        self.path = path
        self.factory = factory
        self.model_type = model_type
        self.backup_dir = backup_dir

    def load(self) -> T:
        if not self.path.exists():
            model = self.factory()
            self.save(model, create_backup=False)
            return model
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return self.model_type.from_dict(data)
        except Exception as exc:
            raise ValidationError(f"Invalid configuration file {self.path}: {exc}") from exc

    def save(self, model: T, create_backup: bool = True) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        if create_backup and self.path.exists():
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            backup = self.backup_dir / f"{self.path.stem}-{stamp}{self.path.suffix}"
            shutil.copy2(self.path, backup)

        data = model.to_dict()
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


class ListJsonRepository(Generic[T]):
    def __init__(self, path: Path, factory: Callable[[], list[T]], model_type: type[T], backup_dir: Path) -> None:
        self.path = path
        self.factory = factory
        self.model_type = model_type
        self.backup_dir = backup_dir

    def load(self) -> list[T]:
        if not self.path.exists():
            items = self.factory()
            self.save(items, create_backup=False)
            return items
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, list):
            raise ValidationError(f"{self.path} must contain a JSON list")
        return [self.model_type.from_dict(item) for item in data]

    def save(self, items: list[T], create_backup: bool = True) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        if create_backup and self.path.exists():
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            shutil.copy2(self.path, self.backup_dir / f"{self.path.stem}-{stamp}{self.path.suffix}")

        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump([item.to_dict() for item in items], handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


class ConfigRepository:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.paths.ensure()
        self.app_repo = AtomicJsonRepository(
            paths.config_dir / "default_profile.json",
            build_default_config,
            AppConfig,
            paths.automatic_backups_dir,
        )
        self.endpoint_repo = ListJsonRepository(
            paths.config_dir / "endpoints.json",
            build_default_endpoints,
            EndpointDefinition,
            paths.automatic_backups_dir,
        )
        self.automation_repo = ListJsonRepository(
            paths.config_dir / "automations.json",
            build_default_automations,
            AutomationDefinition,
            paths.automatic_backups_dir,
        )

    def load_app_config(self) -> AppConfig:
        return self.app_repo.load()

    def save_app_config(self, config: AppConfig) -> None:
        config.last_saved_at = now_iso()
        self.app_repo.save(config)

    def load_endpoints(self) -> list[EndpointDefinition]:
        return self.endpoint_repo.load()

    def save_endpoints(self, endpoints: list[EndpointDefinition]) -> None:
        self.endpoint_repo.save(endpoints)

    def load_automations(self) -> list[AutomationDefinition]:
        return self.automation_repo.load()

    def save_automations(self, automations: list[AutomationDefinition]) -> None:
        self.automation_repo.save(automations)

