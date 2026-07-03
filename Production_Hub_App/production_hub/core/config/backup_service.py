from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path


class BackupService:
    def __init__(self, backup_dir: Path, keep: int = 25) -> None:
        self.backup_dir = backup_dir
        self.keep = keep
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def snapshot(self, source: Path, label: str | None = None) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        suffix = f"-{label}" if label else ""
        destination = self.backup_dir / f"{source.stem}-{stamp}{suffix}{source.suffix}"
        shutil.copy2(source, destination)
        self.prune(source.stem)
        return destination

    def prune(self, prefix: str) -> None:
        backups = sorted(self.backup_dir.glob(f"{prefix}-*"), key=lambda path: path.stat().st_mtime, reverse=True)
        for old in backups[self.keep :]:
            old.unlink(missing_ok=True)

