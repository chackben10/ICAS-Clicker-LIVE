from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = {"password", "token", "authorization", "access_token"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if key.lower() in SENSITIVE_KEYS else _redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "severity": record.levelname,
            "component": getattr(record, "component", record.name),
            "event": getattr(record, "event", record.getMessage()),
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", ""),
            "metadata": _redact(getattr(record, "metadata", {})),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True)


class StructuredLogger:
    def __init__(self, logger: logging.Logger, component: str) -> None:
        self.logger = logger
        self.component = component

    def info(self, event: str, message: str, **metadata: Any) -> None:
        self.logger.info(message, extra={"component": self.component, "event": event, "metadata": metadata})

    def warning(self, event: str, message: str, **metadata: Any) -> None:
        self.logger.warning(message, extra={"component": self.component, "event": event, "metadata": metadata})

    def error(self, event: str, message: str, **metadata: Any) -> None:
        self.logger.error(message, extra={"component": self.component, "event": event, "metadata": metadata})

    def exception(self, event: str, message: str, **metadata: Any) -> None:
        self.logger.exception(message, extra={"component": self.component, "event": event, "metadata": metadata})


def configure_logging(log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("production_hub")
    logger.setLevel(level)
    logger.handlers.clear()

    log_path = log_dir / f"production-hub-{datetime.now(UTC).strftime('%Y-%m-%d')}.log"
    file_handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=14, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(stream_handler)
    return logger

