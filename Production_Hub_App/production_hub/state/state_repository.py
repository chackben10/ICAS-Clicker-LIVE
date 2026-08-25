from __future__ import annotations

import time
from copy import deepcopy
from pathlib import Path
from threading import Lock
from typing import Callable

from production_hub.core.config.repository import AtomicJsonRepository
from production_hub.state.runtime_state import RequestRecord, RuntimeState


class RuntimeStateRepository:
    REQUEST_HISTORY_FLUSH_SECONDS = 2.0

    def __init__(self, state_dir: Path, backup_dir: Path) -> None:
        self._lock = Lock()
        self._repo = AtomicJsonRepository(state_dir / "runtime_state.json", RuntimeState, RuntimeState, backup_dir)
        self._state: RuntimeState | None = None
        self._dirty = False
        self._last_persisted_at = time.monotonic()

    def _load_locked(self) -> RuntimeState:
        if self._state is None:
            self._state = self._repo.load()
        return self._state

    def _persist_locked(self) -> None:
        if self._state is None:
            return
        # Runtime telemetry changes frequently and is not configuration. Keeping
        # versioned backups for every request creates unnecessary disk and CPU load.
        self._repo.save(self._state, create_backup=False)
        self._dirty = False
        self._last_persisted_at = time.monotonic()

    def _save_and_commit_locked(self, state: RuntimeState) -> None:
        """Persist a replacement before exposing it through the in-memory cache."""
        self._repo.save(state, create_backup=False)
        self._state = state
        self._dirty = False
        self._last_persisted_at = time.monotonic()

    def load(self) -> RuntimeState:
        with self._lock:
            return deepcopy(self._load_locked())

    def auto_show_enabled(self) -> bool:
        with self._lock:
            return bool(self._load_locked().auto_show_enabled)

    def clicker_presentation_activation_enabled(self) -> bool:
        with self._lock:
            return bool(self._load_locked().clicker_presentation_activation_enabled)

    def save(self, state: RuntimeState) -> None:
        with self._lock:
            self._save_and_commit_locked(deepcopy(state))

    def update(self, mutator: Callable[[RuntimeState], None]) -> RuntimeState:
        """Atomically load, mutate, and persist runtime state."""
        with self._lock:
            state = deepcopy(self._load_locked())
            mutator(state)
            self._save_and_commit_locked(state)
            return deepcopy(state)

    def record_request(self, record: RequestRecord) -> None:
        """Record request telemetry in memory and coalesce its disk writes."""
        with self._lock:
            self._load_locked().add_request(record)
            self._dirty = True

    def flush_if_due(self) -> None:
        """Persist accumulated telemetry outside the API request path."""
        with self._lock:
            attempted_at = time.monotonic()
            elapsed = attempted_at - self._last_persisted_at
            if self._dirty and elapsed >= self.REQUEST_HISTORY_FLUSH_SECONDS:
                # Throttle retries as well as successful writes if the disk is
                # temporarily unavailable.
                self._last_persisted_at = attempted_at
                self._persist_locked()

    def flush(self) -> None:
        with self._lock:
            if self._dirty:
                self._persist_locked()
