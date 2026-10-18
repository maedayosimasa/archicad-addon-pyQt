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


def test_window_hidden_until_project_config(main_window):
    # 起動直後（project_config 受信前）は表示されない
    assert not main_window.isVisible()
    main_window.signals.show_window.emit()
    assert main_window.isVisible()


def test_show_all_windows_restores_minimized(main_window):
    main_window.show()
    main_window.showMinimized()
    main_window._show_all_windows()
    assert main_window.isVisible() and not main_window.isMinimized()


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
    # GDL パラメータ定義を全ライブラリパーツから集めるため全要素を送る
    assert w.sent.last("get_definitions")["guids"] == [G1, G2, G3]

    w.on_definitions({"definitions": PROPS})
    # 既定は簡易版: 基本情報（ID）だけが出てチェック済み
    assert w.is_simple_mode()
    assert w.prop_tree.topLevelItemCount() == 1
    assert "プロパティ一覧取得完了" in w.condition_status_label.text()

    # 詳細版に切り替えると全プロパティ（チェック状態は引き継ぐ）
    w.prop_mode_combo.setCurrentText(server.PROP_MODE_DETAIL)
    assert w.prop_tree.topLevelItemCount() == 3
    # グループのチェックで子もまとめてチェックされる
    group = next(w.prop_tree.topLevelItem(i) for i in range(3)
                 if w.prop_tree.topLevelItem(i).text(0) == "形状")
    group.setCheckState(0, Qt.CheckState.Checked)
    assert group.child(0).checkState(0) == Qt.CheckState.Checked
    w.get_values()
    req = w.sent.last("get_values")
    assert req["guids"] == [G1, G2, G3] and set(req["propGuids"]) == {"builtin:element_id", "P-WIDTH"}

    w.on_values({"values": [{"guid": G1, "modiStamp": 7, "props": {"P-WIDTH": "100"}}]})
    assert w.model.rowCount() == 3 and w.model.columnCount() == 3   # ID列 + ID + 幅
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


# ── 敷地マネージャー（GDL オブジェクト）────────────────────────────

def test_site_manager_type_label_and_search(main_window):
    w = main_window
    w.on_config({"stories": [], "elementTypes": {"Object": 250, "SiteManager": 3}})
    types = w.filter_tree.topLevelItem(1)
    assert [types.child(i).text(0) for i in range(2)] == ["Object (250)", "敷地マネージャー (3)"]
    site = types.child(1)
    assert site.data(0, Qt.ItemDataRole.UserRole) == "SiteManager"   # 送信キーは英字のまま
    site.setCheckState(0, Qt.CheckState.Checked)
    w.search_elements()
    cmd = w.sent.last("get_elements")
    assert cmd["SiteManager"] is True and cmd["Object"] is False


def test_site_manager_gdl_params_flow(main_window):
    """C++ が返す GDL パラメータ定義（gdl:*）と値を、読み取り専用列として表示できる。"""
    w = main_window
    road, site = "RRRRRRRR-1", "SSSSSSSS-2"
    w.on_elements({"elements": [{"guid": road, "type": "Object"}, {"guid": site, "type": "Object"}]})
    assert w.sent.last("get_definitions")["guids"] == [road, site]

    defs = [
        {"guid": "elem:libPart", "name": "ライブラリパーツ名", "group": "要素設定パラメータ",
         "editable": False, "valueType": 3, "collectionType": 0},
        {"guid": "gdl:iRoadType", "name": "道路種別 (iRoadType) [非表示]", "group": "GDLパラメータ",
         "editable": False, "valueType": 1, "collectionType": 0},
        {"guid": "gdl:strRoadName", "name": "道路名 (strRoadName)", "group": "GDLパラメータ",
         "editable": False, "valueType": 3, "collectionType": 0},
    ]
    w.on_definitions({"definitions": defs})
    w.prop_mode_combo.setCurrentText(server.PROP_MODE_DETAIL)     # GDL パラメータは詳細版に出る
    gdl_group = next(w.prop_tree.topLevelItem(i) for i in range(w.prop_tree.topLevelItemCount())
                     if w.prop_tree.topLevelItem(i).text(0) == "GDLパラメータ")
    assert gdl_group.childCount() == 2
    gdl_group.setCheckState(0, Qt.CheckState.Checked)
    lib_group = next(w.prop_tree.topLevelItem(i) for i in range(w.prop_tree.topLevelItemCount())
                     if w.prop_tree.topLevelItem(i).text(0) == "要素設定パラメータ")
    lib_group.setCheckState(0, Qt.CheckState.Checked)
    w.get_values()
    assert set(w.sent.last("get_values")["propGuids"]) == {"elem:libPart", "gdl:iRoadType", "gdl:strRoadName"}

    w.on_values({"values": [
        {"guid": road, "modiStamp": 1, "props": {"elem:libPart": "道路境界線", "gdl:iRoadType": "公道 (1)",
                                                 "gdl:strRoadName": "市道1号線"}},
        {"guid": site, "modiStamp": 2, "props": {"elem:libPart": "敷地オブジェクト", "gdl:iRoadType": "---",
                                                 "gdl:strRoadName": "---"}},
    ]})
    m = w.model
    col = {p["guid"]: i + 1 for i, p in enumerate(m._properties)}
    assert m.index(0, col["gdl:iRoadType"]).data() == "公道 (1)"
    assert m.index(1, col["elem:libPart"]).data() == "敷地オブジェクト"
    # GDL パラメータは閲覧のみ（敷地マネージャーのデータ整合性を守るため）
    assert not (m.flags(m.index(0, col["gdl:strRoadName"])) & Qt.ItemFlag.ItemIsEditable)
    changed, skipped = w._apply_source_edits([(0, col["gdl:strRoadName"], "変更")], "テスト")
    assert changed == 0 and "編集不可" in skipped[0]


# ── 敷地情報ダイアログ ─────────────────────────────────────────────

def _site_payload():
    from tests.test_site_summary import payload
    return payload()


def test_site_summary_button_and_dialog(main_window, monkeypatch, tmp_path):
    w = main_window
    w.btn_site_summary.click()
    assert w.sent.commands() == ["get_site_summary"]
    assert w.condition_status_label.text() == "敷地情報を取得中..."

    w.signals.site_summary_received.emit(_site_payload())
    dlg = w._site_dlg
    assert dlg.isVisible() and w.condition_status_label.text() == "敷地情報 取得完了"
    assert dlg.tbl_check.item(0, 3).text() == "適合"
    assert dlg.tbl_districts.item(0, 0).text() == "第二種住居地域"
    assert dlg.tbl_totals.item(0, 1).text() == "2,352.00㎡"
    assert dlg.tbl_bounds.rowCount() == 4 and dlg.tbl_roads.item(0, 0).text() == "北側道路"
    assert "敷地オブジェクト 1 件" in dlg.info_label.text()

    dlg.btn_refresh.click()
    assert w.sent.commands()[-1] == "get_site_summary"
    w.on_site_summary(_site_payload())
    assert w._site_dlg is dlg                                          # 2 回目は同じダイアログ

    path = tmp_path / "site.xlsx"
    monkeypatch.setattr(server.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(path), "")))
    dlg.btn_export.click()
    assert path.exists() and "Excel出力" in dlg.info_label.text()


def test_site_summary_without_site_and_missing_totals(main_window):
    w = main_window
    w.on_site_summary({"elements": [], "projectInfo": []})
    assert w.condition_status_label.text() == "敷地マネージャーの敷地オブジェクトが見つかりません"
    p = _site_payload(); p["projectInfo"] = []
    w.on_site_summary(p)
    assert "建築面積集計" in w._site_dlg.info_label.text()


def test_site_summary_not_compliant_is_red(main_window):
    p = _site_payload()
    p["projectInfo"] = [dict(x) for x in p["projectInfo"]]
    p["projectInfo"][1]["value"] = "1500"
    main_window.on_site_summary(p)
    item = main_window._site_dlg.tbl_check.item(0, 3)
    assert item.text() == "不適合" and item.foreground().color().name() == "#ff0000"


def test_site_summary_errors(main_window, monkeypatch, tmp_path):
    w = main_window
    monkeypatch.setattr(server.site_summary, "build_site_summary", lambda d: (_ for _ in ()).throw(KeyError("x")))
    w.on_site_summary({})
    assert w.msgbox.kinds() == ["warning"] and w._site_dlg is None
    monkeypatch.undo()
    w.msgbox.calls.clear()
    w.on_site_summary(_site_payload())
    dlg = w._site_dlg
    monkeypatch.setattr(server.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
    dlg.export_excel()                                                  # キャンセル
    monkeypatch.setattr(server.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(tmp_path / "x.xlsx"), "")))
    monkeypatch.setattr(server.table_tools, "export_sheets",
                        lambda *a: (_ for _ in ()).throw(PermissionError("使用中")))
    monkeypatch.setattr(server.QMessageBox, "warning", staticmethod(lambda *a: w.msgbox.calls.append(("warning",) + a[1:])))
    dlg.export_excel()
    assert w.msgbox.calls[-1][0] == "warning"
    dlg._summary = None
    dlg.export_excel()                                                  # 未取得なら何もしない


def test_site_summary_timeout_when_addon_does_not_answer(main_window, monkeypatch):
    """古いアドオン等で応答が無い場合、「取得中」で止まらず原因を案内する。"""
    w = main_window
    timers = []
    monkeypatch.setattr(server.QTimer, "singleShot", staticmethod(lambda ms, fn: timers.append((ms, fn))))
    w.request_site_summary()
    site_timers = [fn for ms, fn in timers if ms == 15000]   # ボタン用 30 秒タイマーとは別
    assert len(site_timers) == 1
    assert w.is_button_busy(w.btn_site_summary)
    site_timers[0]()                                  # 応答なしのまま時間切れ
    assert not w.is_button_busy(w.btn_site_summary)   # ボタンの色も戻る
    assert w.condition_status_label.text() == "敷地情報: ArchiCAD から応答がありません"
    kind, _, text = w.msgbox.calls[-1]
    assert kind == "warning" and "再起動" in text


def test_site_summary_timeout_ignored_after_response(main_window, monkeypatch):
    w = main_window
    timers = []
    monkeypatch.setattr(server.QTimer, "singleShot",
                        staticmethod(lambda ms, fn: timers.append(fn) if ms == 15000 else None))
    w.request_site_summary()
    w.on_site_summary(_site_payload())                # 時間内に応答
    timers[0]()
    assert w.condition_status_label.text() == "敷地情報 取得完了"
    w.request_site_summary(); w.request_site_summary()
    timers[1]()                                       # 古い要求の時間切れは無視
    assert w.msgbox.calls == []
    timers[2]()
    assert w.msgbox.kinds() == ["warning"]


# ── プロパティ設定: 簡易版（主）/ 詳細版（サブ）────────────────────

SIMPLE_DEFS = [
    {"guid": "builtin:element_id", "name": "ID", "group": "IDとカテゴリ", "editable": True},
    {"guid": "P-WIDTH", "name": "幅", "group": "形状", "editable": True, "isCustom": False},
    {"guid": "P-FLAG", "name": "変更フラグ", "group": "変更フラグ", "editable": True, "isCustom": True},
    {"guid": "P-ROAD", "name": "道路中心高さ", "group": "敷地", "editable": True, "isCustom": True},
    {"guid": "elem:type", "name": "要素タイプ", "group": "要素設定パラメータ", "editable": False},
    {"guid": "elem:layer", "name": "レイヤ名", "group": "要素設定パラメータ", "editable": False},
    {"guid": "elem:height", "name": "高さ", "group": "要素設定パラメータ", "editable": True},
    {"guid": "elem:W", "name": "幅 W (m)", "group": "基本情報", "editable": False, "simple": True},
    {"guid": "elem:L", "name": "長さ L (m)", "group": "基本情報", "editable": False, "simple": True},
    {"guid": "elem:H", "name": "高さ H (m)", "group": "基本情報", "editable": False, "simple": True},
    {"guid": "elem:elevation", "name": "レベル 底面 (m)", "group": "基本情報", "editable": False, "simple": True},
    {"guid": "elem:story", "name": "フロア", "group": "基本情報", "editable": False, "simple": True},
    {"guid": "elem:classification", "name": "分類", "group": "基本情報", "editable": False, "simple": True},
    {"guid": "gdl:A", "name": "寸法 1 (A)", "group": "GDLパラメータ", "editable": False},
]


def _tree(w):
    out = {}
    for i in range(w.prop_tree.topLevelItemCount()):
        g = w.prop_tree.topLevelItem(i)
        out[g.text(0)] = [(g.child(j).text(0), g.child(j).checkState(0) == Qt.CheckState.Checked)
                          for j in range(g.childCount())]
    return out


def test_simple_mode_is_default_with_basic_info_checked(main_window):
    w = main_window
    assert w.prop_mode_combo.currentText() == server.PROP_MODE_SIMPLE
    w.current_elements = [{"guid": G1, "type": "Wall"}]
    w.on_definitions({"definitions": SIMPLE_DEFS})
    tree = _tree(w)
    # 基本情報は決まった順で並び、最初からチェック済み → すぐ［データ取得］できる
    assert tree[server.SIMPLE_GROUP] == [
        ("ID", True), ("要素タイプ", True), ("レイヤ名", True), ("幅 W (m)", True), ("長さ L (m)", True),
        ("高さ H (m)", True), ("レベル 底面 (m)", True), ("フロア", True), ("分類", True)]
    assert w.prop_tree.topLevelItem(0).checkState(0) == Qt.CheckState.Checked
    # プロジェクトで作成したプロパティ（isCustom）は簡易版にも出るが未チェック
    assert tree["変更フラグ"] == [("変更フラグ", False)] and tree["敷地"] == [("道路中心高さ", False)]
    # Archicad 標準プロパティ・GDL・個別パラメータは詳細版のみ
    assert "形状" not in tree and "GDLパラメータ" not in tree and "要素設定パラメータ" not in tree

    w.get_values()
    req = w.sent.last("get_values")
    assert req["propGuids"] == [
        "builtin:element_id", "elem:type", "elem:layer", "elem:W", "elem:L", "elem:H",
        "elem:elevation", "elem:story", "elem:classification"]   # 列はこの順


def test_mode_switch_keeps_checked_properties(main_window):
    w = main_window
    w.current_elements = [{"guid": G1, "type": "Wall"}]
    w.on_definitions({"definitions": SIMPLE_DEFS})
    flag = next(w.prop_tree.topLevelItem(i) for i in range(w.prop_tree.topLevelItemCount())
                if w.prop_tree.topLevelItem(i).text(0) == "変更フラグ")
    flag.child(0).setCheckState(0, Qt.CheckState.Checked)

    w.prop_mode_combo.setCurrentText(server.PROP_MODE_DETAIL)
    tree = _tree(w)
    assert ("幅", False) in tree["形状"] and tree["GDLパラメータ"] == [("寸法 1 (A)", False)]
    assert tree["変更フラグ"] == [("変更フラグ", True)]                       # チェックを引き継ぐ
    assert ("幅 W (m)", True) in tree["基本情報"] and ("ID", True) in tree["IDとカテゴリ"]
    shape = next(w.prop_tree.topLevelItem(i) for i in range(w.prop_tree.topLevelItemCount())
                 if w.prop_tree.topLevelItem(i).text(0) == "形状")
    shape.setCheckState(0, Qt.CheckState.Checked)

    w.prop_mode_combo.setCurrentText(server.PROP_MODE_SIMPLE)
    tree = _tree(w)
    assert tree["変更フラグ"] == [("変更フラグ", True)] and "形状" not in tree
    w.get_values()
    # 簡易版に表示されていない詳細版のチェック（幅）は送らない
    assert "P-WIDTH" not in w.sent.last("get_values")["propGuids"]


def test_simple_mode_values_display(main_window):
    w = main_window
    w.current_elements = [{"guid": G1, "type": "Wall"}]
    w.on_definitions({"definitions": SIMPLE_DEFS})
    w.get_values()
    w.on_values({"values": [{"guid": G1, "modiStamp": 1, "props": {
        "builtin:element_id": "W-01", "elem:type": "壁", "elem:layer": "躯体", "elem:W": "0.200",
        "elem:L": "5.400", "elem:H": "3.000", "elem:elevation": "0.000", "elem:story": "1 Floor (1FL)",
        "elem:classification": "Archicad分類: 壁 壁"}}]})
    m = w.model
    assert [m.headerData(c, Qt.Orientation.Horizontal) for c in range(m.columnCount())] == [
        "ID", "ID", "要素タイプ", "レイヤ名", "＝幅 W (m)", "＝長さ L (m)", "＝高さ H (m)", "＝レベル 底面 (m)",
        "フロア", "分類"]
    assert m.index(0, 4).data() == "0.200" and m.index(0, 9).data() == "Archicad分類: 壁 壁"
    # 基本情報は閲覧のみ
    assert not (m.flags(m.index(0, 4)) & Qt.ItemFlag.ItemIsEditable)


# ── 階ごとの件数（梁は支える床の階に属する）──────────────────────

CONFIG = {
    "stories": [{"index": 0, "name": "1 Floor (1FL)"}, {"index": 1, "name": "2 Floor (2FL)"},
                {"index": 2, "name": "3 Floor (3FL)"}],
    "elementTypes": {"Beam": 346, "Column": 200},
    "elementTypeStories": {"Beam": {"0": 42, "1": 160, "2": 144}, "Column": {"0": 31, "1": 169}},
}


def _types(w):
    root = w.filter_tree.topLevelItem(1)
    return [root.child(i).text(0) for i in range(root.childCount())]


def test_type_counts_follow_selected_stories(main_window):
    w = main_window
    w.on_config(CONFIG)
    assert _types(w) == ["Beam (346)", "Column (200)"]              # 階未選択: 全件数
    stories = w.filter_tree.topLevelItem(0)
    stories.child(0).setCheckState(0, Qt.CheckState.Checked)        # 1FL のみ
    assert _types(w) == ["Beam (42 / 全346)", "Column (31 / 全200)"]
    stories.child(1).setCheckState(0, Qt.CheckState.Checked)        # +2FL（大梁はこちら）
    assert _types(w) == ["Beam (202 / 全346)", "Column (200 / 全200)"]
    stories.child(0).setCheckState(0, Qt.CheckState.Unchecked)
    stories.child(1).setCheckState(0, Qt.CheckState.Unchecked)
    assert _types(w) == ["Beam (346)", "Column (200)"]


def test_story_root_toggles_all_floors(main_window):
    w = main_window
    w.on_config(CONFIG)
    root = w.filter_tree.topLevelItem(0)
    root.setCheckState(0, Qt.CheckState.Checked)                    # 「階層」で全階選択
    assert all(root.child(i).checkState(0) == Qt.CheckState.Checked for i in range(3))
    assert _types(w)[0] == "Beam (346 / 全346)"
    w.filter_tree.topLevelItem(1).child(0).setCheckState(0, Qt.CheckState.Checked)
    w.search_elements()
    cmd = w.sent.last("get_elements")
    assert cmd["stories"] == [0, 1, 2] and cmd["Beam"] is True
    root.child(2).setCheckState(0, Qt.CheckState.Unchecked)         # 1 つ外すと「階層」も外れる
    assert root.checkState(0) == Qt.CheckState.Unchecked
    root.child(2).setCheckState(0, Qt.CheckState.Checked)           # 全部付くと「階層」も付く
    assert root.checkState(0) == Qt.CheckState.Checked
    root.setCheckState(0, Qt.CheckState.Unchecked)
    assert all(root.child(i).checkState(0) == Qt.CheckState.Unchecked for i in range(3))


def test_counts_without_per_story_data_and_before_config(main_window):
    w = main_window
    w.on_filter_item_changed(None, 0)                               # 構成受信前は何もしない
    w.on_config({"stories": [{"index": 0, "name": "1FL"}], "elementTypes": {"Wall": 5}})
    w.filter_tree.topLevelItem(0).child(0).setCheckState(0, Qt.CheckState.Checked)
    assert _types(w) == ["Wall (0 / 全5)"]                         # 古いアドオン（階別件数なし）


def test_elements_capped_notice(main_window):
    w = main_window
    w.on_elements({"capped": True, "elements": [{"guid": G1, "type": "Object"}]})
    assert "上限1000件で打ち切り" in w.condition_status_label.text()
    w.on_elements({"capped": False, "elements": [{"guid": G1, "type": "Object"}]})
    assert "上限" not in w.condition_status_label.text()


# ── ウィンドウの大きさ・位置の保存と復元 ───────────────────────────
# ※ offscreen の仮想画面は 800x600。それより大きいサイズは復元時に画面内へ収められる

def _new_window(monkeypatch, tmp_path):
    import db_manager
    monkeypatch.setattr(db_manager, "DB_PATH", tmp_path / "w2.db")
    w = server.MainWindow()
    w.sent = []
    return w


def test_window_size_is_restored_next_time(main_window, monkeypatch, tmp_path):
    w = main_window
    w.show()
    w.resize(640, 420)
    w.move(40, 60)
    w.data_panel.show()
    w.data_panel.resize(770, 480)
    w.h_split.setSizes([400, 200])
    w.close()                                           # 閉じる時に保存

    w2 = _new_window(monkeypatch, tmp_path)
    try:
        assert (w2.width(), w2.height()) == (640, 420)
        assert (w2.data_panel.width(), w2.data_panel.height()) == (770, 480)
        a, b = w2.h_split.sizes()
        assert a > b                                    # 左右分割の比率も復元
    finally:
        w2.close()


def test_unshown_window_keeps_previous_size(main_window, monkeypatch, tmp_path):
    w = main_window
    w.show()
    w.data_panel.show()
    w.data_panel.resize(761, 466)
    w.save_window_state()
    w.data_panel.hide()

    w2 = _new_window(monkeypatch, tmp_path)            # 今回はデータパネルを開かずに閉じる
    try:
        w2.show()
    finally:
        w2.close()
    w3 = _new_window(monkeypatch, tmp_path)
    try:
        assert (w3.data_panel.width(), w3.data_panel.height()) == (761, 466)
    finally:
        w3.close()


def test_resize_triggers_debounced_autosave(main_window):
    w = main_window
    started = []
    w._state_timer.timeout.disconnect()
    w._state_timer.timeout.connect(lambda: started.append(True))
    w.resize(700, 400)                                  # 未表示ではまだ保存対象外
    assert not w._state_timer.isActive()
    w.show()
    w.resize(800, 450)
    assert w._state_timer.isActive() and w._state_timer.interval() == 500
    w.h_split.splitterMoved.emit(100, 1)
    assert w._state_timer.isActive()


def test_dialog_sizes_are_restored(main_window, monkeypatch, tmp_path):
    w = main_window
    w.on_site_summary({"elements": [], "projectInfo": []})
    w._site_dlg.resize(620, 500)
    dlg = w.show_replace_dialog()
    dlg.resize(500, 260)
    w.save_window_state()

    w2 = _new_window(monkeypatch, tmp_path)
    try:
        w2.on_site_summary({"elements": [], "projectInfo": []})
        assert (w2._site_dlg.width(), w2._site_dlg.height()) == (620, 500)
        assert (w2.show_replace_dialog().width(), w2._replace_dlg.height()) == (500, 260)
    finally:
        w2.close()


def test_close_survives_settings_error(main_window, monkeypatch):
    monkeypatch.setattr(main_window, "save_window_state", lambda: (_ for _ in ()).throw(OSError("read-only")))
    main_window.close()                                 # 保存に失敗しても終了処理は続く
    assert main_window.db._local.conn is None

def test_close_stops_tracking(main_window):
    w = main_window
    w.show_replace_dialog()
    w.close()
    assert w._tracked_windows == {} and not w._state_timer.isActive()
    w.data_panel.resize(300, 200)                       # 閉じた後の変更は保存処理を起動しない
    assert not w._state_timer.isActive()

# ── ボタンの処理中表示（取得中は少し濃く、完了で戻す）──────────────

@pytest.fixture
def no_timers(monkeypatch):
    """QTimer.singleShot を記録だけにする（時間経過を手動で起こす）。"""
    calls = []
    monkeypatch.setattr(server.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append((ms, fn))))
    return calls


def test_busy_button_is_darker_and_restored(main_window, no_timers):
    w = main_window
    btn = w.btn_get_values
    btn.setStyleSheet("color: blue;")
    base = btn.palette().color(server.QPalette.ColorRole.Button)   # 濃くする前の色
    w.set_button_busy(btn)
    assert w.is_button_busy(btn) and "background-color" in btn.styleSheet()
    assert base.darker(120).name() in btn.styleSheet()        # 少し濃い色
    w.set_button_busy(btn)                                     # 二重に濃くしない
    assert len(w._busy_buttons) == 1
    w.clear_button_busy(btn)
    assert not w.is_button_busy(btn) and btn.styleSheet() == "color: blue;"   # 元のスタイルに戻る


def test_busy_auto_restores_after_timeout(main_window, no_timers):
    w = main_window
    w.set_button_busy(w.btn_sync)
    ms, fn = no_timers[-1]
    assert ms == w.BUSY_TIMEOUT_MS
    fn()                                                       # 応答が無くても 30 秒で戻る
    assert not w.is_button_busy(w.btn_sync)
    w.set_button_busy(w.btn_sync)
    fn()                                                       # 古いタイマーは新しい処理中を戻さない
    assert w.is_button_busy(w.btn_sync)


def test_flash_for_immediate_buttons(main_window, no_timers):
    w = main_window
    w.btn_clear_sort.click()                                   # その場で終わる操作
    assert w.is_button_busy(w.btn_clear_sort)
    flash = [fn for ms, fn in no_timers if ms == w.FLASH_MS]
    flash[-1]()
    assert not w.is_button_busy(w.btn_clear_sort)


def test_search_and_values_buttons_follow_responses(main_window, no_timers):
    w = main_window
    w.btn_search.click()
    assert w.is_button_busy(w.btn_search)                      # 抽出中
    w.on_elements({"elements": [{"guid": G1, "type": "Wall"}]})
    assert w.is_button_busy(w.btn_search)                      # プロパティ一覧の取得中も濃いまま
    w.on_definitions({"definitions": [{"guid": "builtin:element_id", "name": "ID", "group": "IDとカテゴリ"}]})
    assert not w.is_button_busy(w.btn_search)                  # 完了で戻る

    w.btn_search.click()
    w.on_elements({"elements": []})                            # 該当なしでも戻る
    assert not w.is_button_busy(w.btn_search)

    w.btn_get_values.click()
    assert w.is_button_busy(w.btn_get_values)
    w.on_values({"values": []})
    assert not w.is_button_busy(w.btn_get_values)


def test_get_values_without_selection_only_flashes(main_window, no_timers):
    w = main_window
    w.btn_get_values.click()                                   # 何も選んでいない → 送信しない
    assert w.sent == []
    assert [ms for ms, _ in no_timers if ms == w.FLASH_MS]     # 押した反応だけ見せる


def test_sync_status_override_site_buttons(loaded, no_timers, monkeypatch):
    w = loaded
    _edit(w, G1, "P-WIDTH", "150")
    w.sync_data()
    assert w.is_button_busy(w.btn_sync)
    w.on_values(_values_payload(stamps=(11, 20, 30)))         # 競合なし → 反映送信、まだ濃い
    assert w.is_button_busy(w.btn_sync)
    w.on_sync_complete({"results": [{"guid": G1, "status": "success"}]})
    assert not w.is_button_busy(w.btn_sync)

    w._selected_guids = [G1]
    w.mark_confirmed()
    assert w.is_button_busy(w.btn_confirm)
    w.on_change_status_result({"status": "ok", "value": 2, "set": 1})
    assert not w.is_button_busy(w.btn_confirm)

    w.btn_bim_override.setChecked(True)
    w.toggle_bim_override()
    assert w.is_button_busy(w.btn_bim_override)
    w.on_bim_override_result({"action": "applied", "status": "ok"})
    assert not w.is_button_busy(w.btn_bim_override)

    w.request_site_summary()
    assert w.is_button_busy(w.btn_site_summary)
    w.on_site_summary({"elements": [], "projectInfo": []})
    assert not w.is_button_busy(w.btn_site_summary)


def test_sync_cancel_and_error_restore(loaded, no_timers, monkeypatch):
    _ConflictBoxDriver(monkeypatch, "全て中止")
    w = loaded
    _edit(w, G1, "P-WIDTH", "150")
    w.sync_data()
    w.on_values(_values_payload(width=("111", "200", "300")))  # 競合 → 中止
    assert not w.is_button_busy(w.btn_sync)

    w.set_button_busy(w.btn_get_values); w.set_button_busy(w.btn_search)
    w.on_error("接続できません")                               # エラー時はすべて戻す
    assert w._busy_buttons == {}

def test_condition_window_groups(main_window):
    """① 条件を指定して取得（要素抽出・プロパティ設定・データ取得）と ② 別の方法で取得 を分けて配置。"""
    w = main_window
    g1, g2 = w.grp_condition, w.grp_other
    assert g1.title().startswith("① 条件を指定して取得") and g2.title().startswith("② 別の方法で取得")
    for child in (w.filter_tree, w.btn_search, w.prop_tree, w.prop_mode_combo, w.btn_get_values):
        assert g1.isAncestorOf(child) and not g2.isAncestorOf(child)
    for child in (w.btn_site_summary, w.btn_schedules):
        assert g2.isAncestorOf(child) and not g1.isAncestorOf(child)
    lay = w.centralWidget().layout()
    order = [lay.itemAt(i).widget() for i in range(lay.count())]
    assert order.index(g1) < order.index(g2) < order.index(w.condition_status_label)   # ① → ② → 状態表示