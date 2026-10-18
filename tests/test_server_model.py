"""server.py の純粋関数 / PropertyTableModel / ExcelFilterProxyModel のテスト。"""
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QStandardItem, QStandardItemModel, QUndoStack

import server

D = Qt.ItemDataRole


# ── 純粋関数 ───────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [(None, ""), ("  ID ", "id"), (5, "5"), ("", "")])
def test_normalize_label(value, expected):
    assert server.normalize_label(value) == expected


@pytest.mark.parametrize("prop_def, guid, expected", [
    ({}, "builtin:element_id", True),
    ({}, "BUILTIN:ID", True),
    ({}, "builtin:elementid", True),
    ({"name": "ID", "group": "IDとカテゴリ"}, "x", True),
    ({"name": "要素ID", "group": "IDとカテゴリ"}, "x", True),
    ({"name": "Element ID", "group": "ID and Categories"}, "x", True),
    ({"name": "My element id", "group": "Category & ID"}, "x", True),
    ({"name": "ID", "group": "カテゴリとID"}, "x", True),
    ({"name": "ID", "group": "一般"}, "x", False),          # グループが違う
    ({"name": "名前", "group": "IDとカテゴリ"}, "x", False),  # 名前が違う
])
def test_is_element_id_property(prop_def, guid, expected):
    assert server.is_element_id_property(prop_def, guid) is expected


@pytest.mark.parametrize("p_def, guid, expected", [
    ({}, "builtin:element_id", "element_id"),
    ({}, "builtin:RenovationStatus", "parameter"),
    ({}, "builtin:StructuralFunction", "category"),
    ({}, "builtin:Position", "category"),
    ({}, "builtin:Layer", "element"),
    ({}, "builtin:Other", "parameter"),
    ({"group": "IDとカテゴリ"}, "G", "classification"),
    ({"group": "Classification"}, "G", "classification"),
    ({"group": "レイヤ"}, "G", "element"),
    ({"group": "形状"}, "G", "parameter"),
    ({"group": "面と材質"}, "G", "attribute"),
    ({"group": "一般"}, "G", "property"),
    ({}, "G", "property"),
])
def test_get_group_type(p_def, guid, expected):
    assert server.get_group_type(p_def, guid) == expected


# ── ExcelFilterProxyModel ──────────────────────────────────────────

def _source(rows):
    m = QStandardItemModel()
    for r in rows:
        m.appendRow([QStandardItem(str(v)) for v in r])
    return m


def _visible(proxy, col=0):
    return [proxy.index(r, col).data() for r in range(proxy.rowCount())]


def test_proxy_value_filter(qapp):
    p = server.ExcelFilterProxyModel()
    p.setSourceModel(_source([["A"], ["B"], ["C"]]))
    assert _visible(p) == ["A", "B", "C"]
    p.set_value_filter(0, ["A", "C"])
    assert _visible(p) == ["A", "C"]


@pytest.mark.parametrize("flt, expected", [
    (("gte", 2), ["2", "3", "10"]),
    (("lte", 2), ["1", "2"]),
    (("eq", 3), ["3"]),
    (("range", 2, 3), ["2", "3"]),
])
def test_proxy_numeric_filters(qapp, flt, expected):
    p = server.ExcelFilterProxyModel()
    p.setSourceModel(_source([["1"], ["2"], ["3"], ["10"]]))
    p.set_numeric_filter(0, flt)
    assert _visible(p) == expected


def test_proxy_numeric_filter_hides_non_numbers_and_clears(qapp):
    p = server.ExcelFilterProxyModel()
    p.setSourceModel(_source([["5"], ["abc"], ["---"]]))
    p.set_numeric_filter(0, ("gte", 0))
    assert _visible(p) == ["5"]
    p.clear_numeric_filter(0)
    assert _visible(p) == ["5", "abc", "---"]
    p.clear_numeric_filter(0)  # 未設定列の解除は何もしない


# ── PropertyTableModel ─────────────────────────────────────────────

ELEMS = [{"guid": "AAAAAAAA-1111"}, {"guid": "BBBBBBBB-2222"}, {"guid": "CCCCCCCC-3333"}]
PROPS = [{"guid": "P1", "name": "幅"}, {"guid": "P2", "name": "名前"}]


@pytest.fixture
def model(qapp):
    m = server.PropertyTableModel()
    m.set_data(ELEMS, PROPS,
               {("AAAAAAAA-1111", "P1"): "100", ("AAAAAAAA-1111", "P2"): "壁A"},
               stamps={"AAAAAAAA-1111": 5, "BBBBBBBB-2222": 6})
    return m


def _bg(m, row, col):
    v = m.index(row, col).data(D.BackgroundRole)
    return v.name() if isinstance(v, QColor) else v


def test_dimensions_and_headers(model):
    assert model.rowCount() == 3
    assert model.columnCount() == 3
    assert model.headerData(0, Qt.Orientation.Horizontal) == "ID"
    assert model.headerData(2, Qt.Orientation.Horizontal) == "名前"
    assert model.headerData(0, Qt.Orientation.Vertical) is None


def test_display_values(model):
    assert model.index(0, 0).data() == "AAAAAAAA"
    assert model.index(0, 1).data() == "100"
    assert model.index(1, 1).data() == ""
    assert model.data(model.index(99, 0)) is None


def test_stamp_flags_from_set_data(model):
    assert model._stamp_flags == {
        "AAAAAAAA-1111": "ok", "BBBBBBBB-2222": "ok", "CCCCCCCC-3333": "unknown"}
    assert _bg(model, 2, 0) == "#f0f0f0"
    assert _bg(model, 0, 0) is None


def test_flags(model):
    F = Qt.ItemFlag
    assert model.flags(model.index(0, 0)) == F.ItemIsEnabled | F.ItemIsSelectable  # ID 列: コピー用に選択可
    assert model.flags(model.index(0, 1)) & F.ItemIsEditable


def test_non_editable_property_is_locked_and_gray(qapp):
    m = server.PropertyTableModel()
    m.set_data([{"guid": "E1"}],
               [{"guid": "P1", "name": "RO", "editable": False}, {"guid": "P2", "name": "RW"}], {})
    assert not (m.flags(m.index(0, 1)) & Qt.ItemFlag.ItemIsEditable)
    assert m.flags(m.index(0, 2)) & Qt.ItemFlag.ItemIsEditable
    assert m.index(0, 1).data(D.ForegroundRole).name() == QColor("gray").name()
    assert m.index(0, 2).data(D.ForegroundRole).name() == QColor("black").name()
    assert m.prop_at(0) is None and m.prop_at(1)["name"] == "RO" and m.prop_at(3) is None


def test_foreground_marks_changed_cells_red(model):
    assert model.index(0, 1).data(D.ForegroundRole).name() == QColor("black").name()
    model.set_value_direct("AAAAAAAA-1111", "P1", "200")
    assert model.index(0, 1).data(D.ForegroundRole).name() == QColor("red").name()
    model._conflicts["AAAAAAAA-1111"] = "conflict"
    assert model.index(0, 0).data(D.ForegroundRole).name() == QColor("darkRed").name()
    assert model.index(0, 2).data(D.ForegroundRole).name() == QColor("darkRed").name()


def test_background_priority(model):
    g = "AAAAAAAA-1111"
    model._stamp_flags[g] = model.STAMP_STALE
    assert _bg(model, 0, 1) == "#fffacd"
    model._change_status[g] = 1
    assert _bg(model, 0, 1) == "#ffd0d0"
    model._change_status[g] = 2
    assert _bg(model, 0, 1) == "#c8f0c8"
    model._value_conflict_guids.add(g)
    assert _bg(model, 0, 0) == "#ffd0a0"
    model._value_conflict_cells[(g, "P1")] = {"orig_val": "o", "ac_val": "a", "user_val": "u"}
    assert _bg(model, 0, 1) == "#ffb347"
    model._conflicts[g] = "skipped"
    assert _bg(model, 0, 1) == "#ffe5cc"
    model._conflicts[g] = "conflict"
    assert _bg(model, 0, 1) == "#ffcccc"
    model.set_selected_guid(g)
    assert _bg(model, 0, 1) == "#aed6f1"


def test_font_bold_on_conflict(model):
    assert not model.index(0, 0).data(D.FontRole).bold()
    model._conflicts["AAAAAAAA-1111"] = "conflict"
    assert model.index(0, 0).data(D.FontRole).bold()


def test_tooltips(model):
    g = "AAAAAAAA-1111"
    assert model.index(0, 0).data(D.ToolTipRole) == "stamp:5"
    assert model.index(2, 0).data(D.ToolTipRole) == "stamp未取得"
    assert model.index(0, 1).data(D.ToolTipRole) is None

    model._value_conflict_cells[(g, "P1")] = {"orig_val": "o", "ac_val": "a", "user_val": "u"}
    model._value_conflict_guids.add(g)
    cell_tip = model.index(0, 1).data(D.ToolTipRole)
    assert "取得時       : o" in cell_tip and "ArchiCAD現在 : a" in cell_tip

    model._conflicts[g] = "conflict"
    model._change_status[g] = 1
    model._stamp_flags[g] = model.STAMP_STALE
    tip = model.index(0, 0).data(D.ToolTipRole)
    assert "競合: conflict" in tip and "値競合フラグあり" in tip
    assert "変更済 (赤)" in tip and "ArchiCAD側で変更済み (stamp:5)" in tip


def test_unknown_role_returns_none(model):
    assert model.index(0, 1).data(D.DecorationRole) is None


def test_set_value_direct_clears_conflict_cell(model):
    g = "AAAAAAAA-1111"
    model._value_conflict_cells[(g, "P1")] = {}
    model._value_conflict_cells[(g, "P2")] = {}
    model._value_conflict_guids.add(g)
    model.set_value_direct(g, "P1", "x")
    assert g in model._value_conflict_guids  # P2 がまだ残っている
    model.set_value_direct(g, "P2", "y")
    assert g not in model._value_conflict_guids
    model.set_value_direct("NOT-IN-TABLE", "P1", "z")  # 行が無くても例外にならない
    assert model._values[("NOT-IN-TABLE", "P1")] == "z"


def test_set_data_emits_data_changed(model):
    seen = []
    model.dataChanged.connect(lambda tl, br, roles: seen.append((tl.row(), tl.column())))
    model.set_value_direct("BBBBBBBB-2222", "P2", "新")
    assert seen == [(1, 2)]


def test_setData_paths_without_undo_stack(model):
    idx = model.index(0, 1)
    assert model.setData(idx, "100") is False            # 値が同じ
    assert model.setData(model.index(0, 0), "x") is False  # ID 列は編集不可
    assert model.setData(idx, "x", D.DisplayRole) is False  # EditRole 以外
    assert model.setData(idx, "150") is True              # undo stack 無し → 直接
    assert model._values[("AAAAAAAA-1111", "P1")] == "150"


def test_setData_pushes_undo_command(model, db):
    db.upsert_elements([{"guid": "AAAAAAAA-1111"}], db.save_condition([], {}))
    db.upsert_properties(PROPS)
    stack = QUndoStack()
    model._undo_stack, model._db, model._session_id = stack, db, "S"
    assert model.setData(model.index(0, 1), "300") is True
    assert stack.canUndo()
    assert db.load_values() == {("AAAAAAAA-1111", "P1"): "300"}
    stack.undo()
    assert model.index(0, 1).data() == "100"


def test_update_change_status_and_refresh(model):
    rows = []
    model.dataChanged.connect(lambda tl, br, roles: rows.append(tl.row()))
    model.update_change_status(["AAAAAAAA-1111", "CCCCCCCC-3333"], 2)
    assert model._change_status == {"AAAAAAAA-1111": 2, "CCCCCCCC-3333": 2}
    assert rows == [0, 2]
    model.update_change_status(["AAAAAAAA-1111"], 0)
    assert model._change_status == {"CCCCCCCC-3333": 2}
    rows.clear()
    model.refresh_view()
    assert rows == [0]


def test_refresh_view_on_empty_model(qapp):
    m = server.PropertyTableModel()
    m.refresh_view()  # 空でも例外にならない
    assert m.rowCount() == 0 and m.columnCount() == 1


def test_set_selected_guid_updates_prev_and_new_rows(model):
    model.set_selected_guid("AAAAAAAA-1111")
    rows = []
    model.dataChanged.connect(lambda tl, br, roles: rows.append(tl.row()))
    model.set_selected_guid("CCCCCCCC-3333")
    assert rows == [0, 2]
