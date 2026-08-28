from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from testing.harness.paths import OUTPUT_ROOT, REPOSITORY_ROOT


SECRET_KEY = re.compile(r"password|passwd|secret|token|authorization|credential|api[_-]?key", re.I)


def redact(value: Any, key: str = "") -> Any:
    if key and SECRET_KEY.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(item_key): redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(authorization:\s*)([^\s]+)", r"\1<redacted>", value)
        value = re.sub(r"(?i)(password|token|secret)=([^&\s]+)", r"\1=<redacted>", value)
    return value


def git_revision() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


@dataclass
class PhaseResult:
    name: str
    ok: bool
    returncode: int = 0
    duration_seconds: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunReport:
    mode: str
    output_dir: Path
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    revision: str = field(default_factory=git_revision)
    phases: list[PhaseResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recovery: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, mode: str) -> "RunReport":
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        output_dir = OUTPUT_ROOT / f"{timestamp}-{git_revision()}-{mode}"
        output_dir.mkdir(parents=True, exist_ok=False)
        return cls(mode=mode, output_dir=output_dir)

    @property
    def ok(self) -> bool:
        return bool(self.phases) and all(phase.ok for phase in self.phases)

    def add(self, phase: PhaseResult) -> None:
        self.phases.append(phase)
        self.write()

    def write(self) -> None:
        payload = {
            "mode": self.mode,
            "started_at": self.started_at,
            "revision": self.revision,
            "ok": self.ok,
            "system": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": sys.version.split()[0],
            },
            "phases": [asdict(phase) for phase in self.phases],
            "warnings": self.warnings,
            "recovery": self.recovery,
        }
        target = self.output_dir / "summary.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(redact(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, target)

