"""データ表示・編集ウィンドウの複数表示（SQLite で管理・閉じると消去・同じものは開かない）。"""
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMessageBox

import db_manager
import server

G1, G2, G3 = "AAAAAAAA-1", "BBBBBBBB-2", "CCCCCCCC-3"
CONFIG = {"stories": [{"index": 0, "name": "1FL"}, {"index": 1, "name": "2FL"}],
          "elementTypes": {"Beam": 3, "Column": 2},
          "elementTypeStories": {"Beam": {"0": 1, "1": 2}, "Column": {"0": 2}}}
DEFS = [
    {"guid": "builtin:element_id", "name": "ID", "group": "IDとカテゴリ", "editable": True},
    {"guid": "elem:W", "name": "幅 W (m)", "group": "基本情報", "editable": False},
    {"guid": "P-NOTE", "name": "備考", "group": "メモ", "editable": True, "isCustom": True},
]


# ── SQLite ───────────────────────────────────────────────────────────

def test_window_key_ignores_order_and_unchecked_types():
    k1 = db_manager.DbManager.make_window_key([1, 0], {"Beam": True, "Wall": False}, ["b", "a"])
    k2 = db_manager.DbManager.make_window_key([0, 1], {"Beam": True}, ["a", "b"])
    k3 = db_manager.DbManager.make_window_key([0], {"Beam": True}, ["a", "b"])
    assert k1 == k2 and k1 != k3


def test_db_window_registry(db):
    assert db.open_window("W1", "K1", "t1", 5, [0], {"Beam": True}, ["G"], ["P"]) is True
    assert db.open_window("W2", "K1", "dup", None, [], {}, [], []) is False   # 同じキーは登録しない
    assert db.find_window("K1")["window_id"] == "W1"
    assert db.find_window("none") is None
    db.open_window("W3", "K3", "t3", None, [], {}, [], [])
    assert [w["window_id"] for w in db.list_windows()] == ["W1", "W3"]
    db.close_window("W1")
    assert db.find_window("K1") is None
    db.clear_windows()
    assert db.list_windows() == []


# ── 画面 ─────────────────────────────────────────────────────────────

def _prepare(w, stories=(0,), types=("Beam",)):
    w.on_config(CONFIG)
    root = w.filter_tree.topLevelItem(0)
    for i in range(root.childCount()):
        root.child(i).setCheckState(0, Qt.CheckState.Checked if i in stories else Qt.CheckState.Unchecked)
    t = w.filter_tree.topLevelItem(1)
    for i in range(t.childCount()):
        key = t.child(i).data(0, Qt.ItemDataRole.UserRole)
        t.child(i).setCheckState(0, Qt.CheckState.Checked if key in types else Qt.CheckState.Unchecked)
    w.search_elements()
    w.on_elements({"elements": [{"guid": G1, "type": "Beam"}, {"guid": G2, "type": "Beam"}]})
    w.on_definitions({"definitions": DEFS})


def _check_prop(w, guid, on=True):
    it = server.QTreeWidgetItemIterator(w.prop_tree)
    while it.value():
        d = it.value().data(0, Qt.ItemDataRole.UserRole)
        if d and d.get("guid") == guid:
            it.value().setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        it += 1


def _values(req, width="0.300"):
    return {"type": "property_values", "reqId": req["reqId"], "values": [
        {"guid": g, "modiStamp": 1, "props": {"builtin:element_id": f"B-{i}", "elem:W": width, "P-NOTE": ""}}
        for i, g in enumerate(req["guids"])]}


def _fetch(w):
    w.get_values()
    req = w.sent.last("get_values")
    w.on_values(_values(req))
    return req


def test_first_fetch_reuses_initial_window_and_registers(main_window):
    w = main_window
    initial = w.data_panel
    _prepare(w)
    req = _fetch(w)
    assert req["reqId"] == initial.window_id                      # 最初は既存の 1 枚を使う
    assert w.open_panels() == [initial] and initial.isVisible()
    row = w.db.find_window(initial.window_key)
    assert row["window_id"] == initial.window_id
    assert "#1" in initial.windowTitle() and "1FL / Beam" in initial.windowTitle()
    assert "(2件 × 2列)" in initial.windowTitle()


def test_different_condition_opens_second_window(main_window):
    w = main_window
    _prepare(w)
    r1 = _fetch(w)
    _check_prop(w, "P-NOTE")                                        # プロパティを変える
    r2 = _fetch(w)
    assert r1["reqId"] != r2["reqId"]
    panels = w.open_panels()
    assert len(panels) == 2 and all(p.isVisible() for p in panels)
    assert len(w.db.list_windows()) == 2
    assert panels[1].model.columnCount() == panels[0].model.columnCount() + 1
    assert "#2" in panels[1].windowTitle()


def test_same_condition_is_not_opened_twice(main_window):
    w = main_window
    _prepare(w)
    _fetch(w)
    before = len(w.sent)
    w.get_values()                                                  # 同じ条件でもう一度
    assert len(w.sent) == before                                    # 取得要求を送らない
    kind, title, text = w.msgbox.calls[-1]
    assert kind == "information" and "既に開いています" in text
    assert len(w.open_panels()) == 1 and len(w.db.list_windows()) == 1


def test_values_route_by_request_id(main_window):
    w = main_window
    _prepare(w)
    w.get_values(); r1 = w.sent.last("get_values")
    _check_prop(w, "P-NOTE")
    w.get_values(); r2 = w.sent.last("get_values")
    p1, p2 = w._panels[r1["reqId"]], w._panels[r2["reqId"]]
    w.on_values(_values(r2, width="2.000"))                         # 逆順に届いても
    w.on_values(_values(r1, width="1.000"))
    assert p1.model._values[(G1, "elem:W")] == "1.000"
    assert p2.model._values[(G1, "elem:W")] == "2.000"
    assert w._value_requests == {}


def test_values_without_request_id_follow_request_order(main_window):
    w = main_window
    _prepare(w)
    w.get_values(); r1 = w.sent.last("get_values")
    _check_prop(w, "P-NOTE")
    w.get_values(); r2 = w.sent.last("get_values")
    old = {k: v for k, v in _values(r1, "9").items() if k != "reqId"}  # 古いアドオン（reqId 無し）
    w.on_values(old)
    assert w._panels[r1["reqId"]].model._values[(G1, "elem:W")] == "9"


def test_closing_window_deletes_registration(main_window):
    w = main_window
    _prepare(w)
    _fetch(w)
    _check_prop(w, "P-NOTE")
    _fetch(w)
    first, second = w.open_panels()
    first.close()
    assert first.window_id not in w._panels
    assert [r["window_id"] for r in w.db.list_windows()] == [second.window_id]
    assert w.data_panel is second
    _check_prop(w, "P-NOTE", on=False)
    _fetch(w)                                                       # 閉じた条件はまた開ける
    assert len(w.open_panels()) == 2


def test_close_with_unsynced_changes_asks(main_window):
    w = main_window
    _prepare(w)
    _fetch(w)
    p = w.data_panel
    assert p.model.setData(p.model.index(0, 1), "NEW-ID")          # 未反映の変更
    w.msgbox.question_answer = QMessageBox.StandardButton.No
    p.close()
    assert p.window_id in w._panels and w.db.find_window(p.window_key)   # 閉じない
    w.msgbox.question_answer = QMessageBox.StandardButton.Yes
    p.close()
    assert p.window_id not in w._panels and w.db.list_windows() == []


def test_closing_last_window_keeps_spare_panel(main_window):
    w = main_window
    _prepare(w)
    _fetch(w)
    w.data_panel.close()
    spare = w.data_panel
    assert spare is not None and spare.window_key is None           # self.model 等が引き続き使える
    assert w.model.rowCount() == 0


def test_buttons_act_on_their_own_window(main_window):
    w = main_window
    _prepare(w)
    _fetch(w)
    _check_prop(w, "P-NOTE")
    _fetch(w)
    first, second = w.open_panels()
    first.model.setData(first.model.index(0, 1), "X-1")
    assert w.data_panel is second
    first.btn_sync.click()                                          # 1 枚目のボタン → 1 枚目が対象
    assert w.data_panel is first
    verify = w.sent.last("get_values")
    assert verify["reqId"] == first.window_id and verify["guids"] == [G1]
    w.on_values(_values({"reqId": verify["reqId"], "guids": [G1]}))  # 競合なし → 反映
    assert w.sent.last("apply_changes")["changes"][0]["value"] == "X-1"
    w.activate_panel(second)                                        # 別ウィンドウを触っていても
    w.on_sync_complete({"results": [{"guid": G1, "status": "success"}]})
    assert first.model._original_values[(G1, "builtin:element_id")] == "X-1"   # 結果は 1 枚目へ


def test_undo_history_is_per_window(main_window):
    w = main_window
    _prepare(w)
    _fetch(w)
    _check_prop(w, "P-NOTE")
    _fetch(w)
    first, second = w.open_panels()
    first.model.setData(first.model.index(0, 1), "A")
    assert first.undo_stack.count() == 1 and second.undo_stack.count() == 0
    first.btn_undo.click()
    assert first.model._values[(G1, "builtin:element_id")] == "B-0"


def test_selection_sync_targets_window_with_element(main_window):
    w = main_window
    _prepare(w)
    _fetch(w)
    first = w.data_panel
    _prepare(w, stories=(1,))
    w.on_elements({"elements": [{"guid": G3, "type": "Beam"}]})
    w.on_definitions({"definitions": DEFS})
    _fetch(w)
    second = w.data_panel
    assert second is not first
    w.on_archicad_selection_changed({"guid": G1})                   # 1 枚目にだけある要素
    assert w.data_panel is first and first.model._selected_guid == G1


def test_error_discards_window_waiting_for_first_values(main_window):
    w = main_window
    _prepare(w)
    w.get_values()
    assert len(w.db.list_windows()) == 1
    w.on_error("接続できません")
    assert w.db.list_windows() == [] and w._value_requests == {}
    w.msgbox.calls.clear()
    _fetch(w)                                                       # 同じ条件で取り直せる
    assert len(w.open_panels()) == 1


def test_main_close_clears_all_windows(main_window):
    w = main_window
    _prepare(w)
    _fetch(w)
    _check_prop(w, "P-NOTE")
    _fetch(w)
    w.data_panel.model.setData(w.data_panel.model.index(0, 1), "Z")  # 終了時は確認しない
    w.close()
    assert w._panels == {}
    reopened = db_manager.DbManager(db_manager.DB_PATH)
    try:
        assert reopened.list_windows() == []
    finally:
        reopened.close()


def test_startup_clears_stale_registrations(qapp, tmp_path, monkeypatch, msgbox):
    path = tmp_path / "stale.db"
    pre = db_manager.DbManager(path)
    pre.open_window("OLD", "K", "前回の残り", None, [], {}, [], [])
    pre.close()
    monkeypatch.setattr(db_manager, "DB_PATH", path)
    monkeypatch.setattr(server, "SETTINGS_PATH", tmp_path / "w.ini")
    monkeypatch.setattr(server.MainWindow, "_start_floor_polling", lambda self: None)
    w = server.MainWindow()
    try:
        assert w.db.list_windows() == []
    finally:
        w.close()


def test_replace_dialog_targets_its_window(main_window):
    w = main_window
    _prepare(w)
    _fetch(w)
    first = w.data_panel
    dlg = w.show_replace_dialog()
    _check_prop(w, "P-NOTE")
    _fetch(w)
    assert w.data_panel is not first
    dlg.find_edit.setText("B-"); dlg.repl_edit.setText("BEAM-")
    second = w.data_panel
    dlg.btn_replace.click()
    assert first.model._values[(G1, "builtin:element_id")] == "BEAM-0"
    assert second.model._values[(G1, "builtin:element_id")] == "B-0"   # 他のウィンドウは変わらない
