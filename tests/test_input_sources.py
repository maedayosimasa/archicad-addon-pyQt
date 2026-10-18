"""入力元（elem:in:*）の編集と、反映後の自動計算の取り直しのテスト。"""
import pytest
from PyQt6.QtCore import Qt

import server
import table_tools

NA = table_tools.NOT_APPLICABLE
GW, GS = "WALL-0001", "SLAB-0001"
ELEMS = [{"guid": GW, "type": "Wall"}, {"guid": GS, "type": "Slab"}]
PROPS = [
    {"guid": "elem:H", "name": "高さ H (m)", "group": "基本情報", "editable": False, "valueType": 2},
    {"guid": "elem:in:height", "name": "入力 高さ", "group": "基本情報", "editable": True,
     "valueType": 2, "input": True, "unit": "m"},
    {"guid": "elem:in:length", "name": "入力 長さ", "group": "基本情報", "editable": True,
     "valueType": 2, "input": True, "unit": "m"},
    {"guid": "elem:in:topLink", "name": "入力 上部リンク", "group": "基本情報", "editable": True,
     "valueType": 1, "input": True, "unit": "story"},
]


def _payload(h_wall="3.000", h_slab="0.200", reqid=None):
    d = {"type": "property_values", "values": [
        {"guid": GW, "modiStamp": 10, "props": {"elem:H": h_wall, "elem:in:height": h_wall,
                                                 "elem:in:length": "5.000", "elem:in:topLink": "0"}},
        {"guid": GS, "modiStamp": 20, "props": {"elem:H": h_slab, "elem:in:height": h_slab,
                                                 "elem:in:length": NA, "elem:in:topLink": NA}},
    ]}
    if reqid:
        d["reqId"] = reqid
    return d


@pytest.fixture
def loaded(main_window):
    w = main_window
    w.current_condition_id = w.db.save_condition([0], {"Wall": True, "Slab": True})
    w.current_elements = list(ELEMS)
    w.current_props = list(PROPS)
    w.on_values(_payload())
    w.sent.clear()
    return w


def _cell(w, guid, pg):
    row = next(i for i, e in enumerate(w.model._elements) if e["guid"] == guid)
    col = next(i for i, p in enumerate(w.model._properties) if p["guid"] == pg) + 1
    return row, col


# ── table_tools ────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("0.6", "0.600"), ("600mm", "0.600"), ("60 cm", "0.600"), ("0.6m", "0.600"),
    ("1,200mm", "1.200"), ("-0.15", "-0.150"), (".5", "0.500"), (3, "3.000"), ("2.5M", "2.500"),
])
def test_normalize_length(text, expected):
    assert table_tools.normalize_value({"unit": "m"}, text) == expected


@pytest.mark.parametrize("text, expected", [("0", "0"), ("2", "2"), ("1.0", "1")])
def test_normalize_story(text, expected):
    assert table_tools.normalize_value({"unit": "story"}, text) == expected


@pytest.mark.parametrize("unit, text", [("m", "abc"), ("m", "6ft"), ("m", ""),
                                        ("story", "x"), ("story", "-1"), ("story", "1.5")])
def test_normalize_invalid(unit, text):
    with pytest.raises(ValueError):
        table_tools.normalize_value({"unit": unit}, text)


def test_normalize_other_columns_unchanged():
    assert table_tools.normalize_value({}, " abc ") == " abc "
    assert table_tools.normalize_value({}, None) == ""


def test_validate_not_applicable_and_unit():
    p = {"name": "入力 長さ", "unit": "m"}
    assert table_tools.validate_value(p, "1", NA) == (False, "入力 長さ: この要素タイプには無い項目（対象外）")
    ok, reason = table_tools.validate_value(p, "abc", "5.000")
    assert not ok and "長さとして解釈できません" in reason
    assert table_tools.validate_value(p, "600mm", "5.000") == (True, "")
    assert table_tools.validate_value({"name": "x"}, "anything") == (True, "")


# ── 定義・方針 ────────────────────────────────────────────────────

def test_input_keys_follow_computed_simple_keys():
    keys = server.SIMPLE_KEYS
    assert keys.index("elem:in:width") > keys.index("elem:classification")
    assert all(k in keys for k in server.INPUT_KEYS)


def test_elem_keys_use_strict_policy():
    assert server.get_group_type({"group": "基本情報"}, "elem:in:height") == "parameter"
    assert server.get_group_type({"group": "要素設定パラメータ"}, "elem:thickness") == "parameter"


# ── 表の編集 ──────────────────────────────────────────────────────

def test_not_applicable_cell_is_read_only_and_gray(loaded):
    m = loaded.model
    r, c = _cell(loaded, GS, "elem:in:length")
    assert not (m.flags(m.index(r, c)) & Qt.ItemFlag.ItemIsEditable)
    assert m.data(m.index(r, c), Qt.ItemDataRole.ForegroundRole).name() == "#808080"
    assert not m.setData(m.index(r, c), "3")
    r, c = _cell(loaded, GW, "elem:in:length")
    assert m.flags(m.index(r, c)) & Qt.ItemFlag.ItemIsEditable
    r, c = _cell(loaded, GW, "elem:H")                      # 自動計算は閲覧のみのまま
    assert not (m.flags(m.index(r, c)) & Qt.ItemFlag.ItemIsEditable)


def test_setdata_normalizes_units(loaded):
    m = loaded.model
    r, c = _cell(loaded, GW, "elem:in:height")
    assert m.setData(m.index(r, c), "2800mm")
    assert m._values[(GW, "elem:in:height")] == "2.800"
    assert not m.setData(m.index(r, c), "2.8")             # 同じ値（0.001 m 単位）→ 変更なし
    assert not m.setData(m.index(r, c), "高い")            # 解釈できない値は拒否
    assert m._values[(GW, "elem:in:height")] == "2.800"


def test_paste_skips_not_applicable_and_invalid(loaded):
    w = loaded
    rw, c = _cell(w, GW, "elem:in:length")
    rs, _ = _cell(w, GS, "elem:in:length")
    n, skipped = w._apply_source_edits([(rw, c, "6000mm"), (rs, c, "6")], "貼り付け", warn=False)
    assert n == 1 and len(skipped) == 1 and "対象外" in skipped[0]
    assert w.model._values[(GW, "elem:in:length")] == "6.000"
    n, skipped = w._apply_source_edits([(rw, c, "長い")], "貼り付け", warn=False)
    assert n == 0 and "解釈できません" in skipped[0]


def test_sync_sends_input_change_as_parameter(loaded):
    w = loaded
    r, c = _cell(w, GW, "elem:in:topLink")
    assert w.model.setData(w.model.index(r, c), "1")
    w.sync_data()
    ch = next(c for c in w._pending_changes if c["propId"] == "elem:in:topLink")
    assert ch["group"] == "parameter" and ch["value"] == "1" and ch["modiStamp"] == 10
    assert "AbortAll (厳密): 1件" in w.msgbox.calls[-1][2]


# ── 反映後の取り直し ──────────────────────────────────────────────

def test_sync_success_refreshes_computed_values(loaded):
    w = loaded
    r, c = _cell(w, GW, "elem:in:height")
    assert w.model.setData(w.model.index(r, c), "2.5")
    w.on_sync_complete({"results": [{"guid": GW, "status": "success", "currentStamp": 11}]})
    req = w.sent.last("get_values")
    assert req["guids"] == [GW] and req["reqId"].startswith("refresh:")
    assert set(req["propGuids"]) == {p["guid"] for p in PROPS}

    # Archicad が再計算した結果（自動計算の H も 2.5 に）を表へ反映
    fresh = _payload(h_wall="2.500", reqid=req["reqId"])
    fresh["values"] = fresh["values"][:1]
    fresh["values"][0]["modiStamp"] = 12
    w.on_values(fresh)
    m = w.model
    assert m._values[(GW, "elem:H")] == "2.500"
    assert m._original_values[(GW, "elem:H")] == "2.500"
    assert m._original_values[(GW, "elem:in:height")] == "2.500"
    assert m._stamps[GW] == 12 and m._stamp_flags[GW] == server.PropertyTableModel.STAMP_OK
    assert m._values[(GS, "elem:H")] == "0.200"           # 他の要素はそのまま
    assert "反映後の最新値" in w.status_label.text()
    assert w._refresh_requests == {}


def test_sync_success_with_partial_reason_is_reported(loaded):
    w = loaded
    w.on_sync_complete({"results": [{"guid": GW, "status": "success",
                                     "reason": "一部未反映: elem:in:sill(この要素では対象外) "}]})
    kind, _, text = w.msgbox.calls[-1]
    assert kind == "information" and "一部未反映" in text


def test_no_refresh_when_nothing_succeeded(loaded):
    loaded.on_sync_complete({"results": [{"guid": GW, "status": "error", "reason": "x"}]})
    assert "get_values" not in loaded.sent.commands()


def test_refresh_ignored_after_window_closed(loaded, monkeypatch):
    w = loaded
    panel = w.data_panel
    w._refresh_requests["refresh:x"] = panel
    monkeypatch.setattr(w, "_panels", {})
    before = dict(w.model._values)
    w.on_values(_payload(h_wall="9.000", reqid="refresh:x"))
    assert w.model._values == before


def test_refresh_ignores_unknown_elements_and_db_error(loaded, monkeypatch):
    w = loaded
    w._refresh_requests["refresh:y"] = w.data_panel
    monkeypatch.setattr(w.db, "upsert_values", lambda *a: (_ for _ in ()).throw(RuntimeError("db")))
    w.on_values({"reqId": "refresh:y", "values": [
        {"guid": "OTHER", "modiStamp": 5, "props": {"elem:H": "1"}},
        {"guid": GS, "modiStamp": 21, "props": {"elem:H": "0.250"}},
    ]})
    assert ("OTHER", "elem:H") not in w.model._values
    assert w.model._values[(GS, "elem:H")] == "0.250"


# ── 自動計算と入力元の見分け・入力元の編集ダイアログ ─────────────────

def test_header_and_colors_distinguish_computed_and_input(loaded):
    m = loaded.model
    _, ch = _cell(loaded, GW, "elem:H")
    _, ci = _cell(loaded, GW, "elem:in:height")
    assert m.headerData(ch, Qt.Orientation.Horizontal) == "＝高さ H (m)"
    assert m.headerData(ci, Qt.Orientation.Horizontal) == "入力 高さ"
    assert "自動計算" in m.headerData(ch, Qt.Orientation.Horizontal, Qt.ItemDataRole.ToolTipRole)
    assert "入力元" in m.headerData(ci, Qt.Orientation.Horizontal, Qt.ItemDataRole.ToolTipRole)
    assert m.headerData(0, Qt.Orientation.Vertical) is None
    m._stamp_flags = {GW: m.STAMP_OK, GS: m.STAMP_OK}
    bg = lambda g, pg: m.data(m.index(*_cell(loaded, g, pg)), Qt.ItemDataRole.BackgroundRole)
    assert bg(GW, "elem:H").name().upper() == server.COMPUTED_BG
    assert bg(GW, "elem:in:height").name().upper() == server.INPUT_BG
    assert bg(GS, "elem:in:length") is None                 # 対象外は色なし（灰色文字）
    tip = lambda g, pg: m.data(m.index(*_cell(loaded, g, pg)), Qt.ItemDataRole.ToolTipRole)
    assert "ダブルクリック" in tip(GW, "elem:H")
    assert "対象外" in tip(GS, "elem:in:length")
    assert "単位なしはメートル" in tip(GW, "elem:in:height")


def _stub_dialog(monkeypatch, action, values=None, seen=None):
    def fake_exec(dlg):
        if seen is not None:
            seen.append(dlg)
        for k, v in (values or {}).items():
            dlg._edits[k].setText(v)
        if action == "apply":
            dlg.on_apply()
        elif action == "goto":
            dlg.on_goto()
        return 0
    monkeypatch.setattr(server.InputSourceDialog, "exec", fake_exec)


def test_double_click_computed_opens_dialog_and_applies(loaded, monkeypatch):
    w = loaded
    seen = []
    _stub_dialog(monkeypatch, "apply", {"elem:in:height": "2500mm"}, seen)
    edited = []
    monkeypatch.setattr(w.table, "edit", lambda idx: edited.append(idx))
    r, c = _cell(w, GW, "elem:H")
    w._on_table_double_clicked(w.proxy.mapFromSource(w.model.index(r, c)))
    assert not edited                                       # 閲覧のみの列は直接編集しない
    dlg = seen[0]
    assert list(dlg._edits)[0] == "elem:in:height"          # 関係する入力元が先頭
    assert dlg.goto_key == "elem:in:height"
    assert w.model._values[(GW, "elem:in:height")] == "2.500"
    assert w.undo_stack.count() == 1


def test_dialog_skips_not_applicable_inputs(loaded, monkeypatch):
    seen = []
    _stub_dialog(monkeypatch, None, seen=seen)
    r, _ = _cell(loaded, GS, "elem:H")
    loaded.open_input_sources(r, "elem:H")
    assert set(seen[0]._edits) == {"elem:in:height"}        # スラブ: 長さ・上部リンクは対象外


def test_dialog_invalid_value_stays_open(loaded, monkeypatch):
    seen = []
    _stub_dialog(monkeypatch, "apply", {"elem:in:height": "高い"}, seen)
    r, _ = _cell(loaded, GW, "elem:H")
    loaded.open_input_sources(r, "elem:H")
    assert seen[0].action is None and "解釈できません" in seen[0].error_label.text()
    assert loaded.model._values[(GW, "elem:in:height")] == "3.000"


def test_dialog_goto_moves_to_input_column(loaded, monkeypatch):
    w = loaded
    _stub_dialog(monkeypatch, "goto")
    r, _ = _cell(w, GW, "elem:H")
    w.open_input_sources(r, "elem:H")
    cur = w.proxy.mapToSource(w.table.currentIndex())
    assert (cur.row(), cur.column()) == _cell(w, GW, "elem:in:height")


def test_goto_cell_hidden_by_filter(loaded, monkeypatch):
    from PyQt6.QtCore import QModelIndex
    w = loaded
    monkeypatch.setattr(w.proxy, "mapFromSource", lambda idx: QModelIndex())   # フィルタで非表示
    assert not w.goto_cell(0, 1)
    assert "非表示" in w.status_label.text()


def test_dialog_without_input_columns(main_window):
    w = main_window
    w.current_condition_id = w.db.save_condition([0], {"Wall": True})
    w.current_elements = [ELEMS[0]]
    w.current_props = [PROPS[0]]
    w.on_values(_payload())
    w.open_input_sources(0, "elem:H")
    assert w.msgbox.kinds()[-1] == "information"
    assert "入力元" in w.msgbox.calls[-1][2]
    w.open_input_sources(99)                                # 範囲外は何もしない


def test_dialog_when_all_inputs_not_applicable(loaded):
    for k in ("elem:in:height",):
        loaded.model._values[(GS, k)] = NA
    r, _ = _cell(loaded, GS, "elem:H")
    loaded.open_input_sources(r, "elem:H")
    assert "編集できる入力元がありません" in loaded.msgbox.calls[-1][2]


def test_context_menu_entry_opens_for_current_cell(loaded, monkeypatch):
    w = loaded
    calls = []
    monkeypatch.setattr(w, "open_input_sources", lambda row, key=None: calls.append((row, key)))
    w.open_input_sources_for_current()                      # 未選択
    assert not calls and "行を選んで" in w.status_label.text()
    r, c = _cell(w, GW, "elem:in:length")
    w.table.setCurrentIndex(w.proxy.mapFromSource(w.model.index(r, c)))
    w.open_input_sources_for_current()
    assert calls == [(r, "elem:in:length")]
