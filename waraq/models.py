"""List models for QML that update in place instead of resetting.

A QVariantList property republished from Python looks like a brand-new model
to every QML ListView: all delegates are destroyed and the view snaps back to
the top. DictListModel instead exposes a list of dicts as roles and syncs the
existing rows on every update, so scroll position, hover state and flick
velocity all survive a refresh.
"""
from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt


class DictListModel(QAbstractListModel):
    """A QAbstractListModel over a plain list of dicts.

    Every dict key becomes a QML role (auto-discovered on the first update;
    keys that only appear on some rows read as null elsewhere), and a reserved
    role named `row` returns the whole row dict. Delegates should read
    `row.<key>` — a bare role name can be shadowed by unrelated keys (a worker
    row's own `model` key once replaced the delegate's `model` wrapper) and a
    role named like an existing Item property (`enabled`) would be shadowed by
    the Item itself, while `row` is always the one true row object.

    update_items() is the only mutation entry point: it adjusts the row count
    with proper insert/remove notifications and emits a single dataChanged
    range covering the rows whose content actually changed — never a model
    reset — while the data is unchanged it emits nothing at all.

    Call it on the GUI thread only (begin/end* notifications are not
    thread-safe); background results must be marshalled first, e.g. via a
    queued signal.
    """

    _ROW_ROLE = int(Qt.ItemDataRole.UserRole) + 1001

    def __init__(self, roles=None, parent=None):
        super().__init__(parent)
        self._roles = list(roles) if roles else []
        self._items: list[dict] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def roleNames(self):
        base = int(Qt.ItemDataRole.UserRole) + 1
        names = {base + i: role.encode("utf-8") for i, role in enumerate(self._roles)}
        names[self._ROW_ROLE] = b"row"
        return names

    def data(self, index, role=int(Qt.ItemDataRole.DisplayRole)):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role == self._ROW_ROLE:
            return item
        offset = role - int(Qt.ItemDataRole.UserRole) - 1
        if 0 <= offset < len(self._roles):
            return item.get(self._roles[offset])
        return None

    def update_items(self, items):
        """Sync the model to `items` with the smallest possible notifications."""
        items = list(items)
        known = set(self._roles)
        unknown = {key for item in items for key in item if key not in known}
        if unknown:
            # New keys change roleNames(); QML only re-reads that on a reset.
            # Roles stabilise after the first population, so this fires once.
            self.beginResetModel()
            self._roles.extend(sorted(unknown))
            self._items = items
            self.endResetModel()
            return
        if len(items) > len(self._items):
            self.beginInsertRows(QModelIndex(), len(self._items), len(items) - 1)
            self._items.extend([{}] * (len(items) - len(self._items)))
            self.endInsertRows()
        elif len(items) < len(self._items):
            self.beginRemoveRows(QModelIndex(), len(items), len(self._items) - 1)
            del self._items[len(items):]
            self.endRemoveRows()
        first = last = -1
        for row, item in enumerate(items):
            if self._items[row] != item:
                self._items[row] = item
                if first < 0:
                    first = row
                last = row
        if first >= 0:
            self.dataChanged.emit(self.index(first), self.index(last))
