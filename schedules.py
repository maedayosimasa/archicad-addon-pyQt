"""Archicad の連動一覧表（スキーム XML）を読み、同じ構成（対象要素・列）でデータを取るための処理。

Archicad はプロジェクトを開くと、その連動一覧表の定義を
  %APPDATA%\\GRAPHISOFT\\Archicad 28.x.x <言語>\\連動一覧表\\<番号> <名前>.xml
に Scheme_Settings 形式で書き出す。ここから
  - 対象要素（Criteria_Settings/Criterion: 要素タイプ・プロパティ・GDL パラメータの条件）
  - 列（Header_Items/Header_Item: プロパティ / GDL パラメータ、見出し階層）
を読み取る。C++ の API には一覧表の内容を取得する関数が無いため、この XML を使う。
"""
import glob
import os
import re
import xml.etree.ElementTree as ET

# ── 要素タイプ GUID（ExtendedElem_ElemClassId）→ アドオンの要素タイプキー ──
# 実プロジェクトの一覧表名・列から特定したもの（壁リスト=壁、建具表（ドア）=ドア 等）
CLASS_TO_TYPE = {
    "4F654582-33AF-4E52-ABFC-C35D46765EC4": "Wall",
    "13E8EB85-5A33-47A3-A4D4-1E05296AEC7E": "Column",
    "A5C2D285-5A7C-4A68-9554-9EDA9D4556CA": "Beam",
    "D8F07689-9CFA-4FBE-AEB4-0A60B8E667EE": "Door",
    "93F161A0-4C4E-4DF7-A100-4FD0E8C4F1E1": "Window",
    "3AD0332E-EFE2-491A-92A6-5B14BFDD7229": "Zone",
    "DBEFCEC3-1BBF-4867-B8B3-C4F7C859DF61": "Object",
}
TYPE_LABELS = {"Wall": "壁", "Column": "柱", "Beam": "梁", "Door": "ドア", "Window": "窓",
               "Zone": "ゾーン", "Object": "オブジェクト"}
ALL_TYPES = sorted(set(CLASS_TO_TYPE.values()))
NULL_GUID = "00000000-0000-0000-0000-000000000000"

# よく使う組み込みプロパティ（条件で使われるもの）
LAYER_PROP = "0F43553E-58E7-4661-8CEB-8B4E5D2ED50A"        # レイヤー（値はレイヤー番号）
HOME_STORY_PROP = "792002ED-8D64-4DE4-877E-817D5CDF798F"   # 配置フロア（値はフロア番号）
HOME_STORY_NAME_PROP = "8583BC95-C85D-48A3-9C2E-1EBED328BBFE"   # 配置フロア名（値取得では番号で返る）
# アドオンが番号ではなく名前（レイヤー名・フロア名）に置き換えて返すプロパティ
NAME_DISPLAY_PROPS = {LAYER_PROP, HOME_STORY_NAME_PROP}

# Relation_Index（実データの値の組み合わせから推定）
REL_EQ, REL_NE, REL_GE, REL_CONTAINS, REL_NOT_CONTAINS, REL_NOT_EMPTY = 1, 6, 4, 9, 12, 22
REL_LABELS = {REL_EQ: "＝", REL_NE: "≠", REL_GE: "≧", REL_CONTAINS: "を含む",
              REL_NOT_CONTAINS: "を含まない", REL_NOT_EMPTY: "が空でない"}

PT_ELEM_TYPE, PT_GDL, PT_PROPERTY = "88", "51", "232"
FIELD_PROPERTY = "180"
TRUE_WORDS = {"true", "yes", "はい", "オン", "on", "1", "有効"}


# ── フォルダと一覧 ───────────────────────────────────────────────────

def find_schedule_folder(appdata=None):
    """Archicad 28 の連動一覧表フォルダ（最も新しく更新されたもの）。無ければ None。"""
    appdata = appdata or os.environ.get("APPDATA", "")
    candidates = []
    for pattern in ("Archicad 28*" + os.sep + "連動一覧表", "Archicad 28*" + os.sep + "Interactive Schedule*"):
        candidates += [p for p in glob.glob(os.path.join(appdata, "GRAPHISOFT", pattern)) if os.path.isdir(p)]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _v(e, tag, default=""):
    x = e.find(tag)
    if x is None:
        return default
    return x.get("value", x.text if x.text is not None else default)


def _clean_caption(text):
    """見出しの改行用の全角/半角スペースの連続を 1 つにまとめる（「防煙区画　　　面積」→「防煙区画 面積」）。"""
    return re.sub(r"[\s　]+", " ", text or "").strip()


def _variant(e):
    var = e.find(".//Variant")
    if var is None:
        return None, None
    return var.get("Type", ""), (var.findtext("Value") or "").strip()


def parse_schedule(path):
    """スキーム XML 1 件 → dict（name, scheme_type, criteria, fields, ...）。"""
    root = ET.parse(path).getroot()
    sched = {
        "path": path,
        "file": os.path.basename(path),
        "name": root.get("Name", ""),
        "id": root.get("ID", ""),
        "scheme_type": root.get("Scheme_Type", ""),
        "criteria": [],
        "fields": [],
    }
    m = re.match(r"(\d+)\s", sched["file"])
    sched["order"] = int(m.group(1)) if m else 9999
    # ナビゲータ（一覧表 > 要素）と同じ表示名: 「ID 名前」（例: 営繕_ 建具表_ドア / 構造 大梁リスト_RC）
    sched["display"] = f"{sched['id']} {sched['name']}".strip()

    for c in root.iter("Criterion"):
        vtype, value = _variant(c)
        sched["criteria"].append({
            "param_type": _v(c, "Param_Type"),
            "relation": int(_v(c, "Relation_Index", "0") or 0),
            "and_next": _v(c, "AndNext", "1") != "0",
            "open": int(_v(c, "Before_Brackets", "0") or 0),
            "close": int(_v(c, "After_Brackets", "0") or 0),
            "elem_class": _v(c, "ExtendedElem_ElemClassId").upper(),
            "special": _v(c, "ExtendedElem_SpecialType"),
            "prop_guid": _v(c, "ACPropertyGuid").upper(),
            "prop_name": _v(c, "ACPropertyName"),
            "prop_group": _v(c, "ACPropertyGroup"),
            "gdl_var": _v(c, "Variable"),
            "label": c.findtext("Name") or "",
            "value_type": vtype,
            "value": value,
        })

    items = root.findall("Header_Items/Header_Item")
    by_id = {_v(i, "ID_of_Item"): i for i in items}
    parents = {_v(i, "ID_of_Parent") for i in items}
    for i in items:
        item_id = _v(i, "ID_of_Item")
        if item_id in parents:
            continue   # 子を持つ見出し（「寸法」等）は列にしない → 見出しの前置きに使う
        # 見出しの階層（最上位の表題は除く）: 寸法 / W
        path_caps, pid = [], _v(i, "ID_of_Parent")
        while pid in by_id and _v(by_id[pid], "ID_of_Parent") in by_id:
            path_caps.insert(0, _v(by_id[pid], "Caption"))
            pid = _v(by_id[pid], "ID_of_Parent")
        caption = _clean_caption(_v(i, "Caption")) or _clean_caption(_v(i, "Parameter_Desc_Name"))
        try:
            sort = int(_v(i, "Sort_below", "0") or 0)
        except ValueError:
            sort = 0
        sched["fields"].append({
            "caption": " / ".join([_clean_caption(p) for p in path_caps if _clean_caption(p)] + [caption]),
            "sort": sort,
            "param_type": _v(i, "Parameter_Type"),
            "param_name": _v(i, "Parameter_Name"),
            "desc": _v(i, "Parameter_Desc_Name"),
            "prop_guid": _v(i, "ACPropertyGuid").upper(),
            "prop_name": _v(i, "ACPropertyName"),
            "prop_group": _v(i, "ACPropertyGroup"),
        })
    return sched


SORT_ASC, SORT_DESC = 1, 2   # Header_Item/Sort_below（0 は見出し・並び替えなし）


def sort_specs(sched, columns):
    """Archicad と同じ行の並び: 列を左から順に、各列の昇順/降順で並べるための (キー, 降順か) のリスト。"""
    shown = {c["guid"] for c in columns}
    specs = []
    for f in sched["fields"]:
        k = field_key(f)
        if f.get("sort") not in (SORT_ASC, SORT_DESC) or k is None:
            continue
        key = k if k.startswith("gdl:") or k in shown else None
        if key is None:
            for c in columns:   # 解決後の GUID（作成プロパティ）に読み替え
                if c.get("ref") == k:
                    key = c["guid"]
                    break
        if key and key not in [s[0] for s in specs]:
            specs.append((key, f["sort"] == SORT_DESC))
    return specs


def sort_rows(rows, specs, key_func):
    """rows（{"props": {...}} のリスト）を specs の順で並べる。key_func は値→比較キー（数値は数値順）。"""
    out = list(rows)
    for key, desc in reversed(specs):   # 安定ソートを右の列から → 左の列が最優先
        out.sort(key=lambda r: key_func(r.get("props", {}).get(key)), reverse=desc)
    return out


def list_schedules(folder):
    """フォルダ内の一覧表（要素一覧のみ）を番号順に。読めないファイルは飛ばす。"""
    out = []
    for path in glob.glob(os.path.join(folder, "*.xml")):
        try:
            s = parse_schedule(path)
        except ET.ParseError:
            continue
        if s["scheme_type"] == "Element_List":
            out.append(s)
    return sorted(out, key=lambda s: (s["order"], s["name"]))


# ── 列・条件 → 取得するキー ─────────────────────────────────────────

def _is_null(guid):
    return not guid or guid == NULL_GUID


def prop_ref(item):
    """プロパティの参照キー: 組み込みは GUID、作成プロパティは「グループ||名前」。"""
    if not _is_null(item.get("prop_guid")):
        return item["prop_guid"]
    if item.get("prop_name"):
        return f"{item.get('prop_group', '')}||{item['prop_name']}"
    return None


def field_key(field):
    """列 → 値の取得キー（プロパティ参照 / gdl:名前）。取れない列（画像・数量）は None。"""
    if field["param_type"] == FIELD_PROPERTY:
        return prop_ref(field)
    if field["param_name"] and field["param_type"] not in ("0", ""):
        return "gdl:" + field["param_name"]
    return None


def schedule_types(sched):
    """取得する要素タイプ（条件で「＝」指定のもの）と、未対応の要素タイプ GUID。"""
    types, unknown, all_types = set(), set(), False
    for c in sched["criteria"]:
        if c["param_type"] != PT_ELEM_TYPE or c["relation"] != REL_EQ:
            continue
        if _is_null(c["elem_class"]):
            all_types = True
        elif c["elem_class"] in CLASS_TO_TYPE:
            types.add(CLASS_TO_TYPE[c["elem_class"]])
        else:
            unknown.add(c["elem_class"])
    if all_types:
        types = set(ALL_TYPES)
    return sorted(types), sorted(unknown)


def required_refs(sched):
    """解決が必要なプロパティ参照（列＋条件）と、条件で使うレイヤー番号。"""
    refs, layers = [], []
    for f in sched["fields"]:
        k = field_key(f)
        if k and not k.startswith("gdl:") and k not in refs:
            refs.append(k)
    for c in sched["criteria"]:
        if c["param_type"] != PT_PROPERTY:
            continue
        r = prop_ref(c)
        if r == HOME_STORY_PROP:
            continue   # フロア番号で判定（elem:floorIndex）
        if r and r not in refs:
            refs.append(r)
        if r == LAYER_PROP and c["value"] and c["value"] not in layers:
            layers.append(c["value"])
    return refs, layers


def to_hex(text):
    return text.encode("utf-8").hex()


# ── 条件の評価 ───────────────────────────────────────────────────────

def _truthy(s):
    return str(s).strip().lower() in TRUE_WORDS


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return None


def _compare(rel, actual, expected, numeric=False):
    """1 条件の比較。対応できない関係は None。"""
    if rel == REL_NOT_EMPTY:
        return actual not in (None, "", "---")
    if actual is None:
        actual = ""
    if numeric:
        a, b = _num(actual), _num(expected)
        if a is None or b is None:
            return rel == REL_NE
        return {REL_EQ: a == b, REL_NE: a != b, REL_GE: a >= b}.get(rel)
    a, b = str(actual), str(expected)
    return {REL_EQ: a == b, REL_NE: a != b, REL_CONTAINS: b in a, REL_NOT_CONTAINS: b not in a,
            REL_GE: (_num(a) or 0) >= (_num(b) or 0)}.get(rel)


def describe_criterion(c):
    rel = REL_LABELS.get(c["relation"], f"関係{c['relation']}")
    if c["param_type"] == PT_ELEM_TYPE:
        t = CLASS_TO_TYPE.get(c["elem_class"])
        name = "全要素" if _is_null(c["elem_class"]) else TYPE_LABELS.get(t, f"未対応タイプ({c['elem_class'][:8]})")
        return f"要素タイプ {rel} {name}"
    label = c["label"] or c["prop_name"] or c["gdl_var"] or "?"
    return f"{label} {rel} {c['value']}".rstrip()


def criterion_supported(c):
    if c["param_type"] == PT_ELEM_TYPE:
        if c["relation"] == REL_NE:
            return True   # 「≠ 未対応タイプ」も、取得する要素はそのタイプではないので常に成り立つ
        return c["relation"] == REL_EQ and (_is_null(c["elem_class"]) or c["elem_class"] in CLASS_TO_TYPE)
    if c["param_type"] == PT_GDL:
        return bool(c["gdl_var"]) and c["relation"] in (REL_EQ, REL_NE, REL_GE)
    if c["param_type"] == PT_PROPERTY:
        if c["relation"] not in REL_LABELS:
            return False
        if c["value_type"] == "Property::AttributeIndexValue":
            return prop_ref(c) == LAYER_PROP
        if prop_ref(c) == HOME_STORY_PROP:
            return c["value_type"] == "IntVariant"
        return prop_ref(c) is not None and c["value_type"] in ("StringVariant", "BoolVariant", "IntVariant",
                                                               "DoubleVariant", "NumVariant", None, "")
    return False


def evaluate_criterion(c, elem, values, ctx):
    """1 条件を 1 要素に適用。values: {キー: 値文字列}。未対応は True（絞り込まない）。"""
    if not criterion_supported(c):
        return True
    if c["param_type"] == PT_ELEM_TYPE:
        if _is_null(c["elem_class"]):
            return True
        same = elem.get("type") == CLASS_TO_TYPE.get(c["elem_class"])
        return same if c["relation"] == REL_EQ else not same
    if c["param_type"] == PT_GDL:
        return bool(_compare(c["relation"], values.get("gdl:" + c["gdl_var"]), c["value"], numeric=True))
    ref = prop_ref(c)
    if ref == HOME_STORY_PROP:
        return bool(_compare(c["relation"], values.get("elem:floorIndex"), c["value"], numeric=True))
    key = ctx.get("ref_to_key", {}).get(ref, ref)
    actual = values.get(key)
    if c["value_type"] == "Property::AttributeIndexValue":
        # レイヤー: Archicad の値取得はアドオンの版によりレイヤー番号（"9"）か名前で返るため両方で照合
        index = str(c["value"]).strip()
        name = ctx.get("layers", {}).get(index)
        matched = str(actual).strip() in {index, name} if actual is not None else False
        if c["relation"] == REL_EQ:
            return matched
        if c["relation"] == REL_NE:
            return not matched
        return True
    if c["value_type"] == "BoolVariant" and c["relation"] in (REL_EQ, REL_NE):
        same = _truthy(actual) == _truthy(c["value"])
        return same if c["relation"] == REL_EQ else not same
    numeric = c["value_type"] in ("IntVariant", "DoubleVariant", "NumVariant") and c["relation"] != REL_CONTAINS
    return bool(_compare(c["relation"], actual, c["value"], numeric=numeric))


def evaluate(criteria, elem, values, ctx):
    """条件全体（AND / OR・括弧）を評価する。AND は OR より先に結合する。"""
    tokens = []
    for i, c in enumerate(criteria):
        tokens += ["("] * c["open"]
        tokens.append(evaluate_criterion(c, elem, values, ctx))
        tokens += [")"] * c["close"]
        if i < len(criteria) - 1:
            tokens.append("and" if c["and_next"] else "or")
    # 括弧の数が合わない定義は、合う範囲だけで評価する
    depth = 0
    fixed = []
    for t in tokens:
        if t == "(":
            depth += 1
        elif t == ")":
            if depth == 0:
                continue
            depth -= 1
        fixed.append(t)
    fixed += [")"] * depth
    pos = 0

    def expr():
        nonlocal pos
        val = term()
        while pos < len(fixed) and fixed[pos] == "or":
            pos += 1
            rhs = term()
            val = val or rhs
        return val

    def term():
        nonlocal pos
        val = factor()
        while pos < len(fixed) and fixed[pos] == "and":
            pos += 1
            rhs = factor()
            val = val and rhs
        return val

    def factor():
        nonlocal pos
        if pos >= len(fixed):
            return True
        t = fixed[pos]
        pos += 1
        if t == "(":
            v = expr()
            if pos < len(fixed) and fixed[pos] == ")":
                pos += 1
            return v
        return bool(t)

    return expr() if fixed else True


# ── 画面用の列定義 ───────────────────────────────────────────────────

def build_columns(sched, resolved):
    """列（表示順）→ データウィンドウ用のプロパティ定義リスト。
    resolved: {参照キー: C++ が返した定義}。解決できない列は missing に入れる。"""
    columns, missing = [], []
    for f in sched["fields"]:
        key = field_key(f)
        if key is None:
            continue
        if key.startswith("gdl:"):
            columns.append({"guid": key, "name": f["caption"], "group": "GDLパラメータ",
                            "editable": False, "valueType": 3, "collectionType": 0})
            continue
        d = resolved.get(key)
        if d is None:
            missing.append(f["caption"])
            continue
        col = dict(d)
        col["name"] = f["caption"] or d.get("name", "")
        if col.get("guid", "").upper() in NAME_DISPLAY_PROPS:
            col["editable"] = False   # 番号を名前に置き換えて表示する列は書き戻さない
        columns.append(col)
    # 同じプロパティを 2 回使う一覧表もあるので、最初の列だけ残す
    seen, unique = set(), []
    for c in columns:
        if c["guid"] not in seen:
            seen.add(c["guid"])
            unique.append(c)
    return unique, missing


def summary(sched):
    """一覧の表示用: 対象タイプ・列数・条件の対応状況。"""
    types, unknown = schedule_types(sched)
    cols = [f for f in sched["fields"] if field_key(f)]
    crit = sched["criteria"]
    unsupported = [describe_criterion(c) for c in crit if not criterion_supported(c)]
    return {
        "types": types,
        "type_labels": "・".join(TYPE_LABELS.get(t, t) for t in types) or "（対象なし）",
        "unknown_types": unknown,
        "columns": len(cols),
        "criteria": len(crit),
        "unsupported": unsupported,
        "openable": bool(types) and bool(cols),
    }
