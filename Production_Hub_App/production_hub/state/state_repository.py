from __future__ import annotations

from pathlib import Path
from threading import Lock

from production_hub.core.config.repository import AtomicJsonRepository
from production_hub.state.runtime_state import RuntimeState


class RuntimeStateRepository:
    def __init__(self, state_dir: Path, backup_dir: Path) -> None:
        self._lock = Lock()
        self._repo = AtomicJsonRepository(state_dir / "runtime_state.json", RuntimeState, RuntimeState, backup_dir)

    def load(self) -> RuntimeState:
        with self._lock:
            return self._repo.load()

    def save(self, state: RuntimeState) -> None:
        with self._lock:
            self._repo.save(state)

