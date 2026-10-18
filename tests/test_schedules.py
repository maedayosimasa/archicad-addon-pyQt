"""連動一覧表（スキーム XML）の解析・条件判定と、一覧表からデータウィンドウを開く流れのテスト。

XML は Archicad が書き出す Scheme_Settings と同じ形式で作る（実プロジェクトの構造に合わせた最小版）。
"""
import os
from xml.sax.saxutils import escape

import pytest
from PyQt6.QtCore import Qt

import schedules as S
import server

WALL, DOOR, WIN, ZONE = ("4F654582-33AF-4E52-ABFC-C35D46765EC4", "D8F07689-9CFA-4FBE-AEB4-0A60B8E667EE",
                         "93F161A0-4C4E-4DF7-A100-4FD0E8C4F1E1", "3AD0332E-EFE2-491A-92A6-5B14BFDD7229")
BEAM = "A5C2D285-5A7C-4A68-9554-9EDA9D4556CA"
STAIR = "FA0F7F15-29B4-4905-80FA-C6769109847B"
NULL = S.NULL_GUID
P_WIDTH, P_HEIGHT, P_ID = ("3799B10A-61C5-4566-BF9C-EAA9CE49196E", "C4B62357-1289-4D43-A3F6-AB02B192864C",
                           "B1B54D45-0000-0000-0000-000000000001")


def crit(pt, rel, cls=NULL, guid=NULL, name="", group="", vtype=None, value="", var="", open_=0, close=0,
         and_next=1, label=""):
    val = "" if vtype is None else (
        f"<Value><Variant Type=\"{vtype}\"><Status>Normal</Status><Value>{escape(str(value))}</Value></Variant></Value>")
    return f"""<Criterion><Param_Type value="{pt}"/><Relation_Index value="{rel}"/>
      <ACPropertyGuid value="{guid}"/><ACPropertyName value="{escape(name)}"/><ACPropertyGroup value="{escape(group)}"/>
      <UniValue><HasVariant>true</HasVariant><ByValue>false</ByValue>{val}</UniValue>
      <Variable value="{var}"/><Name>{escape(label)}</Name>
      <Before_Brackets value="{open_}"/><After_Brackets value="{close}"/><AndNext value="{and_next}"/>
      <ExtendedElem_ElemClassId value="{cls}"/><ExtendedElem_SpecialType value="3"/></Criterion>"""


def field(item_id, parent, cap, ptype="180", pname="", guid=NULL, name="", group="", desc=""):
    return f"""<Header_Item><ID_of_Item value="{item_id}"/><ID_of_Parent value="{parent}"/>
      <Caption value="{escape(cap)}"/><Parameter_Type value="{ptype}"/><Parameter_Name value="{pname}"/>
      <Parameter_Desc_Name value="{escape(desc)}"/><ACPropertyGuid value="{guid}"/>
      <ACPropertyName value="{escape(name)}"/><ACPropertyGroup value="{escape(group)}"/></Header_Item>"""


def scheme(folder, no, name, criteria, fields, scheme_type="Element_List"):
    xml = (f'<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n'
           f'<Scheme_Settings ID="" Name="{escape(name)}" Scheme_Type="{scheme_type}" Version="28.0.0">'
           f'<Criteria_Settings><Complex_Criteria>{"".join(criteria)}</Complex_Criteria></Criteria_Settings>'
           f'<Header_Items>{"".join(fields)}</Header_Items></Scheme_Settings>')
    path = os.path.join(folder, f"{no} {name}.xml")
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(xml)
    return path


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "連動一覧表"
    d.mkdir()
    f = str(d)
    # 建具表: (ドア OR 窓) AND 建具種類 ≠ SS AND GDL kaj_door_ident ≠ 0 / 見出し階層「建具主寸法 / W」
    scheme(f, 18, "建具表_ドア", [
        crit("88", 1, cls=DOOR, open_=1, and_next=0),
        crit("88", 1, cls=WIN, close=1),
        crit("51", 6, var="kaj_door_ident", vtype="NumVariant", value="0"),
        crit("232", 6, name="建具種類", group="建具情報(共通)", vtype="StringVariant", value="SS"),
    ], [
        field(1, 0, "建具表_ドア", ptype="0"),
        field(2, 1, "", name="建具種類", group="建具情報(共通)", desc="建具種類"),
        field(3, 1, "数量", ptype="1"),
        field(4, 1, "建具主寸法", ptype="0"),
        field(5, 4, "W", guid=P_WIDTH, desc="幅"),
        field(6, 4, "H", guid=P_HEIGHT, desc="高さ"),
        field(7, 1, "建具番号", ptype="1", pname="type_no"),
        field(8, 1, "記号", ptype="0", desc="2Dシンボル"),
    ])
    # 大梁リスト: 梁 AND レイヤー = 4 AND 要素ID に G を含む
    scheme(f, 23, "大梁リスト_RC", [
        crit("88", 1, cls=BEAM),
        crit("232", 1, guid=S.LAYER_PROP, vtype="Property::AttributeIndexValue", value="4", label="レイヤー"),
        crit("232", 9, guid=P_ID, vtype="StringVariant", value="G", label="要素ID"),
    ], [field(1, 0, "大梁リスト", ptype="0"), field(2, 1, "要素ID", guid=P_ID)])
    # 内部仕上表 2階以上: ゾーン AND 配置フロア ≥ 1 AND 仕上表に表示 = true
    scheme(f, 54, "内部仕上表 2階以上", [
        crit("88", 1, cls=ZONE),
        crit("232", 4, guid=S.HOME_STORY_PROP, vtype="IntVariant", value="1", label="配置フロア"),
        crit("232", 1, name="仕上表に表示", group="内部仕上", vtype="BoolVariant", value="true"),
    ], [field(1, 0, "仕上表", ptype="0"), field(2, 1, "床", name="床仕上", group="内部仕上")])
    scheme(f, 56, "階段", [crit("88", 1, cls=STAIR)], [field(1, 0, "階段", ptype="0"), field(2, 1, "ID", guid=P_ID)])
    scheme(f, 1, "シートインデックス", [], [], scheme_type="Layout_List")
    with open(os.path.join(f, "99 壊れた.xml"), "w", encoding="utf-8") as fp:
        fp.write("<Scheme_Settings")
    return f


# ── 解析 ─────────────────────────────────────────────────────────────

def test_list_only_element_lists_in_number_order(folder):
    names = [s["name"] for s in S.list_schedules(folder)]
    assert names == ["建具表_ドア", "大梁リスト_RC", "内部仕上表 2階以上", "階段"]   # 図面一覧・壊れた XML は除外


def test_fields_columns_and_caption_path(folder):
    s = S.list_schedules(folder)[0]
    keys = [(f["caption"], S.field_key(f)) for f in s["fields"]]
    assert keys == [
        ("建具種類", "建具情報(共通)||建具種類"),       # 見出しが空なら説明名、作成プロパティは グループ||名前
        ("数量", None),                                # 数量は列にしない
        ("建具主寸法 / W", P_WIDTH),                   # 親見出しを前置き
        ("建具主寸法 / H", P_HEIGHT),
        ("建具番号", "gdl:type_no"),                   # GDL パラメータ
        ("記号", None),                                # 2D シンボル（画像）は取れない
    ]


def test_types_refs_and_summary(folder):
    door, beam, zone, stair = S.list_schedules(folder)
    assert S.schedule_types(door) == (["Door", "Window"], [])
    refs, layers = S.required_refs(beam)
    assert refs == [P_ID, S.LAYER_PROP] and layers == ["4"]
    refs, layers = S.required_refs(zone)
    assert "内部仕上||仕上表に表示" in refs and S.HOME_STORY_PROP not in refs   # 配置フロアは番号で判定
    sm = S.summary(stair)
    assert not sm["openable"] and sm["unknown_types"] == [STAIR]
    assert S.summary(door)["type_labels"] == "ドア・窓" and S.summary(door)["unsupported"] == []


def test_find_schedule_folder(tmp_path):
    assert S.find_schedule_folder(str(tmp_path)) is None
    d = tmp_path / "GRAPHISOFT" / "Archicad 28.0.0 JPN R1" / "連動一覧表"
    d.mkdir(parents=True)
    assert S.find_schedule_folder(str(tmp_path)) == str(d)


def test_to_hex_roundtrip():
    assert bytes.fromhex(S.to_hex("建具情報(共通)||建具種類")).decode("utf-8") == "建具情報(共通)||建具種類"


# ── 条件判定 ─────────────────────────────────────────────────────────

def test_evaluate_door_schedule_with_brackets(folder):
    door = S.list_schedules(folder)[0]
    ctx = {"ref_to_key": {"建具情報(共通)||建具種類": "P-KIND"}}
    ok = lambda t, kind, ident: S.evaluate(door["criteria"], {"type": t},   # noqa: E731
                                           {"P-KIND": kind, "gdl:kaj_door_ident": ident}, ctx)
    assert ok("Door", "AD", "1") and ok("Window", "AW", "1")
    assert not ok("Door", "SS", "1")            # 建具種類 ≠ SS
    assert not ok("Window", "AW", "0")          # kaj_door_ident ≠ 0
    assert not ok("Wall", "AD", "1")            # (ドア OR 窓)


def test_evaluate_layer_contains_and_story(folder):
    _, beam, zone, _ = S.list_schedules(folder)
    ctx = {"layers": {"4": "20-大梁"}, "ref_to_key": {S.LAYER_PROP: S.LAYER_PROP, P_ID: P_ID}}
    assert S.evaluate(beam["criteria"], {"type": "Beam"}, {S.LAYER_PROP: "20-大梁", P_ID: "2G1"}, ctx)
    assert not S.evaluate(beam["criteria"], {"type": "Beam"}, {S.LAYER_PROP: "20-小梁", P_ID: "2G1"}, ctx)
    assert not S.evaluate(beam["criteria"], {"type": "Beam"}, {S.LAYER_PROP: "20-大梁", P_ID: "B1"}, ctx)
    zctx = {"ref_to_key": {"内部仕上||仕上表に表示": "P-SHOW"}}
    assert S.evaluate(zone["criteria"], {"type": "Zone"}, {"elem:floorIndex": "2", "P-SHOW": "はい"}, zctx)
    assert not S.evaluate(zone["criteria"], {"type": "Zone"}, {"elem:floorIndex": "0", "P-SHOW": "True"}, zctx)
    assert not S.evaluate(zone["criteria"], {"type": "Zone"}, {"elem:floorIndex": "3", "P-SHOW": "いいえ"}, zctx)


@pytest.mark.parametrize("rel, actual, expected, numeric, result", [
    (S.REL_EQ, "a", "a", False, True), (S.REL_NE, "a", "a", False, False),
    (S.REL_CONTAINS, "2G1", "G", False, True), (S.REL_NOT_CONTAINS, "駐輪場A", "駐輪場", False, False),
    (S.REL_NOT_EMPTY, "", "x", False, False), (S.REL_NOT_EMPTY, "FS", "", False, True),
    (S.REL_NOT_EMPTY, "---", "", False, False),
    (S.REL_GE, "2", "1", True, True), (S.REL_GE, "0", "1", True, False),
    (S.REL_EQ, "abc", "1", True, False), (S.REL_NE, "abc", "1", True, True),
    (S.REL_EQ, None, "", False, True), (99, "a", "a", False, None),
])
def test_compare(rel, actual, expected, numeric, result):
    assert S._compare(rel, actual, expected, numeric) == result


def test_unsupported_criteria_do_not_filter():
    weird = [{"param_type": "232", "relation": 77, "and_next": True, "open": 0, "close": 0,
              "elem_class": NULL, "prop_guid": "X", "prop_name": "", "prop_group": "", "gdl_var": "",
              "label": "謎", "value_type": "StringVariant", "value": "a"}]
    assert not S.criterion_supported(weird[0])
    assert S.evaluate(weird, {"type": "Wall"}, {}, {})
    assert "関係77" in S.describe_criterion(weird[0])
    assert S.evaluate([], {}, {}, {}) is True


def test_unbalanced_brackets_are_tolerated():
    c = lambda v, o, cl, a=True: {"param_type": "88", "relation": 1, "and_next": a, "open": o, "close": cl,   # noqa: E731
                                  "elem_class": WALL if v else DOOR, "prop_guid": "", "prop_name": "",
                                  "prop_group": "", "gdl_var": "", "label": "", "value_type": "", "value": ""}
    assert S.evaluate([c(True, 0, 2), c(True, 2, 0)], {"type": "Wall"}, {}, {})


def test_build_columns(folder):
    door = S.list_schedules(folder)[0]
    resolved = {"建具情報(共通)||建具種類": {"guid": "P-KIND", "name": "建具種類", "group": "建具情報(共通)",
                                         "editable": True, "enums": ["AD", "SS"]},
                P_WIDTH: {"guid": P_WIDTH, "name": "幅", "group": "", "editable": False}}
    cols, missing = S.build_columns(door, resolved)
    assert [(c["guid"], c["name"]) for c in cols] == [
        ("P-KIND", "建具種類"), (P_WIDTH, "建具主寸法 / W"), ("gdl:type_no", "建具番号")]
    assert cols[0]["enums"] == ["AD", "SS"] and cols[2]["editable"] is False
    assert missing == ["建具主寸法 / H"]


# ── 画面: 一覧表から開く ─────────────────────────────────────────────

@pytest.fixture
def win(main_window, folder, monkeypatch):
    monkeypatch.setenv("BIM_SCHEDULE_DIR", folder)
    return main_window


def _resolve_reply(cmd):
    defs = []
    for g in cmd["propGuids"]:
        defs.append({"ref": g, "guid": g, "name": {P_WIDTH: "幅", P_HEIGHT: "高さ"}.get(g, g[:8]),
                     "group": "", "editable": g != P_HEIGHT})
    for h in cmd["propNamesHex"]:
        ref = bytes.fromhex(h).decode("utf-8")
        group, name = ref.split("||")
        defs.append({"ref": ref, "guid": "P-" + name, "name": name, "group": group, "editable": True,
                     "enums": ["AD", "AW", "SS"] if name == "建具種類" else None})
    return {"type": "schedule_resolved", "reqId": cmd["reqId"], "definitions": defs, "layers": {}}


def _open_door_schedule(w):
    dlg = w.show_schedule_list()
    dlg.tbl.selectRow(0)
    dlg.btn_open.click()
    cmd = w.sent.last("resolve_schedule")
    w.on_schedule_resolved(_resolve_reply(cmd))
    get_el = w.sent.last("get_elements")
    w.on_elements({"type": "elements", "reqId": get_el["reqId"], "elements": [
        {"guid": "D1", "type": "Door"}, {"guid": "D2", "type": "Door"}, {"guid": "W1", "type": "Window"}]})
    get_v = w.sent.last("get_values")
    w.on_values({"type": "property_values", "reqId": get_v["reqId"], "values": [
        {"guid": "D1", "modiStamp": 1, "props": {"P-建具種類": "AD", P_WIDTH: "0.9", P_HEIGHT: "2.1",
                                                 "gdl:type_no": "1", "gdl:kaj_door_ident": "1"}},
        {"guid": "D2", "modiStamp": 2, "props": {"P-建具種類": "SS", P_WIDTH: "1.8", P_HEIGHT: "2.1",
                                                 "gdl:type_no": "2", "gdl:kaj_door_ident": "1"}},
        {"guid": "W1", "modiStamp": 3, "props": {"P-建具種類": "AW", P_WIDTH: "1.2", P_HEIGHT: "1.5",
                                                 "gdl:type_no": "3", "gdl:kaj_door_ident": "1"}},
    ]})
    return dlg, cmd, get_el, get_v


def test_schedule_list_dialog(win):
    dlg = win.show_schedule_list()
    assert dlg.tbl.rowCount() == 4 and "4 件" in dlg.folder_label.text()
    rows = [[dlg.tbl.item(r, c).text() for c in range(6)] for r in range(4)]
    assert rows[0][:5] == ["18", "建具表_ドア", "ドア・窓", "4", "4"]   # 列は 数量・記号 を除く 4
    assert rows[3][5] == "対象要素が未対応"
    assert dlg.tbl.item(3, 1).foreground().color().name() == "#808080"
    dlg.filter_edit.setText("梁")
    assert [dlg.tbl.isRowHidden(r) for r in range(4)] == [True, False, True, True]
    assert win.show_schedule_list() is dlg                    # 2 回目は同じダイアログ（再読込）


def test_open_schedule_full_flow(win):
    dlg, cmd, get_el, get_v = _open_door_schedule(win)
    assert set(cmd["propGuids"]) == {P_WIDTH, P_HEIGHT}
    assert [bytes.fromhex(h).decode() for h in cmd["propNamesHex"]] == ["建具情報(共通)||建具種類"]
    assert get_el["Door"] is True and get_el["Window"] is True and get_el["stories"] == []
    assert get_el["limit"] == win.SCHEDULE_ELEMENT_LIMIT
    assert "gdl:kaj_door_ident" in get_v["propGuids"]         # 条件用の値も取る
    panel = win.data_panel
    m = panel.model
    assert [e["guid"] for e in m._elements] == ["D1", "W1"]   # 建具種類 SS は除外
    assert [m.headerData(c, Qt.Orientation.Horizontal) for c in range(m.columnCount())] == [
        "ID", "建具種類", "建具主寸法 / W", "建具主寸法 / H", "建具番号"]
    assert m.index(0, 2).data() == "0.9"
    assert ("D1", "gdl:kaj_door_ident") not in m._values       # 条件用の値は表に出さない
    assert panel.windowTitle().startswith("一覧表: 建具表_ドア") and "(2件 × 4列)" in panel.windowTitle()
    assert win.db.find_window("schedule:18 建具表_ドア.xml") is not None
    assert "2 件を表示" in dlg.info_label.text()
    assert panel.isVisible() and win._schedule_jobs == {}


def test_schedule_window_is_editable_with_enums(win):
    _open_door_schedule(win)
    panel = win.data_panel
    assert panel.model.setData(panel.model.index(0, 1), "AW")             # 候補にある値
    changed, skipped = win._apply_source_edits([(0, 1, "ZZ")], "テスト")   # 候補外
    assert changed == 0 and skipped
    assert not (panel.model.flags(panel.model.index(0, 3)) & Qt.ItemFlag.ItemIsEditable)   # 編集不可の高さ


def test_same_schedule_is_not_opened_twice(win):
    _open_door_schedule(win)
    before = len(win.sent)
    dlg = win.show_schedule_list()
    dlg.tbl.selectRow(0)
    dlg.btn_open.click()
    assert len(win.sent) == before
    assert "既に開いています" in win.msgbox.calls[-1][2]


def test_schedule_while_loading_and_unopenable(win):
    dlg = win.show_schedule_list()
    dlg.tbl.selectRow(0); dlg.btn_open.click()
    dlg.tbl.selectRow(0); dlg.btn_open.click()                # 取得中にもう一度
    assert "取得中" in dlg.info_label.text()
    assert len([s for s in win.sent if s["command"] == "resolve_schedule"]) == 1
    dlg.tbl.selectRow(3); dlg.btn_open.click()                # 階段（未対応）
    assert "開けません" in dlg.info_label.text()
    dlg.tbl.clearSelection(); dlg.open_selected()
    assert dlg.info_label.text() == "一覧表を選択してください。"


def test_schedule_without_columns_or_elements(win):
    dlg = win.show_schedule_list()
    dlg.tbl.selectRow(0); dlg.btn_open.click()
    cmd = win.sent.last("resolve_schedule")
    win.on_schedule_resolved({"reqId": cmd["reqId"], "definitions": [], "layers": {}})
    assert win.condition_status_label.text().endswith("取得できる列がありません") is False  # gdl 列があるので続行
    get_el = win.sent.last("get_elements")
    win.on_elements({"reqId": get_el["reqId"], "elements": []})
    assert win.condition_status_label.text().endswith("対象の要素がありません")
    assert win._schedule_jobs == {}

    dlg.tbl.selectRow(1); dlg.btn_open.click()                # 大梁: 列は要素IDのみ → 解決できない
    cmd = win.sent.last("resolve_schedule")
    win.on_schedule_resolved({"reqId": cmd["reqId"], "definitions": [], "layers": {}})
    assert win.condition_status_label.text().endswith("取得できる列がありません")
    win.on_schedule_resolved({"reqId": "unknown"})             # 関係ない応答は無視


def test_schedule_no_matching_elements_and_error(win):
    dlg = win.show_schedule_list()
    dlg.tbl.selectRow(0); dlg.btn_open.click()
    cmd = win.sent.last("resolve_schedule")
    win.on_schedule_resolved(_resolve_reply(cmd))
    get_el = win.sent.last("get_elements")
    win.on_elements({"reqId": get_el["reqId"], "capped": True, "elements": [{"guid": "D2", "type": "Door"}]})
    get_v = win.sent.last("get_values")
    win.on_values({"reqId": get_v["reqId"], "values": [
        {"guid": "D2", "props": {"P-建具種類": "SS", "gdl:kaj_door_ident": "1"}}]})
    assert win.condition_status_label.text().endswith("条件に合う要素がありません")
    assert win.open_panels() == []

    dlg.btn_open.click()
    assert win._schedule_jobs
    win.on_error("接続できません")
    assert win._schedule_jobs == {} and "中止" in dlg.info_label.text()


def test_schedule_notes_for_missing_columns_and_cap(win):
    dlg = win.show_schedule_list()
    dlg.tbl.selectRow(0); dlg.btn_open.click()
    cmd = win.sent.last("resolve_schedule")
    reply = _resolve_reply(cmd)
    reply["definitions"] = [d for d in reply["definitions"] if d["guid"] != P_HEIGHT]
    win.on_schedule_resolved(reply)
    get_el = win.sent.last("get_elements")
    win.on_elements({"reqId": get_el["reqId"], "capped": True, "elements": [{"guid": "D1", "type": "Door"}]})
    get_v = win.sent.last("get_values")
    win.on_values({"reqId": get_v["reqId"], "values": [
        {"guid": "D1", "props": {"P-建具種類": "AD", "gdl:kaj_door_ident": "1"}}, {"guid": "GONE", "props": {}}]})
    text = win.data_panel.status_label.text()
    assert "取得できなかった列: 建具主寸法 / H" in text and "打ち切り" in text


def test_schedule_list_without_folder(main_window, monkeypatch, tmp_path):
    monkeypatch.setenv("BIM_SCHEDULE_DIR", str(tmp_path / "none"))
    dlg = main_window.show_schedule_list()
    assert dlg.tbl.rowCount() == 0 and "見つかりません" in dlg.folder_label.text()


def test_layer_criterion_matches_index_or_name():
    """値取得ではレイヤーが番号（"9"）で返る。番号でも名前でも一致させる（内部仕上表 1階 が 0 件になる不具合）。"""
    c = {"param_type": "232", "relation": S.REL_EQ, "and_next": True, "open": 0, "close": 0,
         "elem_class": NULL, "prop_guid": S.LAYER_PROP, "prop_name": "", "prop_group": "", "gdl_var": "",
         "label": "レイヤー", "value_type": "Property::AttributeIndexValue", "value": "9"}
    ctx = {"layers": {"9": "20-ゾーン"}, "ref_to_key": {S.LAYER_PROP: S.LAYER_PROP}}
    assert S.evaluate_criterion(c, {}, {S.LAYER_PROP: "9"}, ctx)              # 番号で返る場合
    assert S.evaluate_criterion(c, {}, {S.LAYER_PROP: "20-ゾーン"}, ctx)      # 名前で返る場合
    assert not S.evaluate_criterion(c, {}, {S.LAYER_PROP: "12"}, ctx)
    assert not S.evaluate_criterion(c, {}, {}, ctx)
    assert S.evaluate_criterion(c, {}, {S.LAYER_PROP: "9"}, {"ref_to_key": {}})   # 名前が解決できなくても番号で一致
    ne = dict(c, relation=S.REL_NE)
    assert not S.evaluate_criterion(ne, {}, {S.LAYER_PROP: "9"}, ctx)
    assert S.evaluate_criterion(ne, {}, {S.LAYER_PROP: "3"}, ctx)
    other = dict(c, relation=S.REL_CONTAINS)
    assert S.evaluate_criterion(other, {}, {S.LAYER_PROP: "3"}, ctx)          # 未対応の関係は絞り込まない


def test_name_display_columns_are_read_only():
    sched = {"fields": [{"caption": "階", "param_type": "180", "param_name": "", "desc": "",
                         "prop_guid": S.HOME_STORY_NAME_PROP, "prop_name": "", "prop_group": "", "sort": 1}]}
    cols, _ = S.build_columns(sched, {S.HOME_STORY_NAME_PROP: {"guid": S.HOME_STORY_NAME_PROP,
                                                               "name": "配置フロア名", "editable": True}})
    assert cols[0]["editable"] is False


def test_sort_specs_and_rows():
    sched = {"fields": [
        {"caption": "階", "param_type": "180", "param_name": "", "prop_guid": "A", "prop_name": "", "prop_group": "", "sort": 1},
        {"caption": "名", "param_type": "180", "param_name": "", "prop_guid": NULL, "prop_name": "名",
         "prop_group": "G", "sort": 2},
        {"caption": "画", "param_type": "0", "param_name": "", "prop_guid": NULL, "prop_name": "", "prop_group": "", "sort": 1},
    ]}
    cols = [{"guid": "A"}, {"guid": "P-NAME", "ref": "G||名"}]
    specs = S.sort_specs(sched, cols)
    assert specs == [("A", False), ("P-NAME", True)]
    rows = [{"props": {"A": "2FL", "P-NAME": "a"}}, {"props": {"A": "1FL", "P-NAME": "a"}},
            {"props": {"A": "1FL", "P-NAME": "b"}}]
    import table_tools
    out = S.sort_rows(rows, specs, table_tools.sort_key)
    assert [(r["props"]["A"], r["props"]["P-NAME"]) for r in out] == [("1FL", "b"), ("1FL", "a"), ("2FL", "a")]


def test_display_name_and_caption_cleanup(tmp_path):
    p = scheme(str(tmp_path), 7, "建具表_ドア", [crit("88", 1, cls=DOOR)],
               [field(1, 0, "表", ptype="0"), field(2, 1, "防煙区画\u3000\u3000\u3000面積", guid=P_WIDTH)])
    import xml.etree.ElementTree as ET
    tree = ET.parse(p); tree.getroot().set("ID", "営繕_"); tree.write(p, encoding="utf-8")
    s = S.parse_schedule(p)
    assert s["display"] == "営繕_ 建具表_ドア"
    assert s["fields"][0]["caption"] == "防煙区画 面積"