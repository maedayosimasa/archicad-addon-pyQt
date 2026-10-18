"""データ表示・編集テーブルの Excel 風操作を支える、GUI に依存しない処理。

- クリップボード用のタブ区切り(TSV)変換（Excel とコピー&ペースト互換）
- PropertyDefinition メタ情報（editable / enums）による値の検証
- openpyxl による .xlsx の書き出し・読み込み
"""
import csv
import io
import re

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

# Excel 上の構成: 1 列目 = 要素 GUID、2 列目以降 = プロパティ。
# 非表示シート PROPS_SHEET にプロパティ GUID を保存し、取り込み時の列特定に使う
# （プロパティ名は重複しうるため）。
DATA_SHEET   = "データ"
PROPS_SHEET  = "_props"
GUID_HEADER  = "GUID"
CHANGED_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
CHANGED_FONT = Font(color="9C0006", bold=True)
HEADER_FONT  = Font(bold=True)

_NUMBER_RE = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?$")


# ── クリップボード (TSV) ───────────────────────────────────────────

def to_tsv(rows):
    """2 次元リスト → Excel に貼り付け可能なタブ区切り文字列。"""
    buf = io.StringIO()
    writer = csv.writer(buf, dialect="excel-tab", lineterminator="\n")
    for r in rows:
        writer.writerow(["" if v is None else str(v) for v in r])
    return buf.getvalue()


def parse_tsv(text):
    """Excel 等からコピーしたタブ区切り文字列 → 2 次元リスト。

    末尾の改行は無視し、セル内改行を含む引用符付きセルにも対応する。
    """
    if not text:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.endswith("\n"):
        text = text[:-1]
    if "\t" not in text and "\n" not in text and '"' not in text:
        return [[text]]
    return [row for row in csv.reader(io.StringIO(text), dialect="excel-tab")]


# ── 検証 ───────────────────────────────────────────────────────────

def is_editable(prop):
    return bool(prop.get("editable", True))


def validate_value(prop, value):
    """プロパティ定義に照らして値を検証する。(ok, 理由) を返す。

    - editable=False のプロパティは変更不可
    - 列挙値 (enums) を持つプロパティは候補にある値のみ許可
    数値型などの詳細な型検証は ArchiCAD 側 (BuildChangedProperties) が行う。
    """
    name = prop.get("name", prop.get("guid", ""))
    if not is_editable(prop):
        return False, f"{name}: 編集不可のプロパティ"
    enums = prop.get("enums") or []
    if enums and str(value) not in enums:
        return False, f"{name}: 「{value}」は候補にありません"
    return True, ""


def values_equal(a, b):
    """表示文字列として等しいか、数値として等しければ True（"100.50" と 100.5 など）。"""
    sa, sb = str(a), str(b)
    if sa == sb:
        return True
    try:
        return float(sa) == float(sb)
    except ValueError:
        return False


# ── 置換 ───────────────────────────────────────────────────────────

def replace_text(value, find, replace, match_case=False, whole_cell=False, regex=False):
    """1 セル分の置換。一致しなければ None、一致すれば置換後の文字列を返す。

    - match_case: 大文字小文字を区別
    - whole_cell: セル全体が一致する場合のみ（find="" なら空欄セルに一致）
    - regex:      find を正規表現として扱い、replace で \\1 等の後方参照を使える
    不正な正規表現は ValueError。
    """
    s = "" if value is None else str(value)
    if find == "" and not whole_cell:
        return None
    pattern = find if regex else re.escape(find)
    try:
        rx = re.compile(pattern, 0 if match_case else re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"正規表現が正しくありません: {e}") from e
    if whole_cell:
        m = rx.fullmatch(s)
        if not m:
            return None
        return m.expand(replace) if regex else replace
    # 正規表現でない場合は置換文字列の "\" をそのまま扱う（関数で渡す）
    new, n = rx.subn(replace if regex else (lambda _m: replace), s)
    return new if n else None


# ── 並び替え用キー ─────────────────────────────────────────────────

def sort_key(value):
    """数値は数値として、文字列は大文字小文字を無視して比較するためのキー。

    空欄と "---"（値なし）は常に (ascending のとき) 末尾に来るよう最大グループにする。
    """
    s = "" if value is None else str(value).strip()
    if s in ("", "---"):
        return (2, 0.0, "")
    try:
        return (0, float(s.replace(",", "")), "")
    except ValueError:
        return (1, 0.0, s.casefold())


# ── Excel 入出力 ───────────────────────────────────────────────────

def _to_excel_cell(value):
    """数値として素直に解釈できる文字列は数値で書き出す（Excel で並び替え・計算できる）。"""
    s = "" if value is None else str(value)
    if _NUMBER_RE.match(s):
        return float(s) if "." in s else int(s)
    return s


def _from_excel_cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def export_xlsx(path, props, rows, changed=frozenset()):
    """テーブルを .xlsx に書き出す。

    props:   [{"guid", "name", ...}]（列順）
    rows:    [(element_guid, [値, ...])]（表示順）
    changed: {(element_guid, prop_guid)} — 赤塗りするセル（未反映の変更）
    """
    wb = Workbook()
    ws = wb.active
    ws.title = DATA_SHEET
    ws.append([GUID_HEADER] + [p.get("name", p["guid"]) for p in props])
    for c in ws[1]:
        c.font = HEADER_FONT
    for guid, values in rows:
        ws.append([guid] + [_to_excel_cell(v) for v in values])
        r = ws.max_row
        for ci, p in enumerate(props):
            if (guid, p["guid"]) in changed:
                cell = ws.cell(row=r, column=ci + 2)
                cell.fill = CHANGED_FILL
                cell.font = CHANGED_FONT
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    ws.column_dimensions["A"].width = 40
    for ci, p in enumerate(props):
        ws.column_dimensions[get_column_letter(ci + 2)].width = max(10, min(40, len(p.get("name", "")) * 2 + 4))

    meta = wb.create_sheet(PROPS_SHEET)
    meta.append(["column", "property_guid", "name"])
    for ci, p in enumerate(props):
        meta.append([ci + 2, p["guid"], p.get("name", "")])
    meta.sheet_state = "hidden"
    wb.save(path)


def read_xlsx(path, props):
    """export_xlsx 形式（または GUID 列 + プロパティ名ヘッダの表）を読み込む。

    戻り値: (values, unknown_headers)
      values:          {(element_guid, prop_guid): 文字列値}
      unknown_headers: 対応するプロパティが見つからなかった列見出し
    """
    wb = load_workbook(path, read_only=False, data_only=True)
    try:
        ws = wb[DATA_SHEET] if DATA_SHEET in wb.sheetnames else wb.worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return {}, []
        header = [("" if h is None else str(h).strip()) for h in rows[0]]
        if GUID_HEADER not in header:
            raise ValueError(f"1 行目に「{GUID_HEADER}」列が見つかりません")
        guid_col = header.index(GUID_HEADER)

        # 列 → プロパティ GUID（_props シートがあれば最優先、無ければ名前で照合）
        known = {p["guid"] for p in props}
        col_to_prop = {}
        if PROPS_SHEET in wb.sheetnames:
            for col, pguid, _name in list(wb[PROPS_SHEET].iter_rows(values_only=True))[1:]:
                if pguid in known and isinstance(col, int):
                    col_to_prop[col - 1] = pguid
        by_name = {}
        for p in props:
            by_name.setdefault(p.get("name", ""), p["guid"])
        unknown = []
        for ci, h in enumerate(header):
            if ci == guid_col or ci in col_to_prop or not h:
                continue
            if h in by_name:
                col_to_prop[ci] = by_name[h]
            else:
                unknown.append(h)

        values = {}
        for r in rows[1:]:
            if guid_col >= len(r) or not r[guid_col]:
                continue
            g = str(r[guid_col]).strip()
            for ci, pguid in col_to_prop.items():
                if ci < len(r):
                    values[(g, pguid)] = _from_excel_cell(r[ci])
        return values, unknown
    finally:
        wb.close()
