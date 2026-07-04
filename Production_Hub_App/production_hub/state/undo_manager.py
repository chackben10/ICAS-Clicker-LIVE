from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class ChangeRecord:
    label: str
    undo: Callable[[], None]
    redo: Callable[[], None]


class UndoManager:
    def __init__(self, max_items: int = 100) -> None:
        self.max_items = max(100, int(max_items))
        self._undo_stack: list[ChangeRecord] = []
        self._redo_stack: list[ChangeRecord] = []

    def record(self, label: str, undo: Callable[[], None], redo: Callable[[], None]) -> None:
        self._undo_stack.append(ChangeRecord(label, undo, redo))
        if len(self._undo_stack) > self.max_items:
            self._undo_stack = self._undo_stack[-self.max_items :]
        self._redo_stack.clear()

    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    def undo(self) -> str:
        if not self._undo_stack:
            return "Nothing to undo."
        record = self._undo_stack.pop()
        record.undo()
        self._redo_stack.append(record)
        return f"Undid: {record.label}"

    def redo(self) -> str:
        if not self._redo_stack:
            return "Nothing to redo."
        record = self._redo_stack.pop()
        record.redo()
        self._undo_stack.append(record)
        return f"Redid: {record.label}"

