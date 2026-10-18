"""MainWindow の操作フローのテスト。

ArchiCAD への送信 (send_to_ac_async) は conftest で記録用スタブに差し替え済み。
モーダルなダイアログ・メニューは自動応答に差し替えて検証する。
"""
import socket
import threading
import time

import pytest
from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtWidgets import QMenu, QMessageBox, QTreeWidgetItemIterator

import server

# conftest の fixture がクラス属性を差し替える前の本物を保持しておく
ORIG_SEND = server.MainWindow.send_to_ac_async
ORIG_START_POLL = server.MainWindow._start_floor_polling

G1, G2, G3 = "AAAAAAAA-1111", "BBBBBBBB-2222", "CCCCCCCC-3333"
ELEMS = [{"guid": G1, "type": "Wall"}, {"guid": G2, "type": "Wall"}, {"guid": G3, "type": "Wall"}]
PROPS = [
    {"guid": "builtin:element_id", "name": "ID", "group": "IDとカテゴリ"},
    {"guid": "P-WIDTH", "name": "幅", "group": "形状"},
    {"guid": "P-FIRE", "name": "耐火", "group": "一般", "enums": ["1時間", "2時間"]},
]


def _values_payload(stamps=(10, 20, 30), width=("100", "200", "300")):
    return {"type": "property_values", "values": [
        {"guid": g, "modiStamp": s,
         "props": {"builtin:element_id": f"W-{i}", "P-WIDTH": w, "P-FIRE": "1時間"}}
        for i, (g, s, w) in enumerate(zip((G1, G2, G3), stamps, width), start=1)
    ]}


@pytest.fixture
def loaded(main_window):
    """要素 3 件 × プロパティ 3 列を取得済みの状態。"""
    w = main_window
    w.current_condition_id = w.db.save_condition([0], {"Wall": True})
    w.current_elements = list(ELEMS)
    w.current_props = list(PROPS)
    w.on_values(_values_payload())
    w.sent.clear()
    return w


def _edit(w, guid, prop, value):
    row = next(i for i, e in enumerate(w.model._elements) if e["guid"] == guid)
    col = next(i for i, p in enumerate(w.model._properties) if p["guid"] == prop) + 1
    assert w.model.setData(w.model.index(row, col), value)


# ── 初期化 ─────────────────────────────────────────────────────────

def test_init_wires_model_and_loads_dialog_order(main_window):
    w = main_window
    assert w.model._db is w.db and w.model._undo_stack is w.undo_stack
    assert w.windowTitle() == "Archicad BIM - 条件設定"
    assert isinstance(w._group_order, list)


def test_init_without_dialog_order_file(qapp, tmp_path, monkeypatch, msgbox):
    import db_manager
    monkeypatch.setattr(db_manager, "DB_PATH", tmp_path / "x.db")
    monkeypatch.setattr(server.MainWindow, "_start_floor_polling", lambda self: None)
    real_open = server.Path.open

    def fake_open(self, *a, **k):
        if self.name == "dialog_order.json":
            raise FileNotFoundError("missing")
        return real_open(self, *a, **k)

    monkeypatch.setattr(server.Path, "open", fake_open)
    w = server.MainWindow()
    try:
        assert w._group_order == [] and w._type_overrides == {}
    finally:
        w.db.close()


def test_restore_conflict_flags_on_startup(qapp, tmp_path, monkeypatch, msgbox):
    import db_manager
    path = tmp_path / "flags.db"
    pre = db_manager.DbManager(path)
    pre.save_conflict_flags([{"element_guid": G1, "property_guid": "P-WIDTH",
                              "orig_val": "1", "ac_val": "2", "user_val": "3"}])
    pre.close()
    monkeypatch.setattr(db_manager, "DB_PATH", path)
    monkeypatch.setattr(server.MainWindow, "_start_floor_polling", lambda self: None)
    w = server.MainWindow()
    try:
        assert w.model._value_conflict_cells[(G1, "P-WIDTH")]["ac_val"] == "2"
        assert G1 in w.model._value_conflict_guids
        assert w.btn_clear_flags.isEnabled()
    finally:
        w.db.close()


def test_restore_conflict_flags_handles_db_error(main_window, monkeypatch):
    def boom():
        raise RuntimeError("db broken")
    monkeypatch.setattr(main_window.db, "load_uncleared_flags", boom)
    main_window._restore_conflict_flags()  # 例外を外に出さない
    assert not main_window.btn_clear_flags.isEnabled()


def test_show_all_windows(main_window):
    main_window._show_all_windows()
    assert main_window.isVisible()


# ── 条件設定 → 要素抽出 → プロパティ一覧 → データ取得 ─────────────

def test_full_fetch_flow(main_window):
    w = main_window
    w.on_config({"stories": [{"index": -1, "name": "PIT"}, {"index": 0, "name": "1FL"}],
                 "elementTypes": {"Wall": 3, "Column": 5}})
    root_stories = w.filter_tree.topLevelItem(0)
    root_types = w.filter_tree.topLevelItem(1)
    assert root_stories.text(0) == "階層" and root_stories.childCount() == 2
    assert root_types.child(1).text(0) == "Column (5)"

    root_stories.child(1).setCheckState(0, Qt.CheckState.Checked)
    root_types.child(0).setCheckState(0, Qt.CheckState.Checked)
    w.search_elements()
    cmd = w.sent.last("get_elements")
    assert cmd["stories"] == [0] and cmd["Wall"] is True and cmd["Column"] is False
    assert w.condition_status_label.text() == "要素抽出中..."
    assert w.db.get_conditions()[0]["id"] == w.current_condition_id

    w.on_elements({"elements": ELEMS})
    assert w._current_element_type == "Wall"
    assert w.sent.last("get_definitions")["guids"] == [G1]

    w.on_definitions({"definitions": PROPS})
    assert w.prop_tree.topLevelItemCount() == 3
    assert "プロパティ一覧取得完了" in w.condition_status_label.text()

    # グループのチェックで子もまとめてチェックされる
    group = next(w.prop_tree.topLevelItem(i) for i in range(3)
                 if w.prop_tree.topLevelItem(i).text(0) == "形状")
    group.setCheckState(0, Qt.CheckState.Checked)
    assert group.child(0).checkState(0) == Qt.CheckState.Checked
    w.get_values()
    req = w.sent.last("get_values")
    assert req["guids"] == [G1, G2, G3] and req["propGuids"] == ["P-WIDTH"]

    w.on_values({"values": [{"guid": G1, "modiStamp": 7, "props": {"P-WIDTH": "100"}}]})
    assert w.model.rowCount() == 3 and w.model.columnCount() == 2
    assert w.model._stamps[G1] == 7
    assert w.data_panel.isVisible()
    assert w.btn_confirm_all.isEnabled() and w.btn_diff.isEnabled()
    assert w.db.load_values([G1]) == {(G1, "P-WIDTH"): "100"}


def test_on_elements_empty_and_mixed_types(main_window):
    w = main_window
    w.on_elements({"elements": []})
    assert "該当なし" in w.condition_status_label.text()
    assert "get_definitions" not in w.sent.commands()
    w.on_elements({"elements": [{"guid": G1, "type": "Wall"}, {"guid": G2, "type": "Beam"}]})
    assert w._current_element_type == ""


def test_db_errors_are_swallowed(main_window, monkeypatch):
    w = main_window

    def boom(*a, **k):
        raise RuntimeError("db down")

    for name in ("save_condition", "upsert_elements", "upsert_properties", "upsert_values"):
        monkeypatch.setattr(w.db, name, boom)
    w.search_elements()
    w.current_condition_id = 1
    w.on_elements({"elements": ELEMS})
    w.on_definitions({"definitions": PROPS})
    w.current_props = PROPS
    w.on_values(_values_payload())  # どれも例外を外に出さない
    assert w.model.rowCount() == 3


def test_get_values_without_selection_sends_nothing(main_window):
    main_window.get_values()
    assert main_window.sent == []


def test_sort_props_by_dialog_order(main_window):
    w = main_window
    w._group_order = ["B", "A"]
    w._type_overrides = {"Beam": ["A", "B"]}
    props = [{"group": "A", "name": "2"}, {"group": "Z", "name": "1"},
             {"group": "B", "name": "x"}, {"group": "A", "name": "1"}]
    assert [(p["group"], p["name"]) for p in w._sort_props_by_dialog_order(props)] == \
        [("B", "x"), ("A", "1"), ("A", "2"), ("Z", "1")]
    assert w._sort_props_by_dialog_order(props, "Beam")[0]["group"] == "A"


def test_enum_delegates_applied_per_column(loaded):
    assert isinstance(loaded.table.itemDelegateForColumn(3), server.EnumComboBoxDelegate)
    assert isinstance(loaded.table.itemDelegateForColumn(2), server.PlaceholderClearDelegate)


# ── 編集・Undo/Redo・差分 ─────────────────────────────────────────

def test_edit_undo_redo_buttons(loaded):
    w = loaded
    _edit(w, G1, "P-WIDTH", "150")
    assert w.btn_undo.isEnabled() and w.btn_undo.text().startswith("戻る: 編集: 幅")
    w._do_undo()
    assert w.model._values[(G1, "P-WIDTH")] == "100"
    assert w.btn_redo.isEnabled()
    w._do_redo()
    assert w.model._values[(G1, "P-WIDTH")] == "150"
    assert [h["source"] for h in w.db.get_history(ascending=True)] == ["user", "undo", "redo"]


def test_show_diff_dialog(loaded, monkeypatch):
    shown = []
    monkeypatch.setattr(server.DiffDialog, "exec", lambda self: shown.append(self) or 0)
    _edit(loaded, G1, "P-WIDTH", "150")
    loaded._selected_guids = [G1]
    loaded.show_diff_dialog()
    assert shown[0]._count_label.text() == "履歴: 1 件"
    assert shown[0]._tbl.item(0, 1).text() == "幅"  # properties との JOIN で名前が出る


def test_show_diff_dialog_error(loaded, monkeypatch):
    monkeypatch.setattr(loaded.db, "get_history", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    loaded.show_diff_dialog()
    assert loaded.msgbox.kinds() == ["warning"]


def test_double_click_starts_edit_only_on_value_columns(loaded, monkeypatch):
    edited = []
    monkeypatch.setattr(loaded.table, "edit", lambda idx: edited.append(idx.column()))
    loaded._on_table_double_clicked(loaded.proxy.index(0, 0))
    loaded._on_table_double_clicked(loaded.proxy.index(0, 2))
    assert edited == [2]


# ── 選択連動・ズーム ──────────────────────────────────────────────

def test_table_click_selects_in_archicad(loaded):
    w = loaded
    w.on_table_clicked(w.proxy.index(1, 0))
    assert w._selected_guids == [G2] and w.model._selected_guid == G2
    assert w.sent.last("select_elements")["guids"] == [G2]
    assert "zoom_to_element" not in w.sent.commands()
    assert w.status_label.text().startswith("選択中: W-2")
    assert w.btn_confirm.isEnabled()

    w.btn_zoom_toggle.setChecked(True)
    w.zoom_slider.setValue(25)
    assert w.zoom_factor_label.text() == "2.5x" and w.btn_zoom_toggle.text() == "ズーム ON"
    w.on_table_clicked(w.proxy.index(0, 0))
    assert w.sent.last("zoom_to_element") == {"command": "zoom_to_element", "guids": [G1], "zoomFactor": 2.5}
    w.btn_zoom_toggle.setChecked(False)
    assert not w.zoom_slider.isEnabled()


def test_table_click_invalid_row_and_error(loaded, monkeypatch):
    w = loaded
    w.on_table_clicked(w.proxy.index(99, 0))  # 範囲外
    assert w.sent == []
    monkeypatch.setattr(w.proxy, "mapToSource", lambda i: (_ for _ in ()).throw(RuntimeError("x")))
    w.on_table_clicked(w.proxy.index(0, 0))  # 例外はログのみ
    assert w.sent == []


def test_archicad_selection_sync(loaded):
    w = loaded
    w.on_archicad_selection_changed({"guid": G3})
    assert w._selected_guids == [G3] and w.status_label.text().startswith("[AC選択] W-3")
    w.on_archicad_selection_changed({"guid": "NOT-IN-TABLE"})
    w.on_archicad_selection_changed({})
    assert w._selected_guids == [G3]


def test_archicad_selection_error_is_logged(loaded, monkeypatch):
    monkeypatch.setattr(loaded.model, "set_selected_guid", lambda g: (_ for _ in ()).throw(RuntimeError("x")))
    loaded.on_archicad_selection_changed({"guid": G1})  # 例外を外に出さない


def test_zoom_fit_all(main_window):
    main_window.zoom_fit_all()
    assert main_window.sent.commands() == ["zoom_fit_all"]


# ── フィルタメニュー ──────────────────────────────────────────────

class _MenuDriver:
    """QMenu.exec を差し替え、指定テキストのアクションを実行してから閉じる。"""

    def __init__(self, monkeypatch, pick=(), uncheck=()):
        self.pick, self.uncheck = list(pick), list(uncheck)
        # 束縛メソッドはクラス属性にしても self(menu) を受け取れないため関数で包む
        monkeypatch.setattr(server.QMenu, "exec", lambda menu, *a: self._exec(menu, *a))

    def _all_actions(self, menu):
        for a in menu.actions():
            yield a
            if a.menu():
                yield from self._all_actions(a.menu())

    def _exec(self, menu, *args):
        acts = {a.text(): a for a in self._all_actions(menu)}
        for t in self.uncheck:
            acts[t].setChecked(False)
        for t in self.pick:
            acts[t].trigger()
        menu.aboutToHide.emit()


def test_filter_menu_value_filter(loaded, monkeypatch):
    w = loaded
    _MenuDriver(monkeypatch, uncheck=["200"])
    w.show_filter_menu(QPoint(w.table.horizontalHeader().sectionViewportPosition(2) + 2, 2))
    assert w.proxy.rowCount() == 2
    _MenuDriver(monkeypatch, pick=["すべて選択"])
    w.show_filter_menu(QPoint(w.table.horizontalHeader().sectionViewportPosition(2) + 2, 2))
    assert w.proxy.rowCount() == 3


@pytest.mark.parametrize("action, answers, expected_rows", [
    ("≧ 条件", [(200.0, True)], 2),
    ("≦ 条件", [(200.0, True)], 2),
    ("＝ 条件", [(300.0, True)], 1),
    ("範囲指定", [(150.0, True), (250.0, True)], 1),
    ("範囲指定", [(150.0, False)], 3),          # 最小値入力をキャンセル
    ("≧ 条件", [(999.0, False)], 3),            # キャンセル
])
def test_filter_menu_numeric(loaded, monkeypatch, action, answers, expected_rows):
    w = loaded
    w.table.horizontalHeader().resize(1000, 30)
    it = iter(answers)
    monkeypatch.setattr(server.QInputDialog, "getDouble", staticmethod(lambda *a, **k: next(it)))
    _MenuDriver(monkeypatch, pick=[action])
    pos = QPoint(w.table.horizontalHeader().sectionViewportPosition(2) + 2, 2)
    w.show_filter_menu(pos)
    assert w.proxy.rowCount() == expected_rows
    _MenuDriver(monkeypatch, pick=["数値フィルタ解除"])
    w.show_filter_menu(pos)
    assert w.proxy.rowCount() == 3


def test_filter_menu_outside_columns(loaded, monkeypatch):
    called = []
    monkeypatch.setattr(server.QMenu, "exec", lambda *a: called.append(1))
    loaded.show_filter_menu(QPoint(-50, -50))
    assert called == []


# ── 反映（sync）と更新前チェック ──────────────────────────────────

def test_sync_without_changes_informs(loaded):
    loaded.sync_data()
    assert loaded.msgbox.kinds() == ["information"]
    assert loaded.sent == []


def test_sync_user_declines(loaded):
    _edit(loaded, G1, "P-WIDTH", "150")
    loaded.msgbox.question_answer = QMessageBox.StandardButton.No
    loaded.sync_data()
    assert loaded.sent == [] and loaded.btn_sync.isEnabled()


def test_sync_builds_policies_and_requests_verification(loaded):
    w = loaded
    _edit(w, G1, "P-WIDTH", "150")             # 形状 → parameter → AbortAll
    _edit(w, G2, "P-FIRE", "2時間")            # 一般 → property → SkipConflicts
    _edit(w, G3, "builtin:element_id", "NEW")  # ID → ForceOverwrite
    w.model._stamp_flags[G1] = server.PropertyTableModel.STAMP_STALE
    w.model._stamp_flags[G2] = server.PropertyTableModel.STAMP_UNKNOWN
    w.model._stamps[G3] = "bad"                # 数値化できない stamp は 0 扱い
    w._last_archicad_floor = 2
    w.sync_data()

    kind, title, text = w.msgbox.calls[-1]
    assert kind == "question"
    for s in ("AbortAll (厳密): 1件", "SkipConflicts (属性): 1件", "ForceOverwrite (強制): 1件",
              "stamp不一致", "stamp未確認"):
        assert s in text
    req = w.sent.last("get_values")
    assert set(req["guids"]) == {G1, G2, G3}
    assert w._verifying_stamps and not w.btn_sync.isEnabled()
    assert w._floor_before_op == 2
    by_guid = {c["guid"]: c for c in w._pending_changes}
    assert by_guid[G3]["specialType"] == "element_id" and by_guid[G3]["modiStamp"] == 0
    assert by_guid[G1]["group"] == "parameter" and by_guid[G1]["modiStamp"] == 10


def test_pre_verify_no_conflict_applies(loaded):
    w = loaded
    _edit(w, G1, "P-WIDTH", "150")
    w.sync_data()
    w.sent.clear()
    w.on_values(_values_payload(stamps=(11, 20, 30)))  # 検証中の応答は pre-verify に回る
    assert w.sent.commands() == ["apply_changes"]
    applied = w.sent[0]["changes"]
    assert applied[0]["modiStamp"] == 11                      # 最新 stamp に更新
    assert w.model._stamp_flags[G1] == server.PropertyTableModel.STAMP_STALE
    assert w.btn_sync.isEnabled() and not w._verifying_stamps


class _ConflictBoxDriver:
    def __init__(self, monkeypatch, button_prefix):
        self.prefix, self.texts = button_prefix, []
        monkeypatch.setattr(server.QMessageBox, "exec", lambda box: self._exec(box))
        monkeypatch.setattr(server.QMessageBox, "clickedButton", lambda box: box._picked)

    def _exec(self, box):
        self.texts.append(box.text())
        box._picked = next(b for b in box.buttons() if b.text().startswith(self.prefix))
        return 0


def _prepare_conflict(w):
    _edit(w, G1, "P-WIDTH", "150")
    _edit(w, G2, "P-WIDTH", "250")
    w.sync_data()
    w.sent.clear()
    # G1 の幅が ArchiCAD 側で 100 → 111 に変わっていた
    w.on_values(_values_payload(width=("111", "200", "300")))


def test_pre_verify_conflict_overwrite_all(loaded, monkeypatch):
    drv = _ConflictBoxDriver(monkeypatch, "全て上書き")
    _prepare_conflict(loaded)
    assert "取得時: 100  ／  ArchiCAD現在: 111  ／  あなたの編集: 150" in drv.texts[0]
    assert loaded.sent.commands() == ["apply_changes"]
    assert len(loaded.sent[0]["changes"]) == 2


def test_pre_verify_conflict_skip(loaded, monkeypatch):
    _ConflictBoxDriver(monkeypatch, "競合をスキップ")
    w = loaded
    _prepare_conflict(w)
    assert w.sent.commands() == ["mark_change_flags", "apply_changes"]
    assert [c["guid"] for c in w.sent.last("apply_changes")["changes"]] == [G2]
    assert w.sent.last("mark_change_flags")["guids"] == [G1]
    assert (G1, "P-WIDTH") in w.model._value_conflict_cells
    assert w.btn_clear_flags.isEnabled() and w._pending_skip_count == 1
    assert [f["element_guid"] for f in w.db.load_uncleared_flags()] == [G1]


def test_pre_verify_all_conflicts_skip_cancels_apply(loaded, monkeypatch):
    _ConflictBoxDriver(monkeypatch, "競合をスキップ")
    w = loaded
    _edit(w, G1, "P-WIDTH", "150")
    w.sync_data()
    w.sent.clear()
    w.on_values(_values_payload(width=("111", "200", "300")))
    assert w.sent.commands() == ["mark_change_flags"]
    assert w.status_label.text() == "反映キャンセル（全て競合）"


def test_pre_verify_conflict_cancel(loaded, monkeypatch):
    _ConflictBoxDriver(monkeypatch, "全て中止")
    _prepare_conflict(loaded)
    assert loaded.sent == []
    assert loaded.status_label.text() == "反映を中止しました"


def test_pre_verify_many_conflicts_are_truncated(main_window, monkeypatch):
    drv = _ConflictBoxDriver(monkeypatch, "全て中止")
    w = main_window
    elems = [{"guid": f"G{i:07d}-X"} for i in range(10)]
    w.current_elements, w.current_props = elems, [PROPS[1]]
    w.on_values({"values": [{"guid": e["guid"], "props": {"P-WIDTH": "1"}} for e in elems]})
    for e in elems:
        _edit(w, e["guid"], "P-WIDTH", "2")
    w.sync_data()
    w.on_values({"values": [{"guid": e["guid"], "props": {"P-WIDTH": "9"}} for e in elems]})
    assert "… 他 2 件" in drv.texts[0]


def test_on_error_resets_verification(loaded):
    w = loaded
    _edit(w, G1, "P-WIDTH", "150")
    w.sync_data()
    w.on_error("connection refused")
    assert not w._verifying_stamps and w._pending_changes is None
    assert w.btn_sync.isEnabled() and w.status_label.text() == "接続エラー"
    assert w.msgbox.calls[-1][0] == "warning"


def test_on_error_when_idle(main_window):
    main_window.on_error("x")
    assert main_window.msgbox.kinds() == ["warning"]


# ── 反映結果 ─────────────────────────────────────────────────────

def test_sync_complete_success_applies_override(loaded):
    w = loaded
    _edit(w, G1, "P-WIDTH", "150")
    w._floor_before_op = 3
    w.on_sync_complete({"results": [{"guid": G1, "status": "success", "currentStamp": 99}, {}]})
    assert w.model._original_values[(G1, "P-WIDTH")] == "150"
    assert w.model._stamps[G1] == 99 and w.model._change_status[G1] == 1
    assert w.msgbox.kinds() == ["information"]
    assert w.sent.last("apply_bim_override")["floor"] == 3
    assert w.btn_bim_override.isChecked()


def test_sync_complete_conflict_and_error(loaded):
    w = loaded
    w._pending_skip_count = 2
    w.on_sync_complete({"data": {"results": [
        {"guid": G1, "status": "conflict", "reason": "stamp", "currentStamp": 55},
        {"guid": G2, "status": "skipped", "reason": "locked"},
        {"guid": G3, "status": "error", "reason": "boom", "currentStamp": 77},
    ]}})
    assert w.model._conflicts == {G1: "conflict", G2: "skipped", G3: "error"}
    assert w.model._stamp_flags[G1] == server.PropertyTableModel.STAMP_STALE
    assert w.model._stamps[G3] == 77
    kind, _, text = w.msgbox.calls[-1]
    assert kind == "critical"
    assert "成功:0 / 競合:2 / エラー:1 / stamp変化:2 / 競合スキップ:2" in text
    assert "apply_bim_override" not in w.sent.commands()


def test_sync_complete_partial_warning(loaded):
    loaded.on_sync_complete({"results": [{"guid": G1, "status": "skipped"},
                                         {"guid": G2, "status": "error"}]})
    loaded.msgbox.calls.clear()
    loaded.on_sync_complete({"results": [{"guid": G1, "status": "skipped"}]})
    assert loaded.msgbox.kinds() == ["warning"]


def test_sync_complete_without_results(loaded):
    loaded.on_sync_complete({"type": "sync_complete"})
    assert loaded.msgbox.kinds() == ["warning"]
    assert loaded.status_label.text() == "待機中"


def test_sync_complete_internal_error(loaded, monkeypatch):
    monkeypatch.setattr(loaded.model, "refresh_view", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    loaded.on_sync_complete({"results": []})
    assert loaded.msgbox.kinds() == ["critical"]


# ── 競合フラグ・ArchiCAD フラグ結果 ───────────────────────────────

def test_clear_value_conflict_flags(loaded):
    w = loaded
    w.db.save_conflict_flags([{"element_guid": G1, "property_guid": "P-WIDTH"}])
    w.model._value_conflict_cells[(G1, "P-WIDTH")] = {}
    w.model._value_conflict_guids.add(G1)
    w.btn_clear_flags.setEnabled(True)
    w.clear_value_conflict_flags()
    assert w.sent.last("clear_change_flags")["guids"] == [G1]
    assert w.db.load_uncleared_flags() == []
    assert not w.model._value_conflict_guids and not w.btn_clear_flags.isEnabled()


def test_clear_value_conflict_flags_db_error_and_empty(loaded, monkeypatch):
    w = loaded
    w.clear_value_conflict_flags()  # フラグ無し → 送信しない
    assert w.sent == []
    w.model._value_conflict_guids.add(G1)
    monkeypatch.setattr(w.db, "clear_conflict_flags", lambda g: (_ for _ in ()).throw(RuntimeError("x")))
    w.clear_value_conflict_flags()
    assert w.sent.commands() == ["clear_change_flags"]


@pytest.mark.parametrize("data, text", [
    ({"status": "ok", "created": True, "propCount": 3}, "「変更フラグ」プロパティを自動作成 → 3件反映"),
    ({"status": "ok", "propCount": 2}, "ArchiCADフラグ: 2件反映"),
    ({"status": "ok"}, "ArchiCADフラグ解除完了"),
    ({"status": "error", "reason": "locked"}, "ArchiCADフラグエラー: locked"),
])
def test_on_flag_result(main_window, data, text):
    main_window.on_flag_result(data)
    assert text in main_window.status_label.text()


# ── BIM 表現の上書き ─────────────────────────────────────────────

def test_toggle_bim_override(main_window):
    w = main_window
    w.btn_bim_override.setChecked(True)
    w.toggle_bim_override()
    assert w.sent[-1] == {"command": "apply_bim_override", "floor": -1}
    w._last_archicad_floor = 0
    w.btn_bim_override.setChecked(False)
    w.toggle_bim_override()
    assert w.sent[-1] == {"command": "remove_bim_override", "floor": 0}
    assert w.btn_bim_override.text() == "変更強調 OFF"


def test_bim_override_setup_results(main_window):
    w = main_window
    w.on_bim_override_result({"action": "setup", "status": "ok", "propCreated": True,
                              "combinationCreated": True, "changeStatusGuid": "CS-GUID"})
    assert "ChangeStatusプロパティ自動作成 / 表現の上書き自動作成" in w.status_label.text()
    assert "CS-GUID" in w.msgbox.calls[-1][2]
    w.on_bim_override_result({"action": "setup", "status": "ok"})
    assert w.status_label.text() == "BIM表現の上書き設定完了"
    w.on_bim_override_result({"action": "setup", "status": "error", "reason": "r"})
    assert w.status_label.text().endswith("エラー: r")


def test_bim_override_apply_results_restore_floor(main_window):
    w = main_window
    w._floor_before_op = 4
    w.on_bim_override_result({"action": "applied", "status": "ok", "viewCount": 3})
    assert w.status_label.text() == "変更強調 ON: 3ビュー更新"
    assert w.sent.last("goto_floor")["floor"] == 4 and w._floor_before_op is None
    w.on_bim_override_result({"action": "removed", "status": "ok"})
    assert w.status_label.text() == "変更強調 OFF: 0ビュー更新"
    w.on_bim_override_result({"action": "removed", "status": "error", "reason": "x"})
    assert w.status_label.text() == "変更強調エラー: x"
    assert w.sent.commands().count("goto_floor") == 1
    w.on_bim_override_result({"action": "unknown"})  # 何もしない


# ── ChangeStatus ────────────────────────────────────────────────

def test_mark_confirmed_and_reset_status(loaded):
    w = loaded
    w.mark_confirmed()
    w.reset_change_status()
    assert w.sent == []  # 未選択では何もしない
    w._selected_guids = [G1]
    w.mark_confirmed()
    assert w.sent[-1] == {"command": "set_change_status", "guids": [G1], "status": 2}
    w.on_change_status_result({"status": "ok", "value": 2, "set": 1})
    assert w.model._change_status == {G1: 2}
    assert w.status_label.text() == "ChangeStatus=確認済: 1件 設定完了"
    w.reset_change_status()
    w.on_change_status_result({"status": "ok", "value": 0, "set": 1})
    assert w.model._change_status == {}


def test_reset_all_table_flow(loaded):
    w = loaded
    w._last_archicad_floor = 1
    w.btn_bim_override.setChecked(True)
    w.reset_all_table()
    assert w.sent[-1]["guids"] == [G1, G2, G3] and w._pending_bim_restore
    w.on_change_status_result({"status": "ok", "value": 0, "set": 3})
    assert w.sent[-1] == {"command": "remove_bim_override", "floor": 1}
    assert not w.btn_bim_override.isChecked() and not w._pending_bim_restore


def test_reset_all_table_declined_or_empty(main_window):
    main_window.reset_all_table()  # テーブル空
    assert main_window.msgbox.calls == []


def test_reset_all_table_declined(loaded):
    loaded.msgbox.question_answer = QMessageBox.StandardButton.No
    loaded.reset_all_table()
    assert loaded.sent == []


def test_change_status_error_and_unknown_value(loaded, monkeypatch):
    w = loaded
    w._pending_bim_restore = True
    w._pending_status_change = {"guids": [G1], "value": 2}
    w.on_change_status_result({"status": "error", "reason": "no prop"})
    assert w._pending_status_change is None and not w._pending_bim_restore
    assert w.status_label.text() == "ChangeStatusエラー: no prop"
    w.on_change_status_result({"status": "ok", "value": 7})
    assert w.status_label.text() == "ChangeStatus=7: 0件 設定完了"
    monkeypatch.setattr(w.status_label, "setText", lambda t: (_ for _ in ()).throw(RuntimeError("x")))
    w.on_change_status_result({"status": "ok"})  # 例外はログのみ


# ── フロア ───────────────────────────────────────────────────────

@pytest.mark.parametrize("data, expected", [
    ({"floor": 0, "valid": True}, 0),      # 1FL(0) も有効
    ({"floor": 3}, 3),                     # 旧プロトコル（非ゼロのみ信用）
    ({"floor": 0}, None),                  # 旧プロトコルの 0 は無視
    ({"floor": -1, "valid": False}, None),
])
def test_on_current_floor(main_window, data, expected):
    main_window.on_current_floor(data)
    assert main_window._last_archicad_floor == expected


def test_change_event_tracks_activation(main_window):
    main_window.changeEvent(QEvent(QEvent.Type.ActivationChange))
    assert main_window._pyqt_active is False


# ── 本物の send_to_ac_async / フロア問い合わせ（ソケットを差し替え）──

class _RecordingSocket:
    instances = []
    mode = "ack"

    def __init__(self, *a):
        self.sent, self.addr = b"", None
        _RecordingSocket.instances.append(self)
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def settimeout(self, t): pass
    def connect(self, addr):
        if self.mode == "refused":
            raise ConnectionRefusedError("refused")
        self.addr = addr
    def sendall(self, b): self.sent += b
    def recv(self, n):
        if self.mode == "noack":
            raise socket.timeout()
        return b"OK\n"


class _SockMod:
    AF_INET, SOCK_STREAM, timeout = socket.AF_INET, socket.SOCK_STREAM, socket.timeout
    socket = _RecordingSocket


@pytest.fixture
def fake_sock(monkeypatch):
    _RecordingSocket.instances.clear()
    _RecordingSocket.mode = "ack"
    monkeypatch.setattr(server, "socket", _SockMod)
    return _RecordingSocket


def _wait(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


@pytest.mark.parametrize("mode", ["ack", "noack"])
def test_real_send_to_ac_async(main_window, fake_sock, mode):
    fake_sock.mode = mode
    ORIG_SEND(main_window, {"command": "get_values", "名前": "壁"})
    assert _wait(lambda: fake_sock.instances and fake_sock.instances[0].sent)
    s = fake_sock.instances[0]
    assert s.addr == ("127.0.0.1", 5001)
    assert s.sent == '{"command":"get_values","名前":"壁"}\n'.encode("utf-8")


def test_real_send_to_ac_async_connection_error(main_window, fake_sock, qapp):
    fake_sock.mode = "refused"
    errors = []
    main_window.signals.error_occurred.disconnect()
    main_window.signals.error_occurred.connect(errors.append)
    ORIG_SEND(main_window, {"command": "x"})
    assert _wait(lambda: (qapp.processEvents(), errors)[1])
    assert "refused" in errors[0]


def test_query_and_store_floor(main_window, fake_sock):
    main_window._query_and_store_floor()
    assert fake_sock.instances[0].sent == b'{"command":"get_current_floor"}\n'
    fake_sock.mode = "refused"
    main_window._query_and_store_floor()  # 失敗はログのみ


def test_floor_polling_loop(main_window, monkeypatch):
    class _Stop(BaseException):
        pass

    captured = {}

    class _FakeThread:
        name = "poll"

        def __init__(self, target, daemon):
            captured["target"] = target

        def start(self):
            pass  # 実スレッドは起動せず、下で target を直接呼ぶ

    monkeypatch.setattr(server.threading, "Thread", _FakeThread)
    ORIG_START_POLL(main_window)
    calls = []
    sleeps = iter([None, None, _Stop])

    def fake_sleep(sec):
        nxt = next(sleeps)
        if nxt is _Stop:
            raise _Stop()

    monkeypatch.setattr(time, "sleep", fake_sleep)
    results = iter([None, RuntimeError("socket")])

    def fake_query():
        calls.append(1)
        r = next(results)
        if r:
            raise r

    main_window._query_and_store_floor = fake_query
    with pytest.raises(_Stop):
        captured["target"]()
    assert calls == [1, 1]  # 2 回目の例外でもループは継続した


# ── 終了 ────────────────────────────────────────────────────────

def test_close_event_stops_server_and_purges(main_window):
    stopped = []
    main_window.server_thread = type("S", (), {"stop": lambda self: stopped.append(True)})()
    main_window.close()
    assert stopped == [True]
    assert main_window.db._local.conn is None


def test_close_event_db_error_ignored(main_window, monkeypatch):
    monkeypatch.setattr(main_window.db, "purge_old_history",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    main_window.close()
    assert not main_window.isVisible()
