"""Row-level list models for long QML lists.

A ``QVariantList`` property hands QML a fresh array on every NOTIFY: views
destroy and rebuild every delegate, lose their scroll position and any
per-row state, and every binding that reads the list re-evaluates. For lists
that update row by row (chapter render status, cue edits, batch progress,
Studio clips/ops) a :class:`DictListModel` keeps the rows and tells QML
exactly what changed:

* :meth:`DictListModel.sync` takes the controller's freshly built row dicts
  and diffs them against the current rows by a key field. Rows with the same
  key emit ``dataChanged`` for the changed roles only; added or dropped keys
  become ``rowsInserted``/``rowsRemoved`` around a common prefix and suffix.
  A model is never reset.
* Each dict key named in ``roles`` is a role, and the ``modelData`` role
  returns the whole row. Delegates written against a JS array
  (``required property var modelData``) therefore keep working unchanged.
* ``count`` and ``get(row)`` cover the scalar reads QML used to take from the
  array (``list.length``, ``list[i].title``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
    Signal,
    Slot,
)

_MODEL_DATA_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class DictListModel(QAbstractListModel):
    """Flat list of dict rows, synced in place by a key field."""

    countChanged = Signal()

    def __init__(self, roles: Sequence[str], *, key: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._key = key
        self._rows: list[dict[str, Any]] = []
        self._role_ids = {name: _MODEL_DATA_ROLE + 1 + i for i, name in enumerate(roles)}
        self._role_names = {_MODEL_DATA_ROLE: QByteArray(b"modelData")}
        self._role_names.update(
            {role: QByteArray(name.encode()) for name, role in self._role_ids.items()}
        )
        self._names_by_role = {role: name for name, role in self._role_ids.items()}

    # ── QAbstractListModel ──────────────────────────────────────────────────

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._rows)

    def roleNames(self) -> dict[int, QByteArray]:  # noqa: N802
        return dict(self._role_names)

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = 0) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == _MODEL_DATA_ROLE:
            return row
        name = self._names_by_role.get(role)
        return None if name is None else row.get(name)

    # ── QML scalar reads ────────────────────────────────────────────────────

    def _count(self) -> int:
        return len(self._rows)

    count = Property(int, _count, notify=countChanged)

    @Slot(int, result="QVariantMap")
    def get(self, row: int) -> dict[str, Any]:
        """Copy of row ``row`` (``{}`` when out of range)."""
        if 0 <= row < len(self._rows):
            return dict(self._rows[row])
        return {}

    def rows(self) -> list[dict[str, Any]]:
        """Copies of every row, in order."""
        return [dict(row) for row in self._rows]

    # ── sync ────────────────────────────────────────────────────────────────

    def sync(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Make the model equal to ``rows`` with the smallest signal set."""
        new = [dict(row) for row in rows]
        old_keys = [row.get(self._key) for row in self._rows]
        new_keys = [row.get(self._key) for row in new]
        before = len(self._rows)
        if old_keys != new_keys:
            shortest = min(len(old_keys), len(new_keys))
            prefix = 0
            while prefix < shortest and old_keys[prefix] == new_keys[prefix]:
                prefix += 1
            suffix = 0
            while (
                suffix < shortest - prefix
                and old_keys[len(old_keys) - 1 - suffix] == new_keys[len(new_keys) - 1 - suffix]
            ):
                suffix += 1
            old_end = len(self._rows) - suffix
            new_end = len(new) - suffix
            if old_end > prefix:
                self.beginRemoveRows(QModelIndex(), prefix, old_end - 1)
                del self._rows[prefix:old_end]
                self.endRemoveRows()
            if new_end > prefix:
                self.beginInsertRows(QModelIndex(), prefix, new_end - 1)
                self._rows[prefix:prefix] = new[prefix:new_end]
                self.endInsertRows()
        # Same keys row for row now: per-row, per-role change notification.
        for i, row in enumerate(new):
            current = self._rows[i]
            if current == row:
                continue
            changed = [
                role for name, role in self._role_ids.items() if current.get(name) != row.get(name)
            ]
            self._rows[i] = row
            index = self.index(i)
            self.dataChanged.emit(index, index, [_MODEL_DATA_ROLE, *changed])
        if len(self._rows) != before:
            self.countChanged.emit()
