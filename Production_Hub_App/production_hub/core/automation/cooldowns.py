from __future__ import annotations

import time


class CooldownGate:
    def __init__(self, seconds: float) -> None:
        self.seconds = max(0, float(seconds))
        self._last_at = 0.0

    def ready(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return (now - self._last_at) >= self.seconds

    def mark(self, now: float | None = None) -> None:
        self._last_at = time.monotonic() if now is None else now


class DebounceGate:
    def __init__(self, seconds: float) -> None:
        self.seconds = max(0, float(seconds))
        self._pending_signature = ""
        self._pending_at = 0.0
        self._last_applied_signature = ""

    def offer(self, signature: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if signature == self._last_applied_signature:
            return False
        if signature != self._pending_signature:
            self._pending_signature = signature
            self._pending_at = now
            return self.seconds == 0
        return (now - self._pending_at) >= self.seconds

    def mark_applied(self) -> None:
        self._last_applied_signature = self._pending_signature

