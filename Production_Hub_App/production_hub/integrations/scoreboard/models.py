from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from production_hub.core.config.models import JsonModel, ValidationError


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ScoreRow(JsonModel):
    id: str
    name: str = ""
    score: int = 0

    def __post_init__(self) -> None:
        self.id = str(self.id or "").strip()
        if not self.id:
            raise ValidationError("Score row id is required")
        self.name = str(self.name or "")
        self.score = int(self.score)


@dataclass
class ScoreboardState(JsonModel):
    schema_version: int = 1
    rows: list[ScoreRow] = field(default_factory=list)
    history: list[list[ScoreRow]] = field(default_factory=list)
    modified_at: str = field(default_factory=_now)
    revision: int = 0
    last_writer: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_legacy_payload(cls, payload: dict[str, Any]) -> "ScoreboardState":
        rows = [ScoreRow.from_dict(item) for item in payload.get("rows", []) if isinstance(item, dict)]
        history: list[list[ScoreRow]] = []
        for snapshot in payload.get("history", []):
            if isinstance(snapshot, list):
                history.append([ScoreRow.from_dict(item) for item in snapshot if isinstance(item, dict)])
        revision = int(payload.get("revision", 0) or 0)
        return cls(rows=rows, history=history[-100:], revision=revision)

    def legacy_payload(self) -> dict[str, Any]:
        data = self.to_dict()
        return {
            "rows": data["rows"],
            "history": data["history"],
            "modified_at": self.modified_at,
            "revision": self.revision,
            "last_writer": self.last_writer,
        }

    def validate_unique_rows(self) -> None:
        ids = [row.id for row in self.rows]
        if len(ids) != len(set(ids)):
            raise ValidationError("Scoreboard row ids must be unique")

