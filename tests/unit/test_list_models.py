"""Row-level list model contract (perf track 6.4).

``DictListModel`` replaces whole-list ``QVariantList`` properties for long
lists: a sync emits ``dataChanged`` for the rows (and roles) that changed and
row inserts/removes for structural changes, so QML views keep their
delegates, scroll position and per-row state across status updates.
"""

import time

import pytest
from PySide6.QtCore import QModelIndex, Qt

from vienetts_app.ui.list_models import DictListModel

ROLES = ("id", "title", "status")


def make(rows=()) -> DictListModel:
    model = DictListModel(ROLES, key="id")
    model.sync(list(rows))
    return model


def rows(*specs: tuple[str, str]) -> list[dict]:
    return [{"id": key, "title": key.upper(), "status": status} for key, status in specs]


class Recorder:
    def __init__(self, model: DictListModel) -> None:
        self.events: list[tuple] = []
        role_ids = {bytes(name).decode(): role for role, name in model.roleNames().items()}
        self.role_names = {role: name for name, role in role_ids.items()}
        model.dataChanged.connect(
            lambda tl, br, roles: self.events.append(
                ("changed", tl.row(), br.row(), sorted(self.role_names[r] for r in roles))
            )
        )
        model.rowsInserted.connect(lambda _p, a, b: self.events.append(("inserted", a, b)))
        model.rowsRemoved.connect(lambda _p, a, b: self.events.append(("removed", a, b)))
        model.modelReset.connect(lambda: self.events.append(("reset",)))
        model.countChanged.connect(lambda: self.events.append(("count",)))


class TestDictListModel:
    def test_roles_include_the_whole_row_as_modelData(self, qcoreapp) -> None:
        model = make(rows(("a", "ready")))
        names = {bytes(n).decode() for n in model.roleNames().values()}
        assert names == {"modelData", "id", "title", "status"}
        role = {bytes(n).decode(): r for r, n in model.roleNames().items()}
        index = model.index(0)
        assert model.data(index, role["title"]) == "A"
        assert model.data(index, role["modelData"]) == {"id": "a", "title": "A", "status": "ready"}
        assert model.rowCount() == 1
        assert model.rowCount(model.index(0)) == 0  # flat list
        assert model.data(model.index(5), role["title"]) is None
        assert model.data(QModelIndex(), Qt.ItemDataRole.DisplayRole) is None

    def test_a_status_update_changes_only_that_row_and_role(self, qcoreapp) -> None:
        model = make(rows(("a", "pending"), ("b", "pending"), ("c", "pending")))
        rec = Recorder(model)
        model.sync(rows(("a", "pending"), ("b", "ready"), ("c", "pending")))
        assert rec.events == [("changed", 1, 1, ["modelData", "status"])]
        assert model.get(1)["status"] == "ready"

    def test_an_identical_sync_emits_nothing(self, qcoreapp) -> None:
        model = make(rows(("a", "pending"), ("b", "ready")))
        rec = Recorder(model)
        model.sync(rows(("a", "pending"), ("b", "ready")))
        assert rec.events == []

    def test_appends_and_removals_are_row_operations_not_resets(self, qcoreapp) -> None:
        model = make(rows(("a", "x"), ("b", "x")))
        rec = Recorder(model)
        model.sync(rows(("a", "x"), ("b", "x"), ("c", "x"), ("d", "x")))
        assert rec.events == [("inserted", 2, 3), ("count",)]
        rec.events.clear()
        model.sync(rows(("a", "x"), ("d", "x")))
        assert rec.events == [("removed", 1, 2), ("count",)]
        assert [model.get(i)["id"] for i in range(model.rowCount())] == ["a", "d"]

    def test_a_middle_insert_keeps_surrounding_rows_and_diffs_them(self, qcoreapp) -> None:
        model = make(rows(("a", "x"), ("c", "x")))
        rec = Recorder(model)
        model.sync(rows(("a", "y"), ("b", "x"), ("c", "x")))
        assert ("inserted", 1, 1) in rec.events
        assert ("changed", 0, 0, ["modelData", "status"]) in rec.events
        assert not any(e[0] in ("reset", "removed") for e in rec.events)
        assert [model.get(i)["id"] for i in range(3)] == ["a", "b", "c"]

    def test_count_get_and_rows_snapshot(self, qcoreapp) -> None:
        model = make(rows(("a", "x")))
        assert model.property("count") == 1
        assert model.get(0) == {"id": "a", "title": "A", "status": "x"}
        assert model.get(3) == {}
        snapshot = model.rows()
        snapshot[0]["status"] = "mutated"
        assert model.get(0)["status"] == "x"  # callers never alias model rows
        model.sync([])
        assert model.property("count") == 0


class TestDictListModelBenchmark:
    @pytest.mark.benchmark
    def test_a_one_row_update_on_a_500_row_list_is_one_signal_and_fast(self, qcoreapp) -> None:
        # A long audiobook / batch queue: a status tick must stay O(rows) in
        # cheap dict compares and touch exactly one row for QML.
        base = rows(*((f"k{i}", "pending") for i in range(500)))
        model = make(base)
        rec = Recorder(model)
        timings = []
        for i in range(20):
            update = [dict(r) for r in base]
            update[250]["status"] = f"tick{i}"
            start = time.perf_counter()
            model.sync(update)
            timings.append(time.perf_counter() - start)
        assert rec.events == [("changed", 250, 250, ["modelData", "status"])] * 20
        assert sorted(timings)[len(timings) // 2] < 0.005
