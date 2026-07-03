from __future__ import annotations

from pathlib import Path
from threading import RLock

from production_hub.core.config.repository import AtomicJsonRepository
from production_hub.integrations.scoreboard.models import ScoreboardState


class ScoreboardRepository:
    def __init__(self, state_dir: Path, backup_dir: Path) -> None:
        self._lock = RLock()
        self._repo = AtomicJsonRepository(state_dir / "scoreboard.json", ScoreboardState, ScoreboardState, backup_dir)

    def load(self) -> ScoreboardState:
        with self._lock:
            return self._repo.load()

    def save(self, state: ScoreboardState) -> None:
        with self._lock:
            self._repo.save(state)

    def locked(self) -> RLock:
        return self._lock

