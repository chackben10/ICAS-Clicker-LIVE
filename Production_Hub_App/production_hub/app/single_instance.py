from __future__ import annotations

import fcntl
import os
import tempfile
from pathlib import Path
from typing import IO


class SingleInstanceLock:
    """A process-lifetime advisory lock for the desktop application."""

    def __init__(self, path: Path, handle: IO[str]) -> None:
        self.path = path
        self._handle: IO[str] | None = handle

    @classmethod
    def acquire(cls, path: Path | None = None) -> SingleInstanceLock | None:
        selected = path or default_lock_path()
        selected.parent.mkdir(parents=True, exist_ok=True)
        handle = selected.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        return cls(selected, handle)

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> SingleInstanceLock:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


def default_lock_path() -> Path:
    user_id = os.getuid() if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / f"org.icas.productionhub.{user_id}.lock"
