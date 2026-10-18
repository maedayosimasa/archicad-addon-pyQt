"""undo_commands.EditValueCommand のテスト（実 QUndoStack + 一時 DB）。"""
from PyQt6.QtGui import QUndoStack

import server
from undo_commands import EditValueCommand


def _model():
    m = server.PropertyTableModel()
    m.set_data([{"guid": "E1"}], [{"guid": "P1", "name": "幅"}], {("E1", "P1"): "100"})
    return m


def test_text_uses_property_name(qapp, db):
    cmd = EditValueCommand(_model(), db, "S", "E1", "P1", "100", "200")
    assert cmd.text() == "編集: 幅 → 200"


def test_text_falls_back_to_guid_prefix(qapp, db):
    cmd = EditValueCommand(_model(), db, "S", "E1", "UNKNOWN-PROP-GUID", "a", "b")
    assert cmd.text() == "編集: UNKNOWN- → b"


def test_push_undo_redo_updates_model_db_and_history(qapp, db):
    cid = db.save_condition([], {})
    db.upsert_elements([{"guid": "E1"}], cid)
    db.upsert_properties([{"guid": "P1", "name": "幅"}])
    model, stack = _model(), QUndoStack()
    stack.push(EditValueCommand(model, db, "S", "E1", "P1", "100", "200", condition_id=cid))
    assert model._values[("E1", "P1")] == "200"
    assert db.load_values() == {("E1", "P1"): "200"}

    stack.undo()
    assert model._values[("E1", "P1")] == "100"
    assert db.load_values() == {("E1", "P1"): "100"}

    stack.redo()
    assert model._values[("E1", "P1")] == "200"

    hist = db.get_history(ascending=True)
    assert [h["source"] for h in hist] == ["user", "undo", "redo"]
    assert [(h["old_value"], h["new_value"]) for h in hist] == [("100", "200"), ("200", "100"), ("100", "200")]
    assert all(h["session_id"] == "S" for h in hist)


class _BrokenDb:
    def push_history(self, *a, **k):
        raise RuntimeError("database is locked")

    def update_value(self, *a, **k):
        raise RuntimeError("database is locked")


def test_db_failure_does_not_break_edit_or_undo(qapp, monkeypatch):
    # Qt(C++) から呼ばれる redo/undo で例外が漏れると PyQt6 はアプリを強制終了する。
    # DB が失敗しても画面上の編集・Undo は継続し、ログに記録されること。
    logged = []
    monkeypatch.setattr(server, "append_log", logged.append)
    model, stack = _model(), QUndoStack()
    stack.push(EditValueCommand(model, _BrokenDb(), "S", "E1", "P1", "100", "200"))
    assert model._values[("E1", "P1")] == "200"
    stack.undo()
    assert model._values[("E1", "P1")] == "100"
    assert [m.split()[0:2] for m in logged] == [["[UNDO", "DB"], ["[UNDO", "DB"]]
    assert "user" in logged[0] and "undo" in logged[1] and "database is locked" in logged[0]


def test_log_without_server_module_is_silent(monkeypatch):
    import sys
    import undo_commands
    monkeypatch.delitem(sys.modules, "server")
    monkeypatch.setattr(sys.modules["__main__"], "append_log", None, raising=False)
    undo_commands._log("どこにも出力されないが例外にもならない")
