from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class LogRepository:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir

    def recent(self, limit: int = 200, component: str | None = None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.log_dir.glob("production-hub-*.log"), reverse=True)[:3]:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if component and record.get("component") != component:
                    continue
                records.append(record)
        return records[-limit:]

