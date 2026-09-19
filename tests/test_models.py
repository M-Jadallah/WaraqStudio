"""DictListModel must sync rows in place: dataChanged for changed content,
insert/remove only for count changes, and nothing at all when nothing changed.
A QVariantList republish resets the whole QML view; this model must never do
that once its roles are discovered."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from waraq.models import DictListModel


class Recorder:
    def __init__(self, model):
        self.log = []
        model.modelReset.connect(lambda: self.log.append("reset"))
        model.rowsAboutToBeInserted.connect(lambda p, f, l: self.log.append(f"insert {f}-{l}"))
        model.rowsAboutToBeRemoved.connect(lambda p, f, l: self.log.append(f"remove {f}-{l}"))
        model.dataChanged.connect(lambda a, b, *args: self.log.append(f"changed {a.row()}-{b.row()}"))


def role_of(model, name):
    for value, encoded in model.roleNames().items():
        if encoded.decode() == name:
            return int(value)
    raise KeyError(name)


def test_first_population_discovers_roles_and_resets_once():
    model = DictListModel()
    recorder = Recorder(model)
    model.update_items([{"id": "a", "title": "درس"}, {"id": "b", "title": "خطب"}])
    assert model.rowCount() == 2
    assert recorder.log == ["reset"]
    roles = {v.decode() for v in model.roleNames().values()}
    assert {"id", "title"} <= roles
    assert model.index(1).data(role_of(model, "title")) == "خطب"


def test_identical_update_emits_nothing():
    model = DictListModel()
    model.update_items([{"id": 1, "progress": 10, "steps": [{"name": "download"}]}])
    recorder = Recorder(model)
    model.update_items([{"id": 1, "progress": 10, "steps": [{"name": "download"}]}])
    assert recorder.log == []


def test_changed_content_is_a_single_datachanged_range():
    model = DictListModel()
    model.update_items([{"id": 1, "progress": 10}, {"id": 2, "progress": 20}, {"id": 3, "progress": 30}])
    recorder = Recorder(model)
    model.update_items([{"id": 1, "progress": 10}, {"id": 2, "progress": 95}, {"id": 3, "progress": 30}])
    assert recorder.log == ["changed 1-1"]
    assert model.index(1).data(role_of(model, "progress")) == 95
    assert model.index(0).data(role_of(model, "progress")) == 10


def test_append_inserts_rows_without_reset():
    model = DictListModel()
    model.update_items([{"id": 1}])
    recorder = Recorder(model)
    model.update_items([{"id": 1}, {"id": 2}, {"id": 3}])
    # The new rows are inserted, then filled in — both notifications are needed
    # by the view, but the surviving row is never touched and no reset happens.
    assert recorder.log == ["insert 1-2", "changed 1-2"]
    assert model.rowCount() == 3
    assert model.index(2).data(role_of(model, "id")) == 3


def test_shrink_removes_rows_without_reset():
    model = DictListModel()
    model.update_items([{"id": 1}, {"id": 2}, {"id": 3}])
    recorder = Recorder(model)
    model.update_items([{"id": 1}])
    assert recorder.log == ["remove 1-2"]
    assert model.rowCount() == 1


def test_reorder_is_content_change_not_reset():
    model = DictListModel()
    model.update_items([{"id": "a", "title": "أول"}, {"id": "b", "title": "ثانٍ"}])
    recorder = Recorder(model)
    model.update_items([{"id": "b", "title": "ثانٍ"}, {"id": "a", "title": "أول"}])
    assert recorder.log == ["changed 0-1"]
    assert model.index(0).data(role_of(model, "id")) == "b"
    assert model.index(1).data(role_of(model, "id")) == "a"


def test_missing_role_reads_as_null():
    model = DictListModel()
    model.update_items([{"id": "a", "error": ""}, {"id": "b"}])
    assert model.index(0).data(role_of(model, "error")) == ""
    assert model.index(1).data(role_of(model, "error")) is None


def test_growth_with_new_keys_resets_once_to_publish_roles():
    model = DictListModel()
    model.update_items([{"id": 1}])
    recorder = Recorder(model)
    model.update_items([{"id": 1, "cooldownSeconds": 30}])
    assert recorder.log == ["reset"]
    assert "cooldownSeconds" in {v.decode() for v in model.roleNames().values()}
