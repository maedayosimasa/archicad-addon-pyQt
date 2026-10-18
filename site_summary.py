"""敷地マネージャーのパレット相当の情報を、アドオンの get_site_summary 応答から組み立てる。

データの所在（実プロジェクトで確認済み）:
- 敷地の形状・境界線・用途地域 … 敷地オブジェクト(GDL) の vertexX/YArray, lengthBoundary,
  iBoundaryTypeArray, iSiteCategory1..4（strSiteCategory の 1 始まり番号）, numAreaBoundary
- 道路 … 道路境界線(GDL) の strRoadName, iRoadType/strRoadType, iRoadProperty/strRoadProperty
- 集計結果・許容値 … プロジェクト情報（敷地面積の合計, 建築面積建物物全体 (合計),
  延べ面積 容積対象, 許容建蔽率, 許容容積率, 規定建蔽率1..4 など）
"""
import math
import re

SITE_LIBPART = "敷地オブジェクト"
ROAD_LIBPART = "道路境界線"
# 延焼ライン関連（Archicad 29 の敷地マネージャー: 延焼ライン / みなし境界線 / FireSpreading*、
# 名前に「延焼」を含むライブラリ部品、レイヤ名に「延焼」を含む線・ポリライン・円弧）
FIRE_KEYWORDS = ("延焼", "みなし境界", "FireSpreading")
DETAIL_ARRAY_MAX = 10   # 詳細表で配列パラメータを並べる最大個数

# プロジェクト情報のうち「集計結果」として表示する項目（説明文と一致させる）
PROJECT_KEYS = [
    "用途地域1", "用途地域2", "用途地域3", "用途地域4",
    "敷地面積1", "敷地面積2", "敷地面積3", "敷地面積4", "敷地面積の合計",
    "建築面積建物物全体 (合計)", "延べ面積建物物全体 (合計)", "延べ面積 容積対象",
    "建蔽率", "容積率", "許容建蔽率", "許容容積率",
    "規定建蔽率1", "規定建蔽率2", "規定建蔽率3", "規定建蔽率4",
    "規定容積率1", "規定容積率2", "規定容積率3", "規定容積率4",
    "前面道路 幅員", "前面道路 敷地と接している部分の長さ",
]

_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?|-?\.\d+")


# ── 小道具 ─────────────────────────────────────────────────────────

def parse_number(value):
    """"864.94㎡" や "1,723.81" から数値を取り出す。取れなければ None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    m = _NUM_RE.search(str(value))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_percent(value):
    """"60%" / "60" / "0.6" → 60.0（％値）。"""
    n = parse_number(value)
    if n is None:
        return None
    if "%" not in str(value) and abs(n) <= 1.0:
        return n * 100.0
    return n


def fmt_area(v):
    return "---" if v is None else f"{v:,.2f}㎡"


def fmt_percent(v):
    return "---" if v is None else f"{v:.2f}%"


def fmt_len(v):
    return "---" if v is None else f"{v:,.3f}m"


def polygon_area(xs, ys):
    """多角形の面積（座標の向きによらず正）。"""
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    s = sum(xs[i] * ys[(i + 1) % n] - xs[(i + 1) % n] * ys[i] for i in range(n))
    return abs(s) / 2.0


def _arr(params, name):
    v = params.get(name)
    return v if isinstance(v, list) else []


def _pick(values, one_based):
    """GDL の 1 始まり番号で配列から取り出す（範囲外は None）。"""
    try:
        i = int(one_based) - 1
    except (TypeError, ValueError):
        return None
    return values[i] if 0 <= i < len(values) and values[i] not in ("", None) else None


def _vertices(params):
    xs, ys = _arr(params, "vertexXArray"), _arr(params, "vertexYArray")
    n = params.get("numVertex")
    n = int(n) if isinstance(n, (int, float)) and n > 0 else min(len(xs), len(ys))
    xs = [float(x) for x in xs[:n] if isinstance(x, (int, float))]
    ys = [float(y) for y in ys[:n] if isinstance(y, (int, float))]
    return xs, ys


def _edge_lengths(xs, ys, closed):
    n = min(len(xs), len(ys))
    count = n if closed else n - 1
    return [math.hypot(xs[(i + 1) % n] - xs[i], ys[(i + 1) % n] - ys[i]) for i in range(max(count, 0))]


# ── 組み立て ───────────────────────────────────────────────────────

def _build_site(el):
    p = el.get("params", {})
    xs, ys = _vertices(p)
    edges = _edge_lengths(xs, ys, closed=True)
    given = _arr(p, "lengthBoundary")
    types = _arr(p, "iBoundaryTypeArray")
    type_names = _arr(p, "strBoundaryType")
    boundaries = []
    for i, computed in enumerate(edges):
        length = parse_number(given[i]) if i < len(given) else None
        boundaries.append({
            "no": i + 1,
            "length": length if length is not None else computed,
            "type": _pick(type_names, types[i]) if i < len(types) else None,
        })
    categories = _arr(p, "strSiteCategory")
    n_area = p.get("numAreaBoundary")
    n_districts = min(4, (int(n_area) if isinstance(n_area, (int, float)) else 0) + 1)
    districts = [{"no": k, "name": _pick(categories, p.get(f"iSiteCategory{k}"))}
                 for k in range(1, n_districts + 1)]
    return {
        "guid": el.get("guid", ""),
        "area": polygon_area(xs, ys),
        "perimeter": sum(b["length"] for b in boundaries),
        "boundaries": boundaries,
        "districts": districts,
        "vertices": list(zip(xs, ys)),
    }


def _build_road(el):
    p = el.get("params", {})
    xs, ys = _vertices(p)
    return {
        "guid": el.get("guid", ""),
        "name": p.get("strRoadName") or "",
        "type": _pick(_arr(p, "strRoadType"), p.get("iRoadType")),
        "property": _pick(_arr(p, "strRoadProperty"), p.get("iRoadProperty")),
        "boundaryType": el.get("descs", {}).get("iBoundaryType"),
        "length": sum(_edge_lengths(xs, ys, closed=False)),
    }


def is_fire_element(el):
    return el.get("category") == "fire" or any(k in str(el.get("libPart", "")) for k in FIRE_KEYWORDS)


def fmt_param(value):
    """GDL パラメータ値を 1 セル分の文字列に（配列は先頭 DETAIL_ARRAY_MAX 個）。"""
    if isinstance(value, list):
        head = ", ".join(fmt_param(v) for v in value[:DETAIL_ARRAY_MAX])
        more = f" …（全 {len(value)} 個）" if len(value) > DETAIL_ARRAY_MAX else ""
        return f"[{head}]{more}"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float):
        return f"{value:.6g}"
    return "" if value is None else str(value)


def _fire_type(el):
    """延焼ラインの種類（例: 隣棟間延焼ライン(両側)）。名前に Type を含むパラメータの表示値。"""
    descs, params = el.get("descs", {}), el.get("params", {})
    for k, v in descs.items():
        if "type" in k.lower() and v:
            return str(v)
    for k, v in params.items():
        if "type" in k.lower() and isinstance(v, str) and v:
            return v
    return None


def _build_fire(el):
    p = el.get("params", {})
    if el.get("kind") == "line":                   # 線・ポリライン・円弧で描かれたもの
        length, points = el.get("length"), el.get("points")
    else:                                         # GDL オブジェクト: 頂点配列があれば折れ線の長さ
        xs, ys = _vertices(p)
        length = sum(_edge_lengths(xs, ys, closed=False)) if len(xs) >= 2 else None
        points = len(xs) or None
    return {
        "guid":   el.get("guid", ""),
        "name":   el.get("libPart", "") or "---",
        "story":  el.get("story") or str(el.get("floor", "")),
        "layer":  el.get("layer", ""),
        "type":   _fire_type(el),
        "length": float(length) if isinstance(length, (int, float)) else None,
        "points": points,
        "params": [[k, fmt_param(v)] for k, v in p.items()],
    }


def _judge_row(label, allowed, planned, is_ratio):
    """パレットの「項目 / 許容 / 計画 / 判定」1 行。比率は適合判定、面積は差分を出す。"""
    fmt = fmt_percent if is_ratio else fmt_area
    if allowed is None or planned is None:
        judge = "---"
    elif is_ratio:
        judge = "適合" if planned <= allowed + 1e-9 else "不適合"
    else:
        judge = fmt_area(planned - allowed)
    return [label, fmt(allowed), fmt(planned), judge]


def build_site_summary(payload):
    """get_site_summary の応答 → 画面・Excel 用の辞書。"""
    elements = payload.get("elements", [])
    project = {}
    for item in payload.get("projectInfo", []):
        desc = str(item.get("desc", "")).strip()
        if desc and desc not in project:
            project[desc] = str(item.get("value", ""))

    sites = [_build_site(e) for e in elements if e.get("libPart") == SITE_LIBPART]
    roads = [_build_road(e) for e in elements if e.get("libPart") == ROAD_LIBPART]
    fires = [_build_fire(e) for e in elements if is_fire_element(e)]

    computed_area = (sum(s["area"] for s in sites) if sites else None) or None   # 0 は未計算扱い
    site_area = parse_number(project.get("敷地面積の合計")) or computed_area
    bcr = parse_percent(project.get("許容建蔽率")) or parse_percent(project.get("規定建蔽率1"))
    far = parse_percent(project.get("許容容積率")) or parse_percent(project.get("規定容積率1"))
    building = parse_number(project.get("建築面積建物物全体 (合計)"))
    floor = parse_number(project.get("延べ面積 容積対象")) or parse_number(project.get("延べ面積建物物全体 (合計)"))

    def ratio(part):
        return part / site_area * 100.0 if part is not None and site_area else None

    def allowed_area(pct):
        return site_area * pct / 100.0 if pct is not None and site_area else None

    # 計画の比率は敷地マネージャーがプロジェクト情報に書いた値を優先（丸め前の面積で計算済みのため）
    planned_bcr = parse_percent(project.get("建蔽率")) or ratio(building)
    planned_far = parse_percent(project.get("容積率")) or ratio(floor)
    check = [
        _judge_row("建蔽率", bcr, planned_bcr, True),
        _judge_row("建築面積", allowed_area(bcr), building, False),
        _judge_row("容積率", far, planned_far, True),
        _judge_row("容積対象面積", allowed_area(far), floor, False),
    ]

    # 用途地域ごとの面積: 1 地域なら敷地面積、複数ならプロジェクト情報の敷地面積k
    district_rows = []
    for s in sites:
        multi = len(s["districts"]) > 1
        for d in s["districts"]:
            area = parse_number(project.get(f"敷地面積{d['no']}")) if multi else (site_area or s["area"] or None)
            district_rows.append([d["name"] or "---", fmt_area(area)])

    totals = [
        ["敷地面積の合計", fmt_area(site_area)],
        ["建築面積の合計", fmt_area(building)],
        ["延床面積の合計", fmt_area(floor)],
    ]
    return {
        "sites": sites,
        "roads": roads,
        "fire_lines": fires,
        "districts": district_rows,
        "totals": totals,
        "check": check,
        "computed_site_area": computed_area,
        "project": project,
        "project_rows": [[k, project[k]] for k in PROJECT_KEYS if k in project],
        "project_all": [[str(i.get("desc", "")), str(i.get("key", "")), str(i.get("value", ""))]
                        for i in payload.get("projectInfo", [])],
        "missing_totals": building is None or floor is None,
    }


def summary_sheets(summary):
    """Excel 出力用: [(シート名, 見出し, 行)]。"""
    boundary_rows = [
        [i + 1, b["no"], round(b["length"], 3), b["type"] or "---"]
        for i, s in enumerate(summary["sites"]) for b in s["boundaries"]
    ]
    road_rows = [[r["name"], r["type"] or "---", r["property"] or "---",
                  r["boundaryType"] or "---", round(r["length"], 3)] for r in summary["roads"]]
    fires = summary.get("fire_lines", [])
    return [
        ("集計結果", ["項目", "許容", "計画", "判定"], summary["check"]),
        ("合計", ["項目", "値"], summary["totals"]),
        ("用途地域", ["用途地域", "面積"], summary["districts"]),
        ("境界線", ["敷地", "No", "長さ(m)", "境界線タイプ"], boundary_rows),
        ("道路", ["道路名", "種別", "道路線", "境界線タイプ", "長さ(m)"], road_rows),
        ("延焼ライン", FIRE_HEADERS, fire_rows(fires, rounded=True)),
        ("延焼ライン詳細", FIRE_DETAIL_HEADERS, fire_detail_rows(fires)),
        ("プロジェクト情報", ["項目", "キー", "値"], summary["project_all"]),
    ]


FIRE_HEADERS        = ["No", "名前", "フロア", "レイヤ", "種類", "長さ(m)", "頂点数"]
FIRE_DETAIL_HEADERS = ["No", "名前", "パラメータ", "値"]


def fire_rows(fires, rounded=False):
    """延焼ラインの一覧（画面は単位付き、Excel は数値）。"""
    rows = []
    for i, f in enumerate(fires, start=1):
        length = f["length"]
        if rounded:
            length = round(length, 3) if length is not None else "---"
        else:
            length = fmt_len(length)
        rows.append([i, f["name"], f["story"] or "---", f["layer"] or "---", f["type"] or "---",
                     length, f["points"] if f["points"] is not None else "---"])
    return rows


def fire_detail_rows(fires):
    """延焼ライン（GDL オブジェクト）のパラメータ一覧。"""
    return [[i, f["name"], k, v] for i, f in enumerate(fires, start=1) for k, v in f["params"]]
