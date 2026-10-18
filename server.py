import os
import sys
import socket
import json
import threading
import traceback
import faulthandler
import uuid
from collections import deque
from pathlib import Path

# ── ロギングをインポート前に有効化（クラッシュ原因を必ず記録する）────────────
# BIM_LOG_DIR 環境変数でログ出力先を差し替え可能（テスト時に本番ログを汚さないため）
_LOG_DIR       = Path(os.environ.get("BIM_LOG_DIR") or Path(__file__).resolve().parent)
LOG_PATH       = _LOG_DIR / "server_log.txt"
FAULT_LOG_PATH = _LOG_DIR / "fault_log.txt"
# ウィンドウの大きさ・位置を次回起動時に復元するための保存先
SETTINGS_PATH  = _LOG_DIR / "pyqt_window.ini"


def append_log(message: str):
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fp:
            fp.write(f"[PY] {message}\n")
    except Exception:
        pass


append_log("--- Python startup begin ---")
append_log(f"Python {sys.version}")
append_log(f"Executable: {sys.executable}")
append_log(f"Path[0]: {sys.path[0] if sys.path else '(empty)'}")

# ── PyQt6 インポート（エラーを必ずログに記録）────────────────────────────────
try:
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QTableView, QVBoxLayout,
        QWidget, QHBoxLayout, QPushButton, QGroupBox, QLabel,
        QTreeWidget, QTreeWidgetItem, QSplitter, QMessageBox, QHeaderView,
        QTreeWidgetItemIterator, QMenu, QInputDialog, QAbstractItemView,
        QStyledItemDelegate, QComboBox, QDialog, QTableWidget, QTableWidgetItem,
        QDialogButtonBox, QFrame, QCheckBox, QSlider,
        QFileDialog, QLineEdit, QTabWidget,
    )
    append_log("PyQt6.QtWidgets OK")
except Exception as _e:
    append_log(f"IMPORT FATAL QtWidgets: {_e}\n{traceback.format_exc()}")
    sys.exit(1)

try:
    from PyQt6.QtCore import (
        Qt, QAbstractTableModel, pyqtSignal, QObject,
        QSortFilterProxyModel, QItemSelection, QItemSelectionModel, QTimer,
        QSettings, QEvent,
    )
    append_log("PyQt6.QtCore OK")
except Exception as _e:
    append_log(f"IMPORT FATAL QtCore: {_e}\n{traceback.format_exc()}")
    sys.exit(1)

try:
    # QKeySequence / QShortcut は PyQt5/6 いずれも QtGui に属する
    from PyQt6.QtGui import QColor, QAction, QFont, QKeySequence, QPalette
    append_log("PyQt6.QtGui OK")
except Exception as _e:
    append_log(f"IMPORT FATAL QtGui: {_e}\n{traceback.format_exc()}")
    sys.exit(1)

# QShortcut は QtGui にある（なければキーボードショートカットを無効化）
QShortcut = None
for _mod in ("PyQt6.QtGui", "PyQt6.QtWidgets"):
    try:
        import importlib as _il
        QShortcut = getattr(_il.import_module(_mod), "QShortcut")
        append_log(f"QShortcut found in {_mod}")
        break
    except Exception:
        pass
if QShortcut is None:
    append_log("QShortcut not available — keyboard shortcuts disabled")

try:
    from PyQt6.QtGui import QUndoStack
    append_log("QUndoStack from QtGui OK")
except ImportError:
    try:
        from PyQt6.QtWidgets import QUndoStack
        append_log("QUndoStack from QtWidgets OK (fallback)")
    except ImportError as _e:
        append_log(f"IMPORT FATAL QUndoStack: {_e}")
        sys.exit(1)

try:
    from db_manager import DbManager
    append_log("db_manager OK")
except Exception as _e:
    append_log(f"IMPORT FATAL db_manager: {_e}\n{traceback.format_exc()}")
    sys.exit(1)

try:
    from undo_commands import EditValueCommand
    append_log("undo_commands OK")
except Exception as _e:
    append_log(f"IMPORT FATAL undo_commands: {_e}\n{traceback.format_exc()}")
    sys.exit(1)

try:
    import table_tools
    import site_summary
    import schedules
    append_log("table_tools OK")
except Exception as _e:
    append_log(f"IMPORT FATAL table_tools (openpyxl 未インストール?): {_e}\n{traceback.format_exc()}")
    sys.exit(1)

append_log("--- All imports OK ---")


def _install_exception_hooks():
    fault_fp = FAULT_LOG_PATH.open("a", encoding="utf-8")
    faulthandler.enable(file=fault_fp)

    def _excepthook(exc_type, exc_value, exc_tb):
        msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        append_log(f"[UNCAUGHT EXCEPTION]\n{msg}")
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _excepthook

    def _thread_excepthook(args):
        msg = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        append_log(f"[THREAD EXCEPTION] thread={args.thread}\n{msg}")
    threading.excepthook = _thread_excepthook


# プロパティ設定の表示モード
PROP_MODE_SIMPLE = "簡易版（基本情報＋作成したプロパティ）"
PROP_MODE_DETAIL = "詳細版（全プロパティ）"
SIMPLE_GROUP     = "基本情報"
# 簡易版「基本情報」に並べる項目（この順で表示し、初期状態でチェック済み）
SIMPLE_KEYS = [
    "builtin:element_id", "elem:type", "elem:layer", "elem:libPart",
    "elem:W", "elem:L", "elem:H", "elem:elevation", "elem:story", "elem:classification",
]
# 入力元（自動計算 W/L/H/レベル の元になる、編集できる値）。基本情報の自動計算の後ろに並べる。
# 全要素タイプ共通の列で、その要素タイプに無い項目のセルは "(対象外)"（編集不可）。
INPUT_KEYS = [
    "elem:in:width", "elem:in:depth", "elem:in:height", "elem:in:length",
    "elem:in:baseOffset", "elem:in:topLink", "elem:in:topOffset", "elem:in:sill",
]
SIMPLE_KEYS = SIMPLE_KEYS + INPUT_KEYS

# 要素タイプのキー → 条件設定ツリーの表示名（無いものはキーをそのまま表示）
ELEMENT_TYPE_LABELS = {
    "SiteManager": "敷地マネージャー",
}


def normalize_label(value):
    return str(value or "").strip().lower()


def is_element_id_property(prop_def, prop_guid=""):
    name  = normalize_label(prop_def.get("name", ""))
    group = normalize_label(prop_def.get("group", ""))
    guid  = normalize_label(prop_guid)
    if guid in {"builtin:id", "builtin:elementid", "builtin:element_id"}:
        return True
    id_like    = (name == "id" or name == "element id" or name == "要素id"
                  or "element id" in name or "要素id" in name)
    group_like = (group == "id and categories" or group == "idとカテゴリ"
                  or ("categor" in group and "id" in group)
                  or ("カテゴリ" in group and "id" in group))
    return id_like and group_like


def get_group_type(p_def, p_guid):
    # 要素の形状・設定（elem:）は要素そのものの変更 → modiStamp 不一致なら中止（厳密）
    if p_guid.startswith("elem:"):
        return "parameter"
    if p_guid.startswith("builtin:"):
        if "element_id"       in p_guid: return "element_id"
        if "Renovation"       in p_guid: return "parameter"
        if "StructuralFunction" in p_guid or "Position" in p_guid: return "category"
        if "Layer"            in p_guid: return "element"
        return "parameter"
    g_name = p_def.get("group", "")
    if g_name in ["IDとカテゴリ", "ID and Categories", "分類", "Classification"]: return "classification"
    if g_name in ["レイヤ", "Layer"]:                                               return "element"
    if g_name in ["一般パラメータ", "General Parameters", "形状", "Geometry", "位置"]: return "parameter"
    if g_name in ["面と材質", "Surfaces", "材質", "Material", "ビルディングマテリアル"]:  return "attribute"
    return "property"


# ─────────────────────────────────────────────────────────────────────
# ExcelFilterProxyModel
# ─────────────────────────────────────────────────────────────────────

class ExcelFilterProxyModel(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self._filters         = {}
        self._numeric_filters = {}
        self._search_text     = ""

    def set_search_text(self, text):
        """全列を対象にしたキーワード検索（部分一致・大文字小文字無視）。"""
        self._search_text = (text or "").strip().casefold()
        self.invalidateFilter()

    def lessThan(self, left, right):
        """Excel と同様、数値は数値として比較し、空欄 / "---" は昇順・降順とも末尾に置く。"""
        kl = table_tools.sort_key(self.sourceModel().data(left))
        kr = table_tools.sort_key(self.sourceModel().data(right))
        if self.sortOrder() == Qt.SortOrder.DescendingOrder:
            # 降順では Qt が結果を反転するため、空欄グループを最小にして末尾へ
            kl = (-1,) + kl[1:] if kl[0] == 2 else kl
            kr = (-1,) + kr[1:] if kr[0] == 2 else kr
        return kl < kr

    def set_value_filter(self, col, allowed_values):
        self._filters[col] = set(allowed_values)
        self.invalidateFilter()

    def set_numeric_filter(self, col, filter_data):
        self._numeric_filters[col] = filter_data
        self.invalidateFilter()

    def clear_numeric_filter(self, col):
        if col in self._numeric_filters:
            del self._numeric_filters[col]
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        for col, allowed in self._filters.items():
            idx = self.sourceModel().index(source_row, col, source_parent)
            if str(self.sourceModel().data(idx)) not in allowed:
                return False
        for col, data in self._numeric_filters.items():
            idx = self.sourceModel().index(source_row, col, source_parent)
            try:
                val   = float(str(self.sourceModel().data(idx)))
                ftype = data[0]
                if ftype == "gte"   and not (val >= data[1]):             return False
                if ftype == "lte"   and not (val <= data[1]):             return False
                if ftype == "eq"    and not (val == data[1]):             return False
                if ftype == "range" and not (data[1] <= val <= data[2]): return False
            except:
                return False
        if self._search_text:
            model = self.sourceModel()
            return any(
                self._search_text in str(model.data(model.index(source_row, c, source_parent))).casefold()
                for c in range(model.columnCount())
            )
        return True


# ─────────────────────────────────────────────────────────────────────
# PropertyTableModel
# ─────────────────────────────────────────────────────────────────────

class PropertyTableModel(QAbstractTableModel):
    STAMP_OK      = "ok"
    STAMP_STALE   = "stale"
    STAMP_UNKNOWN = "unknown"

    def __init__(self):
        super().__init__()
        self._elements             = []
        self._properties           = []
        self._values               = {}
        self._original_values      = {}
        self._stamps               = {}
        self._conflicts            = {}
        self._stamp_flags          = {}
        self._value_conflict_cells = {}
        self._value_conflict_guids = set()
        self._change_status        = {}
        self._selected_guid        = None
        # DB / Undo 連携（MainWindow.__init__ から注入される）
        self._undo_stack           = None
        self._db                   = None
        self._session_id           = None
        self._current_condition_id = None

    def set_data(self, elements, properties, values, stamps=None):
        self.beginResetModel()
        self._elements        = elements
        self._properties      = properties
        self._values          = values.copy()
        self._original_values = values.copy()
        self._conflicts       = {}
        self._stamps          = stamps or {}
        self._stamp_flags     = {
            e["guid"]: (self.STAMP_UNKNOWN if not self._stamps.get(e["guid"]) else self.STAMP_OK)
            for e in elements
        }
        self._value_conflict_cells.clear()
        self._value_conflict_guids.clear()
        self._change_status.clear()
        self._selected_guid = None
        self.endResetModel()

    def rowCount(self, parent=None):    return len(self._elements)
    def columnCount(self, parent=None): return len(self._properties) + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._elements):
            return None
        row, col = index.row(), index.column()
        guid = self._elements[row]["guid"]

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if col == 0:
                return guid[:8]
            prop_guid = self._properties[col - 1]["guid"]
            return str(self._values.get((guid, prop_guid), ""))

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 0:
                return QColor("darkRed") if guid in self._conflicts else QColor("black")
            prop_guid = self._properties[col - 1]["guid"]
            if str(self._values.get((guid, prop_guid))) != str(self._original_values.get((guid, prop_guid))):
                return QColor("red")
            if guid in self._conflicts:
                return QColor("darkRed")
            # 編集不可プロパティ（PropertyDefinition.editable=False）と対象外セルはグレー表示
            if not self.is_cell_editable(guid, self._properties[col - 1]):
                return QColor("gray")
            return QColor("black")

        if role == Qt.ItemDataRole.BackgroundRole:
            if guid == self._selected_guid:
                return QColor("#AED6F1")
            conf = self._conflicts.get(guid)
            if conf == "conflict": return QColor("#FFCCCC")
            if conf == "skipped":  return QColor("#FFE5CC")
            if col > 0:
                prop_guid = self._properties[col - 1]["guid"]
                if (guid, prop_guid) in self._value_conflict_cells:
                    return QColor("#FFB347")
            elif guid in self._value_conflict_guids:
                return QColor("#FFD0A0")
            cs   = self._change_status.get(guid, 0)
            if cs == 2: return QColor("#C8F0C8")
            if cs == 1: return QColor("#FFD0D0")
            flag = self._stamp_flags.get(guid, self.STAMP_OK)
            if flag == self.STAMP_STALE:   return QColor("#FFFACD")
            if flag == self.STAMP_UNKNOWN: return QColor("#F0F0F0")
            return None

        if role == Qt.ItemDataRole.FontRole:
            font = QFont()
            if guid in self._conflicts:
                font.setBold(True)
            return font

        if role == Qt.ItemDataRole.ToolTipRole:
            if col > 0:
                prop_guid = self._properties[col - 1]["guid"]
                cf = self._value_conflict_cells.get((guid, prop_guid))
                if cf:
                    return (f"⚠ 値競合フラグ（スキップ済み）\n"
                            f"  取得時       : {cf['orig_val']}\n"
                            f"  ArchiCAD現在 : {cf['ac_val']}\n"
                            f"  あなたの編集 : {cf['user_val']}")
                return None
            parts = []
            conf = self._conflicts.get(guid)
            if conf:
                parts.append(f"競合: {conf}")
            if guid in self._value_conflict_guids:
                parts.append("⚠ 値競合フラグあり（セルにカーソルで詳細）")
            cs = self._change_status.get(guid, 0)
            if cs in {1, 2}:
                parts.append(f"ChangeStatus: {['', '変更済 (赤)', '確認済 (緑)'][cs]}")
            flag  = self._stamp_flags.get(guid, self.STAMP_OK)
            stamp = self._stamps.get(guid, 0)
            if flag == self.STAMP_STALE:     parts.append(f"⚠ ArchiCAD側で変更済み (stamp:{stamp})")
            elif flag == self.STAMP_UNKNOWN: parts.append("stamp未取得")
            else:                            parts.append(f"stamp:{stamp}")
            return "\n".join(parts) if parts else None

        return None

    def set_value_direct(self, element_guid: str, prop_guid: str, value: str):
        """Undo/Redo コマンドから直接呼ばれる値セット。dataChanged を emit する。"""
        self._values[(element_guid, prop_guid)] = value
        if (element_guid, prop_guid) in self._value_conflict_cells:
            del self._value_conflict_cells[(element_guid, prop_guid)]
            if not any(k[0] == element_guid for k in self._value_conflict_cells):
                self._value_conflict_guids.discard(element_guid)
        for row, elem in enumerate(self._elements):
            if elem["guid"] == element_guid:
                for col_i, prop in enumerate(self._properties):
                    if prop["guid"] == prop_guid:
                        idx = self.index(row, col_i + 1)
                        self.dataChanged.emit(idx, idx, [
                            Qt.ItemDataRole.DisplayRole,
                            Qt.ItemDataRole.EditRole,
                            Qt.ItemDataRole.ForegroundRole,
                        ])
                        return

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        append_log(f"[SETDATA] row={index.row()} col={index.column()} role={role} valid={index.isValid()}")
        if not (index.isValid() and role == Qt.ItemDataRole.EditRole):
            return False
        row, col = index.row(), index.column()
        if col == 0:
            return False
        g       = self._elements[row]["guid"]
        pg      = self._properties[col - 1]["guid"]
        old_val = str(self._values.get((g, pg), ""))
        prop    = self._properties[col - 1]
        ok, reason = table_tools.validate_value(prop, value, old_val)
        if not ok:
            append_log(f"[SETDATA] rejected: {reason}")
            return False
        new_val = table_tools.normalize_value(prop, value)   # 入力元: 600mm → 0.600 など
        append_log(f"[SETDATA] g={g[:8]} pg={pg[:8]} old={old_val!r} new={new_val!r} stack={self._undo_stack is not None}")
        if new_val == old_val:
            append_log("[SETDATA] no change → skip")
            return False
        if self._undo_stack is not None and self._db is not None and self._session_id is not None:
            cmd = EditValueCommand(
                self, self._db, self._session_id,
                g, pg, old_val, new_val, self._current_condition_id,
            )
            self._undo_stack.push(cmd)   # redo() が自動呼ばれ set_value_direct + DB 保存
            append_log(f"[SETDATA] pushed to undo_stack canUndo={self._undo_stack.canUndo()}")
        else:
            append_log(f"[SETDATA] fallback direct (stack={self._undo_stack} db={self._db} sid={self._session_id})")
            self.set_value_direct(g, pg, new_val)
        return True

    def refresh_view(self):
        if self._elements and self._properties:
            tl = self.index(0, 0)
            br = self.index(len(self._elements) - 1, len(self._properties))
            self.dataChanged.emit(tl, br, [
                Qt.ItemDataRole.BackgroundRole,
                Qt.ItemDataRole.ForegroundRole,
                Qt.ItemDataRole.FontRole,
                Qt.ItemDataRole.ToolTipRole,
            ])

    def update_change_status(self, guids, value):
        guid_set = set(guids)
        for g in guids:
            if value == 0: self._change_status.pop(g, None)
            else:          self._change_status[g] = value
        for row, elem in enumerate(self._elements):
            if elem["guid"] in guid_set:
                tl = self.index(row, 0)
                br = self.index(row, len(self._properties))
                self.dataChanged.emit(tl, br, [Qt.ItemDataRole.BackgroundRole])

    def set_selected_guid(self, guid):
        prev, self._selected_guid = self._selected_guid, guid
        for row, elem in enumerate(self._elements):
            if elem["guid"] in (prev, guid):
                tl = self.index(row, 0)
                br = self.index(row, len(self._properties))
                self.dataChanged.emit(tl, br, [Qt.ItemDataRole.BackgroundRole])

    def flags(self, index):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable  # コピー用に ID 列も選択可
        if index.column() == 0 or index.column() > len(self._properties):
            return base
        guid = self._elements[index.row()]["guid"] if index.row() < len(self._elements) else None
        if not self.is_cell_editable(guid, self._properties[index.column() - 1]):
            return base
        return base | Qt.ItemFlag.ItemIsEditable

    def is_cell_editable(self, element_guid, prop):
        """編集可能なプロパティで、かつその要素で対象外（入力元が無いタイプ）でないセル。"""
        if not table_tools.is_editable(prop):
            return False
        return str(self._values.get((element_guid, prop["guid"]), "")) != table_tools.NOT_APPLICABLE

    def merge_values(self, values, stamps):
        """反映後に取り直した値で、その要素の行だけを更新する（自動計算の結果を最新にする）。
        取り直した値は「取得時の値」にもなる（＝未反映の変更なし）。"""
        for key, val in values.items():
            self._values[key] = val
            self._original_values[key] = val
            self._value_conflict_cells.pop(key, None)
        for g, st in stamps.items():
            if st:
                self._stamps[g] = st
                self._stamp_flags[g] = self.STAMP_OK
        guids = set(stamps) | {g for g, _ in values}
        for row, elem in enumerate(self._elements):
            if elem["guid"] in guids:
                self.dataChanged.emit(self.index(row, 0), self.index(row, len(self._properties)))

    def prop_at(self, col):
        """表の列番号 → プロパティ定義（ID 列は None）。"""
        return self._properties[col - 1] if 0 < col <= len(self._properties) else None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return "ID" if section == 0 else self._properties[section - 1]["name"]
        return None


# ─────────────────────────────────────────────────────────────────────
# TCP 通信
# ─────────────────────────────────────────────────────────────────────

class CommunicationSignals(QObject):
    config_received              = pyqtSignal(dict)
    elements_received            = pyqtSignal(dict)
    definitions_received         = pyqtSignal(dict)
    values_received              = pyqtSignal(dict)
    sync_complete                = pyqtSignal(dict)
    flag_result_received         = pyqtSignal(dict)
    bim_override_result_received = pyqtSignal(dict)
    change_status_result_received= pyqtSignal(dict)
    current_floor_received       = pyqtSignal(dict)
    selection_changed            = pyqtSignal(dict)
    site_summary_received        = pyqtSignal(dict)
    schedule_resolved_received   = pyqtSignal(dict)
    project_info_result_received = pyqtSignal(dict)
    show_window                  = pyqtSignal()
    error_occurred               = pyqtSignal(str)


class TcpServerThread(threading.Thread):
    def __init__(self, signals):
        super().__init__(daemon=True)
        self.signals     = signals
        self._stop_event = threading.Event()

    def stop(self): self._stop_event.set()

    def run(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", 5000))
                s.listen()
                s.settimeout(1.0)
                append_log("Server started (5000)")
            except Exception as e:
                self.signals.error_occurred.emit(f"Server Bind Error: {e}")
                return
            while not self._stop_event.is_set():
                try:
                    conn, addr = s.accept()
                    append_log(f"[CONNECT] {addr}")
                    threading.Thread(target=self.handle_client, args=(conn,), daemon=True).start()
                except socket.timeout:
                    continue
                except Exception as e:
                    if not self._stop_event.is_set():
                        append_log(f"Accept Error: {e}")

    def handle_client(self, conn):
        with conn:
            conn.settimeout(10.0)
            raw_bytes = b""
            try:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    raw_bytes += chunk
            except socket.timeout:
                pass
            except Exception as e:
                append_log(f"[RECV ERROR] {e}")
                if not raw_bytes:
                    return
            if not raw_bytes:
                return

            for raw in raw_bytes.decode("utf-8", errors="replace").split("\n"):
                raw = raw.strip()
                if not raw:
                    continue
                append_log(f"[RECV RAW] {raw[:1000]}")
                try:
                    payload = json.loads(raw)
                    if "data" in payload and "type" not in payload:
                        payload = payload["data"]
                    t = payload.get("type")
                    if not t:
                        if "results"  in payload: t = "sync_complete"
                        elif "elements" in payload: t = "elements"
                    append_log(f"[TYPE] {t}")

                    if   t == "project_config":      self.signals.config_received.emit(payload);              self.signals.show_window.emit()
                    elif t == "elements":             self.signals.elements_received.emit(payload)
                    elif t == "property_definitions": self.signals.definitions_received.emit(payload)
                    elif t == "property_values":      self.signals.values_received.emit(payload)
                    elif t in ("sync_complete", "sync_result", "apply_result", "result"):
                        append_log("=== SYNC SIGNAL EMIT ===")
                        self.signals.sync_complete.emit(payload)
                    elif t == "flag_result":           self.signals.flag_result_received.emit(payload)
                    elif t == "bim_override_result":   self.signals.bim_override_result_received.emit(payload)
                    elif t == "change_status_result":  self.signals.change_status_result_received.emit(payload)
                    elif t == "selection_changed":     self.signals.selection_changed.emit(payload)
                    elif t == "current_floor":        self.signals.current_floor_received.emit(payload)
                    elif t == "site_summary":         self.signals.site_summary_received.emit(payload)
                    elif t == "schedule_resolved":    self.signals.schedule_resolved_received.emit(payload)
                    elif t == "project_info_result":  self.signals.project_info_result_received.emit(payload)
                    else:
                        append_log(f"[UNKNOWN TYPE] {t}")
                except Exception as e:
                    append_log(f"[JSON ERROR] {e}")

            try:
                conn.sendall(b"OK\n")
            except:
                pass


# ─────────────────────────────────────────────────────────────────────
# デリゲート
# ─────────────────────────────────────────────────────────────────────

class EnumComboBoxDelegate(QStyledItemDelegate):
    def __init__(self, enums, parent=None):
        super().__init__(parent)
        self._enums = enums

    def createEditor(self, parent, option, index):
        cb = QComboBox(parent)
        cb.addItems(self._enums)
        return cb

    def setEditorData(self, editor, index):
        val = index.data(Qt.ItemDataRole.EditRole) or ""
        idx = editor.findText(val)
        if idx >= 0:
            editor.setCurrentIndex(idx)

    def setModelData(self, editor, model, index):
        append_log(f"[DELEGATE Enum] setModelData col={index.column()} val={editor.currentText()!r}")
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)


class PlaceholderClearDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        append_log(f"[DELEGATE Plain] createEditor col={index.column()}")
        return super().createEditor(parent, option, index)

    def setEditorData(self, editor, index):
        val = index.data(Qt.ItemDataRole.EditRole) or ""
        if hasattr(editor, "setText"):
            editor.setText(val)

    def setModelData(self, editor, model, index):
        val = editor.text() if hasattr(editor, "text") else None
        append_log(f"[DELEGATE Plain] setModelData col={index.column()} val={val!r}")
        if val is not None:
            model.setData(index, val, Qt.ItemDataRole.EditRole)
        else:
            super().setModelData(editor, model, index)


# ─────────────────────────────────────────────────────────────────────
# DiffDialog  ⑥ 差分表示
# ─────────────────────────────────────────────────────────────────────

class DiffDialog(QDialog):
    """選択要素の編集履歴（差分）を一覧表示するダイアログ。

    ソース別カラーコーディング:
      user  → 青背景（ユーザー直接編集）
      undo  → グレー背景（取消操作）
      redo  → 緑背景（やり直し操作）
      archicad → 黄背景（ArchiCAD 側の変更）
    """

    _SOURCE_LABELS = {
        "user":     "ユーザー編集",
        "undo":     "取消(Undo)",
        "redo":     "やり直し(Redo)",
        "archicad": "ArchiCAD",
    }
    _SOURCE_BG = {
        "user":     QColor("#DBEEFF"),
        "undo":     QColor("#EEEEEE"),
        "redo":     QColor("#D6F5D6"),
        "archicad": QColor("#FFFACD"),
    }

    def __init__(self, history: list, element_guid: str = "",
                 db=None, parent=None):
        super().__init__(parent)
        self._db            = db
        self._element_guid  = element_guid
        self._all_history   = list(history)   # 降順（最新が先頭）
        self._ascending     = False           # 表示順: False=新→古, True=古→新

        short = f"[{element_guid[:8]}]" if element_guid else "[全件]"
        self.setWindowTitle(f"編集履歴・差分  {short}")
        self.resize(1000, 560)

        layout = QVBoxLayout(self)
        layout.setSpacing(4)

        # ── ヘッダ行（件数 + ソート切替）──────────────────────────────
        hdr_row = QWidget()
        hdr_lay = QHBoxLayout(hdr_row)
        hdr_lay.setContentsMargins(0, 0, 0, 0)
        total = len(self._all_history)
        self._count_label = QLabel(f"履歴: {total} 件")
        self._count_label.setStyleSheet("font-weight: bold;")
        self._chk_asc = QCheckBox("古い順に表示")
        self._chk_asc.setChecked(self._ascending)
        self._chk_asc.stateChanged.connect(self._on_sort_changed)
        hdr_lay.addWidget(self._count_label)
        hdr_lay.addStretch()
        hdr_lay.addWidget(self._chk_asc)
        layout.addWidget(hdr_row)

        # ── テーブル ────────────────────────────────────────────────
        self._tbl = QTableWidget(0, 5, self)
        self._tbl.setHorizontalHeaderLabels(["日時", "プロパティ名", "変更前", "変更後", "操作"])
        self._tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tbl.setAlternatingRowColors(False)
        self._tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hdr = self._tbl.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._tbl)

        # ── 凡例 ────────────────────────────────────────────────────
        legend_row = QWidget()
        leg_lay = QHBoxLayout(legend_row)
        leg_lay.setContentsMargins(0, 0, 0, 0)
        for src, label in self._SOURCE_LABELS.items():
            lbl = QLabel(f"  {label}  ")
            bg  = self._SOURCE_BG.get(src, QColor("white"))
            lbl.setStyleSheet(
                f"background:{bg.name()}; border:1px solid #aaa;"
                f" padding:1px 4px; border-radius:3px;"
            )
            leg_lay.addWidget(lbl)
        leg_lay.addStretch()
        layout.addWidget(legend_row)

        # ── ボタン ──────────────────────────────────────────────────
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn_box.rejected.connect(self.accept)
        layout.addWidget(btn_box)

        self._refresh_table()

    # ── ソート切替 ───────────────────────────────────────────────────

    def _on_sort_changed(self, state):
        self._ascending = (state == Qt.CheckState.Checked.value)
        self._refresh_table()

    # ── テーブル再描画 ───────────────────────────────────────────────

    def _refresh_table(self):
        rows = list(self._all_history)
        if self._ascending:
            rows = list(reversed(rows))

        self._tbl.setRowCount(len(rows))
        if not rows:
            self._tbl.setRowCount(1)
            self._tbl.setItem(0, 0, QTableWidgetItem("編集履歴はありません。"))
            return

        for row_i, h in enumerate(rows):
            ts        = h.get("timestamp", "")[:19].replace("T", " ")
            prop_name = h.get("prop_name") or h.get("property_guid", "")[:8]
            old_v     = str(h.get("old_value", ""))
            new_v     = str(h.get("new_value", ""))
            src_key   = h.get("source", "user")
            src_label = self._SOURCE_LABELS.get(src_key, src_key)
            row_bg    = self._SOURCE_BG.get(src_key)

            for col_i, val in enumerate([ts, prop_name, old_v, new_v, src_label]):
                item = QTableWidgetItem(str(val))
                item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
                if row_bg:
                    item.setBackground(row_bg)
                # 変更後セルの差分を赤字で強調
                if col_i == 3 and new_v != old_v:
                    item.setForeground(QColor("#C0392B"))
                    f = QFont(); f.setBold(True)
                    item.setFont(f)
                # undo 行の変更前はグレー文字
                if col_i == 2 and src_key == "undo":
                    item.setForeground(QColor("#888888"))
                self._tbl.setItem(row_i, col_i, item)

        # 最新行（降順時は先頭、昇順時は末尾）へスクロール
        scroll_row = len(rows) - 1 if self._ascending else 0
        self._tbl.scrollToItem(self._tbl.item(scroll_row, 0))


# ─────────────────────────────────────────────────────────────────────
# ReplaceDialog  Excel 風「検索と置換」
# ─────────────────────────────────────────────────────────────────────

class ReplaceDialog(QDialog):
    """モードレスの検索と置換ダイアログ。実処理は MainWindow 側（select_matches / replace_all）。"""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._win = window
        self.setWindowTitle("検索と置換")
        self.setModal(False)
        lay = QVBoxLayout(self)

        def row(label, widget):
            w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(label); lbl.setFixedWidth(70)
            h.addWidget(lbl); h.addWidget(widget, 1)
            lay.addWidget(w)

        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("検索する文字列")
        self.repl_edit = QLineEdit()
        self.repl_edit.setPlaceholderText("置換後の文字列（空欄で削除）")
        row("検索:", self.find_edit)
        row("置換後:", self.repl_edit)

        self.chk_case      = QCheckBox("大文字と小文字を区別")
        self.chk_whole     = QCheckBox("セル内容が完全に一致（検索が空なら空欄セル）")
        self.chk_regex     = QCheckBox("正規表現を使う（置換後で \\1 等を使用可）")
        self.chk_selection = QCheckBox("選択範囲のみ")
        for chk in (self.chk_case, self.chk_whole, self.chk_regex, self.chk_selection):
            lay.addWidget(chk)

        self.info_label = QLabel("")
        lay.addWidget(self.info_label)

        btns = QWidget(); bl = QHBoxLayout(btns); bl.setContentsMargins(0, 0, 0, 0)
        self.btn_find    = QPushButton("一致セルを選択")
        self.btn_replace = QPushButton("すべて置換")
        self.btn_close   = QPushButton("閉じる")
        self.btn_find.clicked.connect(self.on_find)
        self.btn_replace.clicked.connect(self.on_replace)
        self.btn_close.clicked.connect(self.close)
        bl.addStretch(); bl.addWidget(self.btn_find); bl.addWidget(self.btn_replace); bl.addWidget(self.btn_close)
        lay.addWidget(btns)
        self.resize(460, 0)

    def options(self):
        return {
            "find":           self.find_edit.text(),
            "replace":        self.repl_edit.text(),
            "match_case":     self.chk_case.isChecked(),
            "whole_cell":     self.chk_whole.isChecked(),
            "regex":          self.chk_regex.isChecked(),
            "selection_only": self.chk_selection.isChecked(),
        }

    def _activate_owner(self):
        panel = getattr(self, "_panel", None)
        if panel is not None and hasattr(self._win, "activate_panel"):
            self._win.activate_panel(panel)

    def on_find(self):
        self._activate_owner()
        n = self._win.select_matches(self.options())
        if n is not None:
            self.info_label.setText(f"一致: {n} セル")

    def on_replace(self):
        self._activate_owner()
        result = self._win.replace_all(self.options())
        if result is not None:
            changed, skipped, locked = result
            parts = [f"置換: {changed} セル"]
            if locked:  parts.append(f"編集不可で対象外: {locked}")
            if skipped: parts.append(f"検証でスキップ: {skipped}")
            self.info_label.setText(" / ".join(parts))


# ─────────────────────────────────────────────────────────────────────
# SiteSummaryDialog  敷地マネージャーのパレット相当の情報
# ─────────────────────────────────────────────────────────────────────

class SiteSummaryDialog(QDialog):
    """敷地情報（敷地・用途地域・道路・集計結果・プロジェクト情報）をまとめて表示する。"""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._win = window
        self._summary = None
        self.setWindowTitle("敷地情報（敷地マネージャー）")
        self.setModal(False)
        self.resize(760, 620)
        lay = QVBoxLayout(self)
        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)
        lay.addWidget(self.info_label)

        self.tabs = QTabWidget()
        self.tbl_check     = self._table(["項目", "許容", "計画", "判定"])
        self.tbl_totals    = self._table(["項目", "値"])
        self.tbl_districts = self._table(["用途地域", "面積"])
        self.tbl_bounds    = self._table(["敷地", "No", "長さ", "境界線タイプ"])
        self.tbl_roads     = self._table(["道路名", "種別", "道路線", "境界線タイプ", "長さ"])
        self.tbl_project   = self._table(["項目", "キー", "値"])

        summary_tab = QWidget(); sl = QVBoxLayout(summary_tab)
        for title, tbl in (("用途地域", self.tbl_districts), ("集計結果", self.tbl_totals),
                           ("許容・計画・判定", self.tbl_check)):
            lbl = QLabel(title); lbl.setStyleSheet("font-weight: bold;")
            sl.addWidget(lbl); sl.addWidget(tbl)
        self.tabs.addTab(summary_tab, "集計結果")
        self.tabs.addTab(self.tbl_bounds, "境界線")
        self.tabs.addTab(self.tbl_roads, "道路")
        self.tabs.addTab(self.tbl_project, "プロジェクト情報")
        lay.addWidget(self.tabs)

        btns = QWidget(); bl = QHBoxLayout(btns); bl.setContentsMargins(0, 0, 0, 0)
        self.btn_refresh = QPushButton("再取得")
        self.btn_export  = QPushButton("Excel出力")
        self.btn_import  = QPushButton("Excel取込（プロジェクト情報）")
        self.btn_import.setToolTip("出力した Excel の「プロジェクト情報」シートで編集した値を Archicad に書き込みます")
        self.btn_close   = QPushButton("閉じる")
        self.btn_refresh.clicked.connect(self._win.request_site_summary)
        self.btn_export.clicked.connect(self.export_excel)
        self.btn_import.clicked.connect(self.import_excel)
        self.btn_close.clicked.connect(self.close)
        bl.addStretch()
        for btn in (self.btn_refresh, self.btn_export, self.btn_import, self.btn_close):
            bl.addWidget(btn)
        lay.addWidget(btns)

    @staticmethod
    def _table(headers):
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        t.verticalHeader().setVisible(False)
        return t

    @staticmethod
    def _fill(table, rows):
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                item = QTableWidgetItem(str(v))
                if c > 0 and (str(v).endswith("㎡") or str(v).endswith("%") or str(v).endswith("m")):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if str(v) == "不適合":
                    item.setForeground(QColor("red"))
                table.setItem(r, c, item)

    def set_summary(self, summary):
        self._summary = summary
        self._fill(self.tbl_check, summary["check"])
        self._fill(self.tbl_totals, summary["totals"])
        self._fill(self.tbl_districts, summary["districts"])
        self._fill(self.tbl_bounds, [
            [i + 1, b["no"], site_summary.fmt_len(b["length"]), b["type"] or "---"]
            for i, s in enumerate(summary["sites"]) for b in s["boundaries"]])
        self._fill(self.tbl_roads, [
            [r["name"] or "---", r["type"] or "---", r["property"] or "---",
             r["boundaryType"] or "---", site_summary.fmt_len(r["length"])] for r in summary["roads"]])
        self._fill(self.tbl_project, summary["project_all"])
        notes = [f"敷地オブジェクト {len(summary['sites'])} 件 / 道路境界線 {len(summary['roads'])} 件"]
        if summary["computed_site_area"] is not None:
            notes.append(f"敷地形状から計算した面積: {site_summary.fmt_area(summary['computed_site_area'])}")
        if summary["missing_totals"]:
            notes.append("※ 建築面積・延床面積がプロジェクト情報にありません。"
                         "敷地マネージャーの［建築面積集計］［延床面積集計］を実行してから再取得してください。")
        self.info_label.setText("\n".join(notes))

    def export_excel(self):
        if not self._summary:
            return
        path = self._win.excel_save_path(self, "Excel出力", "敷地情報")
        if not path:
            return
        sheets = site_summary.summary_sheets(self._summary)
        try:
            table_tools.export_sheets(path, sheets)
            check = table_tools.read_sheet(path, sheets[-1][0])   # 書いたファイルを読み直して確認
        except Exception as e:
            append_log(f"[SITE EXPORT ERROR] {e}\n{traceback.format_exc()}")
            QMessageBox.warning(self, "Excel出力", self._win.excel_error_text(e, writing=True))
            return
        if len(check) - 1 != len(sheets[-1][2]):
            QMessageBox.warning(self, "Excel出力", f"書き出した内容を確認できませんでした。\n{path}")
            return
        self.info_label.setText(self.info_label.text() + f"\nExcel出力: {path}")
        QMessageBox.information(self, "Excel出力",
                                f"{len(sheets)} シートを書き出しました。\n{path}\n\n"
                                "「プロジェクト情報」シートの値を編集すると、［Excel取込］で Archicad に反映できます。")

    def import_excel(self):
        """「プロジェクト情報」シートの値のうち、現在と違うものを確認後に Archicad へ書き込む。"""
        if not self._summary:
            return
        path = self._win.excel_open_path(self, "Excel取込（プロジェクト情報）")
        if not path:
            return
        try:
            rows = table_tools.read_sheet(path, "プロジェクト情報")
        except Exception as e:
            append_log(f"[SITE IMPORT ERROR] {e}\n{traceback.format_exc()}")
            QMessageBox.warning(self, "Excel取込", self._win.excel_error_text(e, writing=False)
                                if isinstance(e, PermissionError) else f"読み込みに失敗しました:\n{e}")
            return
        current = {k: (d, v) for d, k, v in self._summary["project_all"]}
        changes, unknown = [], []
        for row in rows[1:]:
            if len(row) < 3 or not row[1]:
                continue
            desc, key, value = row[0], row[1], row[2]
            if key not in current:
                unknown.append(desc or key)
                continue
            if not table_tools.values_equal(current[key][1], value):
                changes.append({"key": key, "desc": current[key][0] or desc, "old": current[key][1], "new": value})
        note = f"\n\nプロジェクト情報に無い項目（無視）: {', '.join(unknown[:5])}" if unknown else ""
        if not changes:
            QMessageBox.information(self, "Excel取込", "プロジェクト情報に変更はありませんでした。" + note)
            return
        lines = "\n".join(f"  {c['desc']}: {c['old'] or '（空）'} → {c['new'] or '（空）'}" for c in changes[:15])
        more = f"\n  … 他 {len(changes) - 15} 件" if len(changes) > 15 else ""
        if QMessageBox.question(
                self, "プロジェクト情報の更新",
                f"Archicad のプロジェクト情報を {len(changes)} 件更新します（Archicad の取り消しで戻せます）。\n"
                f"{lines}{more}{note}\n\n更新しますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            self.info_label.setText(self.info_label.text() + "\nExcel取込: 中止しました")
            return
        self._win.set_project_info(changes)
        self.info_label.setText(self.info_label.text() + f"\nプロジェクト情報を {len(changes)} 件更新中...")


# ─────────────────────────────────────────────────────────────────────
# ScheduleListDialog  モデル内の連動一覧表を選んで開く
# ─────────────────────────────────────────────────────────────────────

class ScheduleListDialog(QDialog):
    """連動一覧表（要素一覧）の一覧。選んだ一覧表と同じ対象・列でデータウィンドウを開く。"""

    HEADERS = ["No", "一覧表", "対象要素", "列", "条件", "状態"]

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._win = window
        self._items = []
        self.setWindowTitle("一覧表（連動一覧表）から取得")
        self.setModal(False)
        self.resize(820, 560)
        lay = QVBoxLayout(self)
        self.folder_label = QLabel("")
        self.folder_label.setWordWrap(True)
        lay.addWidget(self.folder_label)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("一覧表名で絞り込み")
        self.filter_edit.textChanged.connect(self._apply_filter)
        lay.addWidget(self.filter_edit)
        self.tbl = QTableWidget(0, len(self.HEADERS))
        self.tbl.setHorizontalHeaderLabels(self.HEADERS)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl.verticalHeader().setVisible(False)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.tbl.doubleClicked.connect(lambda _i: self.open_selected())
        lay.addWidget(self.tbl)
        self.info_label = QLabel("一覧表を選んで［開く］（ダブルクリックでも開けます）。"
                                 "Archicad の一覧表と同じ対象要素・列でデータ表示・編集ウィンドウを開きます。")
        self.info_label.setWordWrap(True)
        lay.addWidget(self.info_label)
        btns = QWidget(); bl = QHBoxLayout(btns); bl.setContentsMargins(0, 0, 0, 0)
        self.btn_reload = QPushButton("再読込")
        self.btn_open   = QPushButton("開く")
        self.btn_close  = QPushButton("閉じる")
        self.btn_reload.clicked.connect(self.reload)
        self.btn_open.clicked.connect(self.open_selected)
        self.btn_close.clicked.connect(self.close)
        bl.addWidget(self.btn_reload); bl.addStretch(); bl.addWidget(self.btn_open); bl.addWidget(self.btn_close)
        lay.addWidget(btns)
        self.reload()

    def reload(self):
        folder = self._win.schedule_folder()
        self._items = []
        if not folder or not os.path.isdir(folder):
            self.folder_label.setText("連動一覧表のフォルダが見つかりません。Archicad でプロジェクトを開いてから再読込してください。")
        else:
            from datetime import datetime
            stamp = datetime.fromtimestamp(os.path.getmtime(folder)).strftime("%Y-%m-%d %H:%M")
            self._items = schedules.list_schedules(folder)
            self.folder_label.setText(f"{len(self._items)} 件（Archicad が書き出した一覧表の定義・{stamp} 更新）\n{folder}")
        self.tbl.setRowCount(len(self._items))
        for r, s in enumerate(self._items):
            sm = schedules.summary(s)
            state = "開けます" if sm["openable"] else (
                "対象要素が未対応" if not sm["types"] else "取得できる列がありません")
            if sm["openable"] and sm["unsupported"]:
                state = f"一部の条件は未対応（{len(sm['unsupported'])}件）"
            cells = [s["order"], s["display"], sm["type_labels"], sm["columns"], sm["criteria"], state]
            for c, val in enumerate(cells):
                item = QTableWidgetItem(str(val))
                if not sm["openable"]:
                    item.setForeground(QColor("gray"))
                if c == 5 and sm["unsupported"]:
                    item.setToolTip("未対応の条件（絞り込みに使いません）:\n" + "\n".join(sm["unsupported"]))
                self.tbl.setItem(r, c, item)
        self._apply_filter(self.filter_edit.text())

    def _apply_filter(self, text):
        t = (text or "").strip().casefold()
        for r, s in enumerate(self._items):
            self.tbl.setRowHidden(r, bool(t) and t not in s["display"].casefold())

    def selected_schedule(self):
        rows = {i.row() for i in self.tbl.selectionModel().selectedRows()}
        if not rows:
            return None
        return self._items[min(rows)]

    def open_selected(self):
        s = self.selected_schedule()
        if s is None:
            self.info_label.setText("一覧表を選択してください。")
            return
        if not schedules.summary(s)["openable"]:
            self.info_label.setText(f"「{s['name']}」はこのアドオンで扱えない要素タイプ・列のため開けません。")
            return
        self.info_label.setText(f"「{s['name']}」を取得中...")
        self._win.open_schedule(s)


# ─────────────────────────────────────────────────────────────────────
# DataPanel  データ表示・編集ウィンドウ（複数同時に開ける）
# ─────────────────────────────────────────────────────────────────────

class DataPanel(QWidget):
    """データ表示・編集ウィンドウ 1 枚分。表・ボタン・Undo 履歴・作業状態をウィンドウごとに持つ。

    画面部品と操作は MainWindow._create_data_panel で組み立て、処理は MainWindow 側の
    既存メソッドを「このウィンドウをアクティブにしてから」呼ぶ。
    """
    activated = pyqtSignal(object)   # このウィンドウが前面・操作対象になった
    closed    = pyqtSignal(object)   # 閉じた（SQLite の登録を消す）

    def __init__(self, window_id):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.Window)
        self.window_id     = window_id
        self.window_key    = None      # None = まだデータを表示していない（再利用可能）
        self.seq           = 0         # 表示用の通し番号
        self.elements      = []
        self.condition_id  = None
        self.confirm_close = None      # callable(panel) -> bool。False なら閉じない
        # ウィンドウごとの作業状態（MainWindow から委譲される）
        self.current_props          = []
        self._selected_guids        = []
        self._pending_changes       = None
        self._verifying_stamps      = False
        self._pre_verify_original   = {}
        self._pre_verify_stamps     = {}
        self._pending_skip_count    = 0
        self._pending_status_change = None
        self._pending_bim_restore   = False
        self._replace_dlg           = None
        self._click_prev_guid       = None
        self._click_prev_status     = 0

    def has_unsynced_changes(self):
        m = self.model
        return any(str(v) != str(m._original_values.get(k)) for k, v in m._values.items())

    def event(self, e):
        if e.type() == QEvent.Type.WindowActivate:
            self.activated.emit(self)
        return super().event(e)

    def closeEvent(self, e):
        if self.confirm_close is not None and not self.confirm_close(self):
            e.ignore()
            return
        super().closeEvent(e)
        self.closed.emit(self)


# Excel 出力の設定キー（QSettings）
EXCEL_LOCK_GUID_KEY = "excel/lock_guid"   # GUID 列を編集不可（シート保護）
EXCEL_HIDE_GUID_KEY = "excel/hide_guid"   # GUID 列を非表示

# 条件設定ウィンドウのグループ枠（① 条件を指定して取得 / ② 別の方法で取得）
GROUP_BOX_STYLE = (
    "QGroupBox { font-weight: bold; color: #1a5276; border: 1px solid #9FB0C2; border-radius: 4px;"
    " margin-top: 10px; padding-top: 6px; }"
    "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }"
)

# データ表示・編集の表の見た目（Excel 風）
TABLE_HEADER_COLOR    = "#D6E0EB"   # タイトル行（列見出し）: 少し濃い青灰色
TABLE_ALTERNATE_COLOR = "#EEF3F8"   # 1 行おきの縞模様: ごく薄い青灰色
TABLE_STYLE = (
    f"QTableView {{ alternate-background-color: {TABLE_ALTERNATE_COLOR}; background-color: #FFFFFF;"
    f" gridline-color: #C8D2DC; }}"
    f"QHeaderView::section:horizontal {{ background-color: {TABLE_HEADER_COLOR}; color: #1F2D3A;"
    f" font-weight: bold; padding: 3px 6px; border: none;"
    f" border-right: 1px solid #B4C2D0; border-bottom: 1px solid #9FB0C2; }}"
    f"QHeaderView::section:vertical {{ background-color: {TABLE_HEADER_COLOR}; padding: 0 4px;"
    f" border: none; border-bottom: 1px solid #C8D2DC; border-right: 1px solid #9FB0C2; }}"
    f"QTableCornerButton::section {{ background-color: {TABLE_HEADER_COLOR}; border: none; }}"
)


# 作業中のデータウィンドウに委譲する MainWindow の属性（self.model 等はアクティブなウィンドウのもの）
PANEL_ATTRS = frozenset({
    "status_label", "search_edit", "row_count_label", "btn_clear_sort", "btn_replace",
    "btn_export_excel", "btn_import_excel", "table", "model", "proxy",
    "btn_sync", "btn_clear_flags", "btn_bim_override", "btn_diff",
    "btn_confirm", "btn_reset_status", "btn_confirm_all", "btn_undo", "btn_redo",
    "btn_zoom_toggle", "zoom_slider", "zoom_factor_label", "btn_zoom_fit", "undo_stack",
    "current_props", "_selected_guids", "_pending_changes", "_verifying_stamps",
    "_pre_verify_original", "_pre_verify_stamps", "_pending_skip_count",
    "_pending_status_change", "_pending_bim_restore", "_replace_dlg",
    "_click_prev_guid", "_click_prev_status",
})


# ─────────────────────────────────────────────────────────────────────
# MainWindow
# ─────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        # ① DB・セッション・Undo スタックを setup_ui より先に初期化
        self.db                  = DbManager()
        self.session_id          = str(uuid.uuid4())
        self.current_condition_id = None
        self._last_condition      = ([], {})   # 直近の抽出条件（階, 要素タイプ）

        # データウィンドウ管理（複数表示）。前回の異常終了で残った登録は消す
        self._panels          = {}   # window_id -> DataPanel
        self._panel_seq       = 0
        self._active_panel    = None
        self._value_requests  = {}   # reqId -> DataPanel（get_values の応答先）
        self._refresh_requests = {}  # reqId -> DataPanel（反映後の取り直し。自動計算の結果を最新にする）
        self._owners          = {k: deque() for k in ("sync", "status", "flag", "override")}
        try:
            self.db.clear_windows()
        except Exception as e:
            append_log(f"[DB ERROR] clear_windows: {e}")

        # ウィンドウの大きさ・位置の保存（データウィンドウ生成より先に用意）
        self._tracked_windows = {}           # widget -> 保存キー
        self._last_moved      = {}           # 保存キー -> 最後に動かしたウィンドウ
        self._closing         = False
        self._shown_windows   = set()        # 今回表示したウィンドウ（未表示のものは保存しない）
        self._state_timer = QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(500)   # 連続したサイズ変更をまとめて保存
        self._state_timer.timeout.connect(self.save_window_state)

        self.setWindowTitle("Archicad BIM - 条件設定")
        self.resize(620, 420)

        # ② UI 構築
        self.setup_ui()

        # シグナル接続
        self.signals = CommunicationSignals()
        self.signals.config_received.connect(self.on_config)
        self.signals.elements_received.connect(self.on_elements)
        self.signals.definitions_received.connect(self.on_definitions)
        self.signals.values_received.connect(self.on_values)
        self.signals.sync_complete.connect(self.on_sync_complete)
        self.signals.flag_result_received.connect(self.on_flag_result)
        self.signals.bim_override_result_received.connect(self.on_bim_override_result)
        self.signals.change_status_result_received.connect(self.on_change_status_result)
        self.signals.show_window.connect(self._show_all_windows)
        self.signals.error_occurred.connect(self.on_error)
        self.signals.selection_changed.connect(self.on_archicad_selection_changed)
        self.signals.current_floor_received.connect(self.on_current_floor)
        self.signals.site_summary_received.connect(self.on_site_summary)
        self.signals.schedule_resolved_received.connect(self.on_schedule_resolved)
        self.signals.project_info_result_received.connect(self.on_project_info_result)
        self._schedule_jobs = {}     # reqId -> 一覧表の取得ジョブ（列の解決 → 要素 → 値）
        self._schedule_dlg  = None
        self._site_dlg = None
        self.server_thread = None

        # 内部状態
        self._pending_changes        = None
        self._verifying_stamps       = False
        self._pre_verify_original    = {}
        self._pre_verify_stamps      = {}
        self._pending_skip_count     = 0
        self._selected_guids         = []
        self._pending_status_change  = None
        self._click_prev_guid        = None
        self._click_prev_status      = 0
        self._current_element_type   = ""
        self._pending_bim_restore    = False
        self._last_archicad_floor    = None  # None=未確定, 0+=有効(0=1FL)
        self._floor_before_op        = None  # None=操作外, 0+=有効フロア
        self._pyqt_active            = False
        self._start_floor_polling()

        # dialog_order.json
        _order_file = Path(__file__).resolve().parent / "dialog_order.json"
        try:
            with _order_file.open(encoding="utf-8") as _f:
                _order_cfg = json.load(_f)
            self._group_order    = _order_cfg.get("group_order", [])
            self._type_overrides = _order_cfg.get("element_type_overrides", {})
            append_log(f"[ORDER] dialog_order.json 読み込み完了: {len(self._group_order)} グループ定義")
        except Exception as _e:
            self._group_order    = []
            self._type_overrides = {}
            append_log(f"[ORDER] dialog_order.json 読み込み失敗: {_e}")

        # ⑦ 起動時に未クリア競合フラグを復元
        self._restore_conflict_flags()

        # ⑧ ウィンドウの大きさ・位置を前回の状態に復元し、変更のたびに自動保存
        self._track_window("main", self)
        self._restore_splitter()

    # ── 複数データウィンドウ: 作業中ウィンドウへの委譲 ──────────────────

    def __getattr__(self, name):
        # 通常の属性探索で見つからない時だけ呼ばれる → self.model 等はアクティブなウィンドウのもの
        if name in PANEL_ATTRS:
            panel = self.__dict__.get("_active_panel")
            if panel is not None:
                return getattr(panel, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in PANEL_ATTRS and self.__dict__.get("_active_panel") is not None:
            setattr(self.__dict__["_active_panel"], name, value)
            return
        super().__setattr__(name, value)

    @property
    def data_panel(self):
        """作業中（アクティブ）のデータ表示・編集ウィンドウ。"""
        return self._active_panel

    def open_panels(self):
        """データを表示中（SQLite に登録済み）のウィンドウ一覧。"""
        return [p for p in self._panels.values() if p.window_key is not None]

    def activate_panel(self, panel):
        if panel is not None and panel.window_id in self._panels:
            self.__dict__["_active_panel"] = panel

    def _bind(self, panel, fn, nargs=0):
        """部品のシグナル → 「panel をアクティブにしてから fn」を呼ぶスロット。"""
        def slot(*args):
            if panel.window_id not in self._panels:
                return None   # 閉じた（破棄中の）ウィンドウからの通知は無視
            self.activate_panel(panel)
            return fn(*args[:nargs])
        return slot

    def _create_data_panel(self):
        self._panel_seq += 1
        p = DataPanel(str(uuid.uuid4()))
        p.seq = self._panel_seq
        p.setWindowTitle("Archicad BIM - データ表示・編集")
        p.resize(1050, 720)
        b = lambda fn, n=0: self._bind(p, fn, n)   # noqa: E731

        dp_lay = QVBoxLayout(p)
        dp_lay.setContentsMargins(4, 4, 4, 4)
        dp_lay.setSpacing(4)
        p.status_label = QLabel("待機中")
        dp_lay.addWidget(p.status_label)

        # ── Excel 風ツールバー（検索・並び替え解除・置換・Excel 入出力）──
        tool_row = QWidget()
        trl = QHBoxLayout(tool_row); trl.setContentsMargins(0, 0, 0, 0)
        p.search_edit = QLineEdit()
        p.search_edit.setPlaceholderText("表内を検索（全列・部分一致）")
        p.search_edit.setClearButtonEnabled(True)
        p.search_edit.textChanged.connect(b(self.on_search_changed, 1))
        p.row_count_label = QLabel("表示 0 / 全 0 件")
        p.btn_clear_sort = QPushButton("並び替え解除")
        p.btn_clear_sort.setToolTip("見出しクリックで並び替え（昇順→降順→解除）")
        p.btn_clear_sort.clicked.connect(b(self.clear_sort))
        p.btn_replace = QPushButton("置換")
        p.btn_replace.setToolTip("検索と置換 (Ctrl+H)")
        p.btn_replace.clicked.connect(b(self.show_replace_dialog))
        p.btn_export_excel = QPushButton("Excel出力")
        p.btn_export_excel.setToolTip("表示中の行を .xlsx に書き出し（未反映の変更は赤塗り）")
        p.btn_export_excel.clicked.connect(b(self.export_excel))
        p.btn_import_excel = QPushButton("Excel取込")
        p.btn_import_excel.setToolTip("編集した .xlsx を読み込み、差分のみ表に反映（Undo 可）")
        p.btn_import_excel.clicked.connect(b(self.import_excel))
        # Excel 出力の設定（GUID 列の編集不可・非表示）。設定はウィンドウ共通で記憶
        p.btn_excel_opts = QPushButton("Excel出力設定")
        p.btn_excel_opts.setToolTip("GUID 列を編集不可にする / 非表示にする（出力時の設定）")
        opts_menu = QMenu(p.btn_excel_opts)
        p.act_lock_guid = opts_menu.addAction("GUID列を編集不可にする（シート保護）")
        p.act_hide_guid = opts_menu.addAction("GUID列を非表示にする")
        for act, key in ((p.act_lock_guid, EXCEL_LOCK_GUID_KEY), (p.act_hide_guid, EXCEL_HIDE_GUID_KEY)):
            act.setCheckable(True)
            act.toggled.connect(lambda on, k=key: self.set_excel_option(k, on))
        opts_menu.aboutToShow.connect(lambda: self._sync_excel_option_actions(p))
        self._sync_excel_option_actions(p)
        p.btn_excel_opts.setMenu(opts_menu)
        trl.addWidget(QLabel("検索:"))
        trl.addWidget(p.search_edit, 1)
        trl.addWidget(p.row_count_label)
        for w in (p.btn_clear_sort, p.btn_replace, p.btn_export_excel, p.btn_import_excel, p.btn_excel_opts):
            trl.addWidget(w)
        dp_lay.addWidget(tool_row)

        # ── 表 ──
        p.table = QTableView()
        p.model = PropertyTableModel()
        p.proxy = ExcelFilterProxyModel()
        p.proxy.setSourceModel(p.model)
        p.table.setModel(p.proxy)
        # 見やすさ: タイトル行（列見出し）を少し濃く、データ行は 1 行おきに薄い色（縞模様）。
        # 変更（赤）・確認済（緑）・競合などモデルが色を付けたセルはその色が優先される
        p.table.setAlternatingRowColors(True)
        p.table.setStyleSheet(TABLE_STYLE)
        # Excel 同様: セル単位で複数選択（Shift/Ctrl+クリック、ドラッグ）
        p.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        p.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        # 見出しクリックで並び替え。初期状態は取得順のまま（並び替えなし）
        hh = p.table.horizontalHeader()
        hh.setSortIndicatorClearable(True)
        hh.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        p.table.setSortingEnabled(True)
        p.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        p.table.customContextMenuRequested.connect(b(self.show_cell_menu, 1))
        for sig in (p.proxy.modelReset, p.proxy.rowsInserted, p.proxy.rowsRemoved, p.proxy.layoutChanged):
            sig.connect(b(self._update_row_count))
        # EditTrigger.NoEditTriggers で Qt 内部トリガーを無効化し、
        # doubleClicked シグナルから直接 edit() を呼ぶことで確実に編集を起動する
        p.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        p.table.doubleClicked.connect(b(self._on_table_double_clicked, 1))
        p.table.clicked.connect(b(self.on_table_clicked, 1))
        hh.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hh.customContextMenuRequested.connect(b(self.show_filter_menu, 1))
        dp_lay.addWidget(p.table)

        # ── ボタン行 ──
        def row(*widgets):
            w = QWidget(); lay = QHBoxLayout(w); lay.setContentsMargins(0, 0, 0, 0)
            for x in widgets:
                if isinstance(x, int):
                    lay.addSpacing(x)
                else:
                    lay.addWidget(x)
            dp_lay.addWidget(w)
            return lay

        p.btn_sync = QPushButton("Archicadへ反映")
        p.btn_sync.clicked.connect(b(self.sync_data))
        p.btn_clear_flags = QPushButton("競合フラグ解除")
        p.btn_clear_flags.setEnabled(False)
        p.btn_clear_flags.clicked.connect(b(self.clear_value_conflict_flags))
        row(p.btn_sync, p.btn_clear_flags)

        p.btn_bim_override = QPushButton("変更強調 OFF")
        p.btn_bim_override.setCheckable(True)
        p.btn_bim_override.clicked.connect(b(self.toggle_bim_override))
        p.btn_diff = QPushButton("差分を比較")
        p.btn_diff.setEnabled(False)
        p.btn_diff.clicked.connect(b(self.show_diff_dialog))
        row(p.btn_bim_override, p.btn_diff)

        p.btn_confirm = QPushButton("確認済 (2)")
        p.btn_confirm.setToolTip("選択行を ChangeStatus=2（緑）に設定")
        p.btn_confirm.setEnabled(False)
        p.btn_confirm.clicked.connect(b(self.mark_confirmed))
        p.btn_reset_status = QPushButton("リセット (0)")
        p.btn_reset_status.setToolTip("選択行を ChangeStatus=0（未変更）に戻す")
        p.btn_reset_status.setEnabled(False)
        p.btn_reset_status.clicked.connect(b(self.reset_change_status))
        p.btn_confirm_all = QPushButton("全件リセット")
        p.btn_confirm_all.setToolTip("テーブル全要素を ChangeStatus=0（未変更）にリセット")
        p.btn_confirm_all.setEnabled(False)
        p.btn_confirm_all.clicked.connect(b(self.reset_all_table))
        row(p.btn_confirm, p.btn_reset_status, p.btn_confirm_all)

        p.btn_undo = QPushButton("戻る (Undo)")
        p.btn_undo.setEnabled(False)
        p.btn_redo = QPushButton("進む (Redo)")
        p.btn_redo.setEnabled(False)
        p.btn_undo.clicked.connect(b(self._do_undo))
        p.btn_redo.clicked.connect(b(self._do_redo))
        row(p.btn_undo, p.btn_redo)

        p.btn_zoom_toggle = QPushButton("ズーム OFF")
        p.btn_zoom_toggle.setCheckable(True)
        p.btn_zoom_toggle.setFixedWidth(90)
        p.btn_zoom_toggle.setToolTip("ONにすると行クリック時にArchiCADでズーム表示します")
        p.btn_zoom_toggle.toggled.connect(b(self._on_zoom_toggle, 1))
        p.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        p.zoom_slider.setRange(10, 50)      # 値 10～50 → 1.0x～5.0x（÷10）
        p.zoom_slider.setValue(15)          # デフォルト 1.5x
        p.zoom_slider.setTickInterval(5)
        p.zoom_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        p.zoom_slider.setFixedWidth(180)
        p.zoom_factor_label = QLabel("1.5x")
        p.zoom_factor_label.setFixedWidth(40)
        p.zoom_slider.valueChanged.connect(lambda v: p.zoom_factor_label.setText(f"{v / 10.0:.1f}x"))
        p.btn_zoom_fit = QPushButton("全体表示")
        p.btn_zoom_fit.setToolTip("ArchiCADのビューを全体表示（フィット）します")
        p.btn_zoom_fit.setFixedWidth(80)
        p.btn_zoom_fit.clicked.connect(b(self.zoom_fit_all))
        row(p.btn_zoom_toggle, 8, QLabel("余白:"), p.zoom_slider, p.zoom_factor_label, 8, p.btn_zoom_fit).addStretch()

        # ── Undo/Redo（ウィンドウごと）──
        p.undo_stack = QUndoStack(p)
        p.undo_stack.setUndoLimit(200)
        p.model._undo_stack = p.undo_stack
        p.model._db         = self.db
        p.model._session_id = self.session_id
        p.undo_stack.canUndoChanged.connect(p.btn_undo.setEnabled)
        p.undo_stack.canRedoChanged.connect(p.btn_redo.setEnabled)
        p.undo_stack.undoTextChanged.connect(lambda t: p.btn_undo.setText(f"戻る: {t}" if t else "戻る (Undo)"))
        p.undo_stack.redoTextChanged.connect(lambda t: p.btn_redo.setText(f"進む: {t}" if t else "進む (Redo)"))
        p.undo_stack.indexChanged.connect(lambda _: p.table.viewport().update())

        if QShortcut is not None:
            for keys, slot, parent in ((QKeySequence.StandardKey.Undo, self._do_undo, p),
                                       (QKeySequence.StandardKey.Redo, self._do_redo, p),
                                       (QKeySequence.StandardKey.Copy, self.copy_selection, p.table),
                                       (QKeySequence.StandardKey.Paste, self.paste_clipboard, p.table),
                                       (QKeySequence("Ctrl+D"), self.fill_down, p.table),
                                       (QKeySequence("Ctrl+H"), self.show_replace_dialog, p.table)):
                sc = QShortcut(keys, parent)
                if parent is p.table:
                    sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                sc.activated.connect(b(slot))

        # ── 管理（アクティブ化・閉じる・大きさの記憶・ボタンの反応）──
        p.activated.connect(self.activate_panel)
        p.closed.connect(self._on_panel_closed)
        p.confirm_close = self._confirm_panel_close
        self._panels[p.window_id] = p
        self._track_window("data_panel", p)
        n_open = len(self.open_panels())
        if n_open:                                   # 2 枚目以降は少しずらして重ならないように
            p.move(p.x() + 30 * n_open, p.y() + 30 * n_open)
        self._connect_flash(p)
        return p

    def _panel_title(self, panel, stories, types):
        names = {s["index"]: s["name"] for s in getattr(self, "_story_list", [])}
        floor_txt = "全階" if not stories else ", ".join(str(names.get(s, s)) for s in stories)
        type_txt = ", ".join(ELEMENT_TYPE_LABELS.get(k, k) for k, v in types.items() if v) or "-"
        return (f"データ表示・編集 #{panel.seq}  {floor_txt} / {type_txt}"
                f"  ({len(panel.elements)}件 × {len(panel.current_props)}列)")

    def _acquire_panel(self):
        """データを入れるウィンドウ: 未使用の作業中ウィンドウがあれば再利用、無ければ新規。"""
        p = self._active_panel
        if p is not None and p.window_key is None and not p.model._elements:
            return p
        for q in self._panels.values():
            if q.window_key is None and not q.model._elements:
                return q
        return self._create_data_panel()

    def _confirm_panel_close(self, panel):
        if not panel.has_unsynced_changes():
            return True
        return QMessageBox.question(
            panel, "データ表示・編集を閉じる",
            "Archicad に反映していない変更（赤字）があります。\n閉じると変更は破棄されます。閉じますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def _on_panel_closed(self, panel):
        """閉じたウィンドウは SQLite の登録ごと消去する。"""
        if panel.window_id not in self._panels:
            return
        del self._panels[panel.window_id]
        if panel.window_key is not None:
            try:
                self.db.close_window(panel.window_id)
            except Exception as e:
                append_log(f"[DB ERROR] close_window: {e}")
        for rid, q in list(self._value_requests.items()):
            if q is panel:
                del self._value_requests[rid]
        for dq in self._owners.values():
            for q in [q for q in dq if q is panel]:
                dq.remove(q)
        self._untrack_window(panel)
        # 破棄中にモデル/Undo が出す通知（件数・Undo 可否の変化）で、既に消えた部品を触らないよう止める
        for obj in (panel.undo_stack, panel.proxy, panel.model):
            obj.blockSignals(True)
        append_log(f"[PANEL] closed #{panel.seq} ({panel.window_id[:8]})")
        if self._active_panel is panel:
            remaining = list(self._panels.values())
            if remaining:
                self.__dict__["_active_panel"] = remaining[-1]
            elif not self._closing:
                # 作業中ウィンドウが無くなっても self.model 等が使えるよう未使用の 1 枚を用意
                self.__dict__["_active_panel"] = self._create_data_panel()
        panel.deleteLater()

    def _discard_panel(self, panel):
        """表示前のウィンドウを未使用状態に戻す（SQLite の登録も消す）。"""
        if panel.window_key is not None:
            try:
                self.db.close_window(panel.window_id)
            except Exception as e:
                append_log(f"[DB ERROR] close_window: {e}")
        panel.window_key = None
        panel.elements = []

    def _pop_owner(self, kind):
        """応答の宛先ウィンドウ（送信順）。不明なら作業中のウィンドウ。"""
        dq = self._owners.get(kind)
        while dq:
            p = dq.popleft()
            if p.window_id in self._panels:
                return p
        return self._active_panel

    def _push_owner(self, kind):
        self._owners[kind].append(self._active_panel)

    def _send_owned(self, kind, data):
        """応答（反映結果・ステータス等）をこのウィンドウへ返すため、送信順に宛先を記録して送る。"""
        self._push_owner(kind)
        self.send_to_ac_async(data)

    # ── 両ウィンドウを同時に表示 ─────────────────────────────────────

    def _show_all_windows(self):
        """ArchiCAD からの project_config 受信時: 条件設定パネルのみ表示。
        データパネルは [データ取得] ボタンで初めて開く。"""
        self.show()

    # ── ⑦ 起動時復元 ────────────────────────────────────────────────

    def _restore_conflict_flags(self):
        try:
            flags = self.db.load_uncleared_flags()
            if not flags:
                return
            for f in flags:
                eg, pg = f["element_guid"], f["property_guid"]
                self.model._value_conflict_cells[(eg, pg)] = {
                    "orig_val": f["orig_val"],
                    "ac_val":   f["ac_val"],
                    "user_val": f["user_val"],
                }
                self.model._value_conflict_guids.add(eg)
            self.btn_clear_flags.setEnabled(True)
            append_log(f"[RESTORE] 競合フラグ {len(flags)} 件復元")
        except Exception as e:
            append_log(f"[RESTORE ERROR] {e}")

    # ── ② UI 構築（条件設定パネル ＋ 独立データパネル）──────────────

    def setup_ui(self):
        # ════════════════════════════════════════════════════════════
        # 左：条件設定パネル（MainWindow 本体）― 要素抽出 ／ プロパティ設定 を横並び
        # ════════════════════════════════════════════════════════════
        central = QWidget()
        self.setCentralWidget(central)
        c_lay = QVBoxLayout(central)
        c_lay.setContentsMargins(6, 6, 6, 6)
        c_lay.setSpacing(4)

        # 水平 QSplitter で左右に並べる
        h_split = self.h_split = QSplitter(Qt.Orientation.Horizontal)

        # ── 左半分：要素抽出 ──
        sect_extract = QWidget()
        se_lay = QVBoxLayout(sect_extract)
        se_lay.setContentsMargins(0, 0, 4, 0)
        se_lay.setSpacing(4)
        lbl_extract = QLabel("  要素抽出")
        lbl_extract.setStyleSheet(
            "font-weight: bold; color: #1a5276;"
            "background: #d6eaf8; padding: 3px 6px; border-radius: 3px;"
        )
        self.filter_tree = QTreeWidget()
        self.filter_tree.setHeaderLabel("階 / タイプ")
        self.filter_tree.itemChanged.connect(self.on_filter_item_changed)
        self.btn_search = QPushButton("要素を抽出")
        self.btn_search.clicked.connect(self.search_elements)
        self.btn_site_summary = QPushButton("敷地情報（敷地マネージャー）")
        self.btn_site_summary.setToolTip("用途地域・敷地面積・建蔽率・容積率などをまとめて取得")
        self.btn_site_summary.clicked.connect(self.request_site_summary)
        self.btn_schedules = QPushButton("一覧表（連動一覧表）から取得...")
        self.btn_schedules.setToolTip("モデル内の連動一覧表と同じ対象要素・列でデータ表示・編集ウィンドウを開く")
        self.btn_schedules.clicked.connect(self.show_schedule_list)
        se_lay.addWidget(lbl_extract)
        se_lay.addWidget(self.filter_tree)
        se_lay.addWidget(self.btn_search)

        # ── 右半分：プロパティ設定 ──
        sect_prop = QWidget()
        sp_lay = QVBoxLayout(sect_prop)
        sp_lay.setContentsMargins(4, 0, 0, 0)
        sp_lay.setSpacing(4)
        lbl_prop = QLabel("  プロパティ設定")
        lbl_prop.setStyleSheet(
            "font-weight: bold; color: #1a5276;"
            "background: #d6eaf8; padding: 3px 6px; border-radius: 3px;"
        )
        # 表示切替: 簡易版（主）= 基本情報＋プロジェクトで作成したプロパティ / 詳細版（サブ）= 全プロパティ
        self.prop_mode_combo = QComboBox()
        self.prop_mode_combo.addItems([PROP_MODE_SIMPLE, PROP_MODE_DETAIL])
        self.prop_mode_combo.setToolTip("簡易版: W・L・H・レベル・分類など基本情報と、作成したプロパティ\n"
                                        "詳細版: Archicad の全プロパティ")
        self.prop_mode_combo.currentIndexChanged.connect(lambda _i: self._populate_prop_tree())
        self.prop_tree = QTreeWidget()
        self.prop_tree.setHeaderLabel("プロパティ")
        self.prop_tree.itemChanged.connect(self.on_prop_item_changed)
        self._all_defs = []
        self.btn_get_values = QPushButton("データ取得")
        self.btn_get_values.clicked.connect(self.get_values)
        sp_lay.addWidget(lbl_prop)
        sp_lay.addWidget(self.prop_mode_combo)
        sp_lay.addWidget(self.prop_tree)
        sp_lay.addWidget(self.btn_get_values)

        h_split.addWidget(sect_extract)
        h_split.addWidget(sect_prop)
        h_split.setSizes([1, 1])   # 等分で初期表示

        # ── グループ①: 条件を指定して取得（要素抽出 → プロパティ設定 → データ取得）──
        self.grp_condition = QGroupBox("① 条件を指定して取得　（階・要素タイプ → 要素を抽出 → プロパティ → データ取得）")
        self.grp_condition.setStyleSheet(GROUP_BOX_STYLE)
        g1 = QVBoxLayout(self.grp_condition)
        g1.setContentsMargins(6, 6, 6, 6)
        g1.addWidget(h_split)
        c_lay.addWidget(self.grp_condition, 1)

        # ── グループ②: 別の方法で取得（条件設定とは独立して使える）──
        self.grp_other = QGroupBox("② 別の方法で取得　（上の条件設定とは関係なく使えます）")
        self.grp_other.setStyleSheet(GROUP_BOX_STYLE)
        g2 = QHBoxLayout(self.grp_other)
        g2.setContentsMargins(6, 6, 6, 6)
        g2.addWidget(self.btn_site_summary, 1)
        g2.addWidget(self.btn_schedules, 1)
        c_lay.addWidget(self.grp_other, 0)

        # 条件設定パネル下部：データ取得状態ラベル（1行・固定高さ）
        self.condition_status_label = QLabel("待機中")
        self.condition_status_label.setStyleSheet(
            "color: #1a5276; background: #eaf4fb; padding: 2px 6px; border-radius: 3px;"
        )
        self.condition_status_label.setFixedHeight(22)
        c_lay.addWidget(self.condition_status_label, 0)  # stretch=0 で伸びない

        self._setup_button_feedback()
        # 右：データ表示・編集ウィンドウ（最初の 1 枚。データ取得で使われ、以後は必要に応じて増える）
        self.activate_panel(self._create_data_panel())

    # ── プロパティ順序ソート ─────────────────────────────────────────

    def _sort_props_by_dialog_order(self, props: list, element_type: str = "") -> list:
        order    = self._type_overrides.get(element_type, self._group_order)
        rank     = {g: i for i, g in enumerate(order)}
        fallback = len(order)

        def sort_key(p):
            return (rank.get(p.get("group", ""), fallback), p.get("group", ""), p.get("name", ""))

        return sorted(props, key=sort_key)

    # ── エラー処理 ───────────────────────────────────────────────────

    # ── ボタンの処理中表示（取得中は少し濃く、完了で元に戻す）──────────────

    BUSY_TIMEOUT_MS = 30000   # 応答が無くても濃いままにならないよう自動で戻す
    FLASH_MS        = 250     # その場で終わる操作は押した瞬間だけ濃くする

    def _setup_button_feedback(self):
        self._busy_buttons = {}   # button -> (元のスタイル, トークン)
        self._busy_token = 0
        self._connect_flash(self)

    def _connect_flash(self, root):
        """root 配下の全ボタンに「押した瞬間だけ少し濃く」を付ける（データウィンドウ生成時にも呼ぶ）。"""
        for btn in root.findChildren(QPushButton):
            # 処理側の clicked より後に接続 → 処理中にしたボタンは点滅させない
            btn.clicked.connect(lambda _checked=False, b=btn: self.flash_button(b))

    @staticmethod
    def _shaded_style(btn):
        base = btn.palette().color(QPalette.ColorRole.Button)
        return (f"QPushButton {{ background-color: {base.darker(120).name()};"
                f" border: 1px solid {base.darker(160).name()}; border-radius: 3px; padding: 3px 8px; }}")

    def is_button_busy(self, btn):
        return btn in self._busy_buttons

    def set_button_busy(self, btn):
        """ArchiCAD からの応答待ちの間、ボタンを少し濃くする。"""
        if btn in self._busy_buttons:
            return
        self._busy_token += 1
        token = self._busy_token
        self._busy_buttons[btn] = (btn.styleSheet(), token)
        btn.setStyleSheet(self._shaded_style(btn))
        QTimer.singleShot(self.BUSY_TIMEOUT_MS, lambda: self.clear_button_busy(btn, token))

    def clear_button_busy(self, btn, token=None):
        entry = self._busy_buttons.get(btn)
        if entry is None or (token is not None and entry[1] != token):
            return   # 既に戻っている / 新しい処理中に置き換わった
        del self._busy_buttons[btn]
        try:
            btn.setStyleSheet(entry[0])
        except RuntimeError:
            pass   # ウィンドウを閉じてボタンが破棄済み（タイマー経由で呼ばれた場合）

    def clear_all_busy(self):
        for btn in list(self._busy_buttons):
            self.clear_button_busy(btn)

    def flash_button(self, btn):
        """応答を待たない操作: 押した瞬間だけ少し濃くして戻す。"""
        if btn in self._busy_buttons:
            return
        self.set_button_busy(btn)
        token = self._busy_buttons[btn][1]
        QTimer.singleShot(self.FLASH_MS, lambda: self.clear_button_busy(btn, token))

    def on_error(self, m):
        self.clear_all_busy()
        if self._schedule_jobs:                  # 通信エラー → 一覧表の取得は中止
            self._schedule_jobs.clear()
            self._schedule_status("一覧表の取得を中止しました（通信エラー）")
        # 値を待っていて、まだ一度も表示していないウィンドウは登録ごと破棄（同条件で取り直せるように）
        for rid, panel in list(self._value_requests.items()):
            del self._value_requests[rid]
            if not panel.isVisible() and not panel._verifying_stamps:
                self._discard_panel(panel)
        if self._verifying_stamps:
            self._verifying_stamps  = False
            self._pending_changes   = None
            self._pre_verify_original = {}
            self._pre_verify_stamps   = {}
            self.btn_sync.setEnabled(True)
            self.status_label.setText("接続エラー")
        QMessageBox.warning(self, "エラー", m)

    # ── ⑧ 抽出条件保存 + 要素抽出 ───────────────────────────────────

    def search_elements(self):
        stories, types = [], {}
        it = QTreeWidgetItemIterator(self.filter_tree)
        while it.value():
            item = it.value()
            if item.parent():
                if item.parent().text(0) == "階層":
                    if item.checkState(0) == Qt.CheckState.Checked:
                        stories.append(item.data(0, Qt.ItemDataRole.UserRole))
                else:
                    types[item.data(0, Qt.ItemDataRole.UserRole)] = (
                        item.checkState(0) == Qt.CheckState.Checked
                    )
            it += 1
        # ⑧ 抽出条件を DB に保存
        try:
            self.current_condition_id = self.db.save_condition(stories, types)
            append_log(f"[DB] condition saved: id={self.current_condition_id}")
        except Exception as e:
            append_log(f"[DB ERROR] save_condition: {e}")
        self._last_condition = (stories, types)  # データウィンドウの同一判定・タイトルに使う
        self.condition_status_label.setText("要素抽出中...")
        self.set_button_busy(self.btn_search)   # プロパティ一覧の取得まで
        self.send_to_ac_async({"command": "get_elements", "stories": stories, **types})

    def on_config(self, data):
        self.filter_tree.blockSignals(True)
        self.filter_tree.clear()
        sr = QTreeWidgetItem(self.filter_tree, ["階層"])
        sr.setCheckState(0, Qt.CheckState.Unchecked)       # チェックで全階をまとめて選択/解除
        self._story_root = sr
        self._story_list = data.get("stories", [])
        for s in data.get("stories", []):
            c = QTreeWidgetItem(sr, [s["name"]])
            c.setData(0, Qt.ItemDataRole.UserRole, s["index"])
            c.setCheckState(0, Qt.CheckState.Unchecked)
        tr = QTreeWidgetItem(self.filter_tree, ["要素タイプ"])
        # 階ごとの件数（梁は支える床の階に属する等、選んだ階で件数が変わることを示す）
        self._type_story_counts = data.get("elementTypeStories", {})
        self._type_items = {}
        for n, cnt in data.get("elementTypes", {}).items():
            c = QTreeWidgetItem(tr, [""])
            c.setData(0, Qt.ItemDataRole.UserRole, n)
            c.setData(0, Qt.ItemDataRole.UserRole + 1, cnt)
            c.setCheckState(0, Qt.CheckState.Unchecked)
            self._type_items[n] = c
        self._update_type_counts()
        self.filter_tree.expandAll()
        self.filter_tree.blockSignals(False)

    def _selected_story_indexes(self):
        root = getattr(self, "_story_root", None)
        if root is None:
            return []
        return [root.child(i).data(0, Qt.ItemDataRole.UserRole) for i in range(root.childCount())
                if root.child(i).checkState(0) == Qt.CheckState.Checked]

    def _update_type_counts(self):
        """要素タイプの表示を「名前 (選択階の件数 / 全件数)」に更新する（階未選択なら全件数のみ）。"""
        stories = self._selected_story_indexes()
        for key, item in getattr(self, "_type_items", {}).items():
            total = item.data(0, Qt.ItemDataRole.UserRole + 1)
            label = ELEMENT_TYPE_LABELS.get(key, key)
            if stories:
                per = self._type_story_counts.get(key, {})
                n = sum(int(per.get(str(s), 0)) for s in stories)
                item.setText(0, f"{label} ({n} / 全{total})")
            else:
                item.setText(0, f"{label} ({total})")

    def on_filter_item_changed(self, item, col):
        root = getattr(self, "_story_root", None)
        if root is None:
            return
        self.filter_tree.blockSignals(True)
        try:
            if item is root:
                for i in range(root.childCount()):
                    root.child(i).setCheckState(0, root.checkState(0))
            elif item.parent() is root:
                all_on = all(root.child(i).checkState(0) == Qt.CheckState.Checked for i in range(root.childCount()))
                root.setCheckState(0, Qt.CheckState.Checked if all_on else Qt.CheckState.Unchecked)
            self._update_type_counts()
        finally:
            self.filter_tree.blockSignals(False)

    def on_elements(self, data):
        job = self._schedule_jobs.get(data.get("reqId"))
        if job is not None:                      # 一覧表の取得途中（条件設定パネルとは別）
            self._schedule_elements(job, data)
            return
        self.current_elements = data.get("elements", [])
        types = {e.get("type", "") for e in self.current_elements if e.get("type")}
        self._current_element_type = next(iter(types), "") if len(types) == 1 else ""
        cnt = len(self.current_elements)
        capped = " ※上限1000件で打ち切り（階やタイプを絞ってください）" if data.get("capped") else ""
        if cnt:
            self.condition_status_label.setText(f"要素: {cnt}件 抽出完了{capped} → プロパティ一覧を取得中...")
        else:
            self.condition_status_label.setText("該当なし。条件を変えて再試行してください")
        # ③ DB に要素を保存
        try:
            if self.current_elements and self.current_condition_id:
                self.db.upsert_elements(self.current_elements, self.current_condition_id)
                append_log(f"[DB] elements saved: {len(self.current_elements)}")
        except Exception as e:
            append_log(f"[DB ERROR] on_elements: {e}")
        # 要素が取得できたら自動的にプロパティ一覧を取得（ボタンはプロパティ一覧受信で戻す）
        if self.current_elements:
            self.request_definitions()
        else:
            self.clear_button_busy(self.btn_search)

    # ── プロパティ定義 ───────────────────────────────────────────────

    def request_definitions(self):
        # 全要素を送る: プロパティ定義は先頭要素から、GDL パラメータ定義は
        # 全オブジェクトのライブラリパーツから集める（敷地マネージャー等）
        if hasattr(self, "current_elements") and self.current_elements:
            self.send_to_ac_async({
                "command": "get_definitions",
                "guids":   [e["guid"] for e in self.current_elements],
            })

    def is_simple_mode(self):
        return self.prop_mode_combo.currentText() == PROP_MODE_SIMPLE

    def _checked_prop_guids(self):
        checked = set()
        it = QTreeWidgetItemIterator(self.prop_tree)
        while it.value():
            item = it.value()
            d = item.data(0, Qt.ItemDataRole.UserRole)
            if item.parent() and d and item.checkState(0) == Qt.CheckState.Checked:
                checked.add(d["guid"])
            it += 1
        return checked

    def _populate_prop_tree(self, checked=None):
        """現在のモードでプロパティ一覧を組み直す。checked 省略時は今のチェック状態を引き継ぐ。"""
        if checked is None:
            checked = self._checked_prop_guids()
        self.prop_tree.blockSignals(True)
        self.prop_tree.clear()
        groups = {}

        def add(group_name, d):
            if group_name not in groups:
                p = QTreeWidgetItem(self.prop_tree, [group_name])
                p.setCheckState(0, Qt.CheckState.Unchecked)
                groups[group_name] = p
            c = QTreeWidgetItem(groups[group_name], [d["name"]])
            c.setData(0, Qt.ItemDataRole.UserRole, d)
            c.setCheckState(0, Qt.CheckState.Checked if d["guid"] in checked else Qt.CheckState.Unchecked)

        if self.is_simple_mode():
            by_guid = {d["guid"]: d for d in self._all_defs}
            for key in SIMPLE_KEYS:                       # 基本情報（決まった順）
                if key in by_guid:
                    add(SIMPLE_GROUP, by_guid[key])
            custom = [d for d in self._all_defs if d.get("isCustom") and d["guid"] not in SIMPLE_KEYS]
            for d in self._sort_props_by_dialog_order(custom, element_type=self._current_element_type):
                add(d.get("group", "Other"), d)          # プロジェクトで作成したプロパティ
        else:
            for d in self._sort_props_by_dialog_order(self._all_defs, element_type=self._current_element_type):
                add(d.get("group", "Other"), d)

        for p in groups.values():                        # 子が全部チェック済みならグループもチェック
            if p.childCount() and all(p.child(i).checkState(0) == Qt.CheckState.Checked
                                      for i in range(p.childCount())):
                p.setCheckState(0, Qt.CheckState.Checked)
        self.prop_tree.collapseAll()
        if SIMPLE_GROUP in groups:
            groups[SIMPLE_GROUP].setExpanded(True)
        self.prop_tree.blockSignals(False)

    def on_definitions(self, data):
        self.clear_button_busy(self.btn_search)
        self._all_defs = data.get("definitions", [])
        # 簡易版の基本情報は最初からチェック → そのまま［データ取得］できる
        self._populate_prop_tree(checked=set(SIMPLE_KEYS))
        cnt = len(self.current_elements) if hasattr(self, "current_elements") else 0
        self.condition_status_label.setText(f"要素: {cnt}件 抽出完了 / プロパティ一覧取得完了")
        # ③ DB にプロパティ定義を保存
        try:
            defs = data.get("definitions", [])
            if defs:
                self.db.upsert_properties(defs)
                append_log(f"[DB] properties saved: {len(defs)}")
        except Exception as e:
            append_log(f"[DB ERROR] on_definitions: {e}")

    def on_prop_item_changed(self, item, col):
        self.prop_tree.blockSignals(True)
        for i in range(item.childCount()):
            item.child(i).setCheckState(0, item.checkState(0))
        self.prop_tree.blockSignals(False)

    # ── データ取得 ──────────────────────────────────────────────────

    def get_values(self):
        sel = []
        it  = QTreeWidgetItemIterator(self.prop_tree)
        while it.value():
            item = it.value()
            if item.parent() and item.checkState(0) == Qt.CheckState.Checked:
                d = item.data(0, Qt.ItemDataRole.UserRole)
                if d:
                    sel.append(d)
            it += 1
        if not (sel and getattr(self, "current_elements", None)):
            return
        stories, types = self._last_condition
        prop_guids = [p["guid"] for p in sel]
        key = DbManager.make_window_key(stories, types, prop_guids)

        # 同じ条件（階・要素タイプ・プロパティ）のウィンドウは開かない → 既存を前面に
        existing = next((p for p in self._panels.values() if p.window_key == key), None)
        if existing is not None or self.db.find_window(key) is not None:
            QMessageBox.information(
                self, "データ表示・編集",
                "同じ条件（階・要素タイプ・プロパティ）のウィンドウは既に開いています。\n"
                "そのウィンドウを前面に表示します。\n\n"
                "最新の値を取り直す場合は、そのウィンドウを閉じてから［データ取得］してください。")
            if existing is not None:
                self.activate_panel(existing)
                existing.show(); existing.raise_(); existing.activateWindow()
            return

        # 新しいウィンドウ（未使用のものがあれば再利用）を SQLite に登録してから値を要求
        panel = self._acquire_panel()
        panel.elements      = list(self.current_elements)
        panel.condition_id  = self.current_condition_id
        panel.current_props = sel   # ツリーは既に表示順（簡易版は W→L→H…）なのでその順で列にする
        panel.window_key    = key
        title = self._panel_title(panel, stories, types)
        panel.setWindowTitle(title)
        self.db.open_window(panel.window_id, key, title, panel.condition_id, stories, types,
                            [e["guid"] for e in panel.elements], prop_guids)
        self.activate_panel(panel)
        self._value_requests[panel.window_id] = panel
        self.set_button_busy(self.btn_get_values)
        append_log(f"[PANEL] open #{panel.seq} key={key}")
        self.send_to_ac_async({
            "command":   "get_values",
            "reqId":     panel.window_id,
            "guids":     [e["guid"] for e in panel.elements],
            "propGuids": prop_guids,
        })

    def _panel_for_values(self, data):
        """値の応答の宛先ウィンドウ（reqId で判別。古いアドオンは要求順）。"""
        rid = data.get("reqId")
        if rid and rid in self._value_requests:
            return self._value_requests.pop(rid)
        if self._value_requests:
            first = next(iter(self._value_requests))
            return self._value_requests.pop(first)
        return self._active_panel

    def on_values(self, data):
        refresh_panel = self._refresh_requests.pop(data.get("reqId"), None) if data.get("reqId") else None
        if refresh_panel is not None:            # 反映後の取り直し
            self._on_refreshed_values(refresh_panel, data)
            return
        job = self._schedule_jobs.get(data.get("reqId"))
        if job is not None:                      # 一覧表の取得途中
            self._schedule_values(job, data)
            return
        self.activate_panel(self._panel_for_values(data))
        if self._verifying_stamps:
            self._verifying_stamps = False
            self._on_pre_apply_verify(data)
            return
        self.clear_button_busy(self.btn_get_values)
        self._display_values(data)

    def _display_values(self, data):
        """作業中のデータウィンドウに値を表示し、SQLite に保存する。"""
        panel = self.data_panel
        if not panel.elements:   # 旧来の 1 枚運用（直接呼ばれた場合）は条件設定側の要素を使う
            panel.elements = list(getattr(self, "current_elements", []))
            panel.condition_id = self.current_condition_id
        v, s = {}, {}
        for item in data.get("values", []):
            g = item["guid"]
            s[g] = item.get("modiStamp", 0)
            for pg, val in item["props"].items():
                v[(g, pg)] = val
        self.model.set_data(panel.elements, self.current_props, v, s)
        # モデルにこのウィンドウの condition_id を反映
        self.model._current_condition_id = panel.condition_id
        self.btn_confirm_all.setEnabled(bool(self.model._elements))
        self.btn_diff.setEnabled(bool(self.model._elements))
        self._apply_enum_delegates()
        # 右ペインのステータスに件数を表示（左右のリンクを明示）
        elem_cnt = len(self.model._elements)
        prop_cnt = len(self.model._properties)
        self.status_label.setText(
            f"データ取得完了:  要素 {elem_cnt}件  ×  プロパティ {prop_cnt}列  （セルをダブルクリックで編集）"
        )
        # 条件設定パネルにデータ取得結果を表示
        self.condition_status_label.setText(
            f"データ取得完了:  要素 {elem_cnt}件  ×  プロパティ {prop_cnt}列"
        )
        self.status_label.setText("セルをダブルクリックで編集")
        # [データ取得] で初めてデータパネルを開く（以後は既に開いているので raise のみ）
        self.data_panel.show()
        self.data_panel.raise_()
        # ③ SQLite に保存
        try:
            self.db.upsert_elements(panel.elements, panel.condition_id, s)
            self.db.upsert_properties(getattr(self, "current_props", []))
            self.db.upsert_values(v, s)
            append_log(f"[DB] values saved: {len(v)} cells")
        except Exception as e:
            append_log(f"[DB ERROR] on_values: {e}")

    def _refresh_after_sync(self, guids):
        """反映に成功した要素の値を取り直す。入力元を変えると Archicad が W/L/H/レベル 等を
        再計算するので、その結果（自動計算の列）を表に反映する。"""
        panel = self.data_panel
        prop_guids = [p["guid"] for p in self.model._properties]
        if not (guids and prop_guids):
            return
        rid = f"refresh:{panel.window_id}"
        self._refresh_requests[rid] = panel
        append_log(f"[REFRESH] request {len(guids)} elements x {len(prop_guids)} props")
        self.send_to_ac_async({"command": "get_values", "reqId": rid,
                               "guids": list(guids), "propGuids": prop_guids})

    def _on_refreshed_values(self, panel, data):
        if panel not in self._panels.values():   # 応答前にウィンドウが閉じられた
            return
        self.activate_panel(panel)
        v, s = {}, {}
        known = {e["guid"] for e in self.model._elements}
        for item in data.get("values", []):
            g = item.get("guid")
            if g not in known:
                continue
            s[g] = item.get("modiStamp", 0)
            for pg, val in item.get("props", {}).items():
                v[(g, pg)] = val
        self.model.merge_values(v, s)
        self.table.viewport().update()
        self.status_label.setText(f"反映後の最新値を取得しました（自動計算の列を更新: {len(s)} 要素）")
        append_log(f"[REFRESH] merged {len(v)} cells / {len(s)} elements")
        try:
            self.db.upsert_values(v, s)
        except Exception as e:
            append_log(f"[DB ERROR] refresh upsert_values: {e}")

    def _apply_enum_delegates(self):
        for col_idx, prop in enumerate(getattr(self, "current_props", [])):
            view_col = col_idx + 1
            enums    = prop.get("enums", [])
            delegate = EnumComboBoxDelegate(enums, self.table) if enums else PlaceholderClearDelegate(self.table)
            self.table.setItemDelegateForColumn(view_col, delegate)

    # ── ⑥ 差分ダイアログ ────────────────────────────────────────────

    def show_diff_dialog(self):
        try:
            guid    = self._selected_guids[0] if self._selected_guids else None
            # 最新 500 件を降順（新→古）で取得
            history = self.db.get_history(element_guid=guid, limit=500, ascending=False)
            dlg     = DiffDialog(history, element_guid=guid or "", db=self.db, parent=self)
            dlg.exec()
        except Exception as e:
            QMessageBox.warning(self, "エラー", f"差分取得失敗: {e}")
            append_log(f"[DIFF ERROR] {e}")

    # ── Undo / Redo ─────────────────────────────────────────────────

    def _do_undo(self):
        self.undo_stack.undo()
        self.model.refresh_view()
        self.table.viewport().update()

    def _do_redo(self):
        self.undo_stack.redo()
        self.model.refresh_view()
        self.table.viewport().update()

    def _on_table_double_clicked(self, index):
        """ダブルクリックで編集を起動（EditTrigger.NoEditTriggers を補完）"""
        if index.column() == 0:
            return
        append_log(f"[DBLCLICK] proxy row={index.row()} col={index.column()}")
        self.table.edit(index)

    # ── テーブルクリック・ArchiCAD 選択連動 ─────────────────────────

    def on_table_clicked(self, index):
        try:
            source_index = self.proxy.mapToSource(index)
            row = source_index.row()
            append_log(f"on_table_clicked: proxy row={index.row()} → source row={row}, total_elems={len(self.model._elements)}")
            if row < 0 or row >= len(self.model._elements):
                return
            guid = self.model._elements[row]["guid"]

            self._selected_guids = [guid]
            self.model.set_selected_guid(guid)
            self.table.viewport().update()

            elem_id = next(
                (str(self.model._values.get((guid, p["guid"]), ""))
                 for p in self.model._properties if "element_id" in p.get("guid", "")),
                guid[:8],
            ) or guid[:8]
            self.status_label.setText(f"選択中: {elem_id}  ({guid[:8]}...)")

            self.btn_confirm.setEnabled(True)
            self.btn_reset_status.setEnabled(True)
            self.btn_diff.setEnabled(True)

            self.send_to_ac_async({"command": "select_elements", "guids": [guid]})
            if self.btn_zoom_toggle.isChecked():
                zoom_factor = self.zoom_slider.value() / 10.0
                self.send_to_ac_async({"command": "zoom_to_element", "guids": [guid], "zoomFactor": zoom_factor})
        except Exception as e:
            append_log(f"on_table_clicked ERROR: {e}\n{traceback.format_exc()}")

    def on_archicad_selection_changed(self, payload):
        try:
            guid = payload.get("guid", "")
            if not guid:
                return
            # その要素を表示しているウィンドウ（作業中のウィンドウを優先）を対象にする
            panels = [self._active_panel] + [p for p in self._panels.values() if p is not self._active_panel]
            target = next((p for p in panels if p is not None
                           and any(e["guid"] == guid for e in p.model._elements)), None)
            if target is not None:
                self.activate_panel(target)
            row = next((i for i, e in enumerate(self.model._elements) if e["guid"] == guid), -1)
            if row < 0:
                append_log(f"on_archicad_selection_changed: guid={guid} not found in table")
                return
            append_log(f"on_archicad_selection_changed: guid={guid[:8]} → row={row}")
            self._selected_guids = [guid]
            self.model.set_selected_guid(guid)
            self.table.viewport().update()
            proxy_index = self.proxy.mapFromSource(self.model.index(row, 0))
            if proxy_index.isValid():
                self.table.scrollTo(proxy_index, QAbstractItemView.ScrollHint.EnsureVisible)
            elem_id = next(
                (str(self.model._values.get((guid, p["guid"]), ""))
                 for p in self.model._properties if "element_id" in p.get("guid", "")),
                guid[:8],
            ) or guid[:8]
            self.status_label.setText(f"[AC選択] {elem_id}  ({guid[:8]}...)")
            self.btn_confirm.setEnabled(True)
            self.btn_reset_status.setEnabled(True)
            self.btn_diff.setEnabled(True)
        except Exception as e:
            append_log(f"on_archicad_selection_changed ERROR: {e}\n{traceback.format_exc()}")

    # ── Excel 風の表操作（検索・並び替え・コピー/貼り付け・一括入力）──────

    def _update_row_count(self, *args):
        self.row_count_label.setText(f"表示 {self.proxy.rowCount()} / 全 {self.model.rowCount()} 件")

    def on_search_changed(self, text):
        self.proxy.set_search_text(text)
        self._update_row_count()

    def clear_sort(self):
        """並び替えを解除して ArchiCAD から取得した順に戻す。"""
        self.table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.proxy.sort(-1)

    def _selected_proxy_indexes(self):
        idxs = self.table.selectionModel().selectedIndexes()
        if not idxs:
            cur = self.table.currentIndex()
            idxs = [cur] if cur.isValid() else []
        return sorted(idxs, key=lambda i: (i.row(), i.column()))

    def copy_selection(self):
        """選択セルをタブ区切りでクリップボードへ（Excel にそのまま貼り付け可能）。"""
        idxs = self._selected_proxy_indexes()
        if not idxs:
            return
        rows = sorted({i.row() for i in idxs})
        cols = sorted({i.column() for i in idxs})
        sel  = {(i.row(), i.column()) for i in idxs}
        grid = [[self.proxy.index(r, c).data() if (r, c) in sel else "" for c in cols] for r in rows]
        QApplication.clipboard().setText(table_tools.to_tsv(grid))
        self.status_label.setText(f"コピー: {len(idxs)} セル")

    def paste_clipboard(self):
        """クリップボード（Excel 等のタブ区切り）を選択位置から貼り付け。

        値が 1 つで複数セル選択中なら、選択セルすべてに同じ値を入れる（Excel と同じ）。
        """
        grid = table_tools.parse_tsv(QApplication.clipboard().text())
        idxs = self._selected_proxy_indexes()
        if not grid or not idxs:
            return
        if len(grid) == 1 and len(grid[0]) == 1 and len(idxs) > 1:
            targets = [(i.row(), i.column(), grid[0][0]) for i in idxs]
        else:
            r0 = min(i.row() for i in idxs)
            c0 = min(i.column() for i in idxs)
            targets = [
                (r0 + dr, c0 + dc, val)
                for dr, line in enumerate(grid) for dc, val in enumerate(line)
                if r0 + dr < self.proxy.rowCount() and c0 + dc < self.proxy.columnCount()
            ]
        self._apply_edits(targets, "貼り付け")

    def fill_down(self):
        """各列で選択範囲の先頭セルの値を下のセルへコピー（Excel の Ctrl+D）。"""
        by_col = {}
        for i in self._selected_proxy_indexes():
            by_col.setdefault(i.column(), []).append(i.row())
        targets = []
        for c, rows in by_col.items():
            rows.sort()
            top = self.proxy.index(rows[0], c).data()
            targets += [(r, c, top) for r in rows[1:]]
        if not targets:
            self.status_label.setText("下方向へコピー: 同じ列で 2 セル以上を選択してください")
            return
        self._apply_edits(targets, "下方向へコピー")

    def bulk_input(self):
        """選択セルに同じ値をまとめて入力。列挙型の列だけを選んでいる場合は候補から選ぶ。"""
        idxs = [i for i in self._selected_proxy_indexes() if i.column() > 0]
        if not idxs:
            return
        cols  = {i.column() for i in idxs}
        prop  = self.model.prop_at(next(iter(cols))) if len(cols) == 1 else None
        enums = (prop or {}).get("enums") or []
        title, label = "一括入力", f"{len(idxs)} セルに設定する値:"
        if enums:
            value, ok = QInputDialog.getItem(self, title, label, enums, 0, False)
        else:
            value, ok = QInputDialog.getText(self, title, label)
        if ok:
            self._apply_edits([(i.row(), i.column(), value) for i in idxs], "一括入力")

    def _apply_edits(self, proxy_targets, label):
        """(表示上の行, 列, 値) を元モデルの位置に変換してから反映する。"""
        # 並び替え中は編集で行が移動するため、編集前にすべて変換しておく
        src = []
        for r, c, v in proxy_targets:
            si = self.proxy.mapToSource(self.proxy.index(r, c))
            if si.isValid():
                src.append((si.row(), si.column(), v))
        return self._apply_source_edits(src, label)

    def _apply_source_edits(self, targets, label, warn=True):
        """PropertyDefinition（editable / enums）で検証し、変更のあるセルだけを
        1 回の Undo 操作としてまとめて反映する。戻り値: (変更セル数, スキップ理由リスト)。"""
        valid, skipped = [], []
        for row, col, value in targets:
            prop = self.model.prop_at(col)
            if prop is None:
                skipped.append("ID 列は編集できません")
                continue
            guid = self.model._elements[row]["guid"]
            current = str(self.model._values.get((guid, prop["guid"]), ""))
            ok, reason = table_tools.validate_value(prop, value, current)
            if not ok:
                skipped.append(reason)
                continue
            value = table_tools.normalize_value(prop, value)   # 入力元: 600mm → 0.600 など
            if current != value:
                valid.append((row, col, value))

        if valid:
            self.undo_stack.beginMacro(f"{label} ({len(valid)} セル)")
            try:
                for row, col, value in valid:
                    self.model.setData(self.model.index(row, col), value)
            finally:
                self.undo_stack.endMacro()
            self.table.viewport().update()

        msg = f"{label}: {len(valid)} セル変更"
        if skipped:
            msg += f" / {len(skipped)} セルはスキップ"
        self.status_label.setText(msg)
        if skipped and warn:
            uniq = list(dict.fromkeys(skipped))
            more = f"\n… 他 {len(uniq) - 10} 種類" if len(uniq) > 10 else ""
            QMessageBox.warning(self.data_panel, f"{label} - スキップしたセル",
                                f"{msg}\n\n" + "\n".join(uniq[:10]) + more)
        append_log(f"[TABLE EDIT] {msg}")
        return len(valid), skipped

    def show_cell_menu(self, pos):
        menu = QMenu(self.table)
        for text, slot in (("コピー\tCtrl+C", self.copy_selection),
                           ("貼り付け\tCtrl+V", self.paste_clipboard),
                           ("下方向へコピー\tCtrl+D", self.fill_down),
                           ("選択セルに一括入力...", self.bulk_input),
                           ("検索と置換...\tCtrl+H", self.show_replace_dialog)):
            menu.addAction(text).triggered.connect(slot)
        menu.addSeparator()
        menu.addAction("並び替え解除").triggered.connect(self.clear_sort)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # ── 敷地情報（敷地マネージャー）────────────────────────────────────

    SITE_SUMMARY_TIMEOUT_MS = 15000

    def request_site_summary(self):
        self.condition_status_label.setText("敷地情報を取得中...")
        self._site_request_id = getattr(self, "_site_request_id", 0) + 1
        req = self._site_request_id
        self._site_pending = req
        self.set_button_busy(self.btn_site_summary)
        self.send_to_ac_async({"command": "get_site_summary"})
        # 応答が来ないまま「取得中」で止まらないよう時間切れを設ける
        QTimer.singleShot(self.SITE_SUMMARY_TIMEOUT_MS, lambda: self._on_site_summary_timeout(req))

    def _on_site_summary_timeout(self, req):
        if getattr(self, "_site_pending", None) != req:
            return  # 既に応答済み、または新しい要求に置き換わった
        self._site_pending = None
        self.clear_button_busy(self.btn_site_summary)
        self.condition_status_label.setText("敷地情報: ArchiCAD から応答がありません")
        append_log("[SITE SUMMARY] timeout (no response from add-on)")
        QMessageBox.warning(
            self, "敷地情報",
            "ArchiCAD のアドオンから応答がありませんでした。\n\n"
            "考えられる原因:\n"
            "・ArchiCAD が古いアドオン（敷地情報に未対応）を読み込んでいる\n"
            "  → ArchiCAD を再起動して、新しいアドオンを読み込んでください\n"
            "・ArchiCAD がダイアログ表示中・処理中で応答できない\n"
            "  → ArchiCAD の操作を終えてから［敷地情報］を押し直してください")

    def on_site_summary(self, data):
        self._site_pending = None
        self.clear_button_busy(self.btn_site_summary)
        try:
            summary = site_summary.build_site_summary(data)
        except Exception as e:
            append_log(f"[SITE SUMMARY ERROR] {e}\n{traceback.format_exc()}")
            QMessageBox.warning(self, "敷地情報", f"敷地情報の解析に失敗しました:\n{e}")
            return
        if not summary["sites"] and not summary["roads"]:
            self.condition_status_label.setText("敷地マネージャーの敷地オブジェクトが見つかりません")
        else:
            self.condition_status_label.setText("敷地情報 取得完了")
        if self._site_dlg is None:
            self._site_dlg = SiteSummaryDialog(self, parent=self)
            self._track_window("site_summary", self._site_dlg)
        self._site_dlg.set_summary(summary)
        self._site_dlg.show()
        self._site_dlg.raise_()
        append_log(f"[SITE SUMMARY] sites={len(summary['sites'])} roads={len(summary['roads'])}")

    # ── プロジェクト情報の更新（敷地情報の Excel 取込）──────────────────

    def set_project_info(self, changes):
        """changes: [{"key", "new", ...}]。キー・値は 16 進（UTF-8）で送る（任意の文字を安全に渡すため）。"""
        self.send_to_ac_async({
            "command":   "set_project_info",
            "keysHex":   [schedules.to_hex(c["key"]) for c in changes],
            "valuesHex": [schedules.to_hex(str(c["new"])) for c in changes],
        })
        append_log(f"[PROJECT INFO] update {len(changes)} items")

    def on_project_info_result(self, data):
        ok, failed = data.get("set", 0), data.get("failed", [])
        parent = self._site_dlg if self._site_dlg is not None else self
        if failed:
            QMessageBox.warning(parent, "プロジェクト情報の更新",
                                f"{ok} 件を更新しました。\n更新できなかった項目（Archicad が変更を許可しない項目など）:\n  "
                                + "\n  ".join(failed[:10]))
        else:
            QMessageBox.information(parent, "プロジェクト情報の更新", f"{ok} 件を Archicad に書き込みました。")
        self.request_site_summary()   # 最新の値で敷地情報を表示し直す

    # ── 連動一覧表から取得（列の解決 → 要素 → 値 → 条件で絞り込み → 表示）────

    SCHEDULE_ELEMENT_LIMIT = 20000

    @staticmethod
    def schedule_folder():
        """Archicad が書き出した連動一覧表フォルダ（BIM_SCHEDULE_DIR で差し替え可能）。"""
        return os.environ.get("BIM_SCHEDULE_DIR") or schedules.find_schedule_folder()

    def show_schedule_list(self):
        if self._schedule_dlg is None:
            self._schedule_dlg = ScheduleListDialog(self, parent=self)
            self._track_window("schedules", self._schedule_dlg)
        else:
            self._schedule_dlg.reload()
        self._schedule_dlg.show()
        self._schedule_dlg.raise_()
        return self._schedule_dlg

    def _schedule_status(self, text):
        self.condition_status_label.setText(text)
        if self._schedule_dlg is not None:
            self._schedule_dlg.info_label.setText(text)

    def open_schedule(self, sched):
        key = "schedule:" + sched["file"]
        existing = next((p for p in self._panels.values() if p.window_key == key), None)
        if existing is not None or self.db.find_window(key) is not None:
            QMessageBox.information(
                self, "一覧表",
                f"一覧表「{sched['name']}」のウィンドウは既に開いています。\n"
                "そのウィンドウを前面に表示します。\n\n"
                "最新の値を取り直す場合は、そのウィンドウを閉じてから開き直してください。")
            if existing is not None:
                self.activate_panel(existing)
                existing.show(); existing.raise_(); existing.activateWindow()
            return None
        if any(j["key"] == key for j in self._schedule_jobs.values()):
            self._schedule_status(f"一覧表「{sched['name']}」は取得中です")
            return None
        refs, layers = schedules.required_refs(sched)
        job = {"id": "sched:" + str(uuid.uuid4()), "key": key, "sched": sched, "stage": "resolve"}
        self._schedule_jobs[job["id"]] = job
        guids = [r for r in refs if "||" not in r]
        names = [schedules.to_hex(r) for r in refs if "||" in r]
        self._schedule_status(f"一覧表「{sched['name']}」: 列の定義を取得中...")
        self.set_button_busy(self.btn_schedules)
        append_log(f"[SCHEDULE] open {sched['name']} refs={len(refs)} layers={layers}")
        self.send_to_ac_async({"command": "resolve_schedule", "reqId": job["id"],
                               "propGuids": guids, "propNamesHex": names, "layers": layers})
        return job

    def on_schedule_resolved(self, data):
        job = self._schedule_jobs.get(data.get("reqId"))
        if job is None:
            return
        sched = job["sched"]
        resolved = {d["ref"].upper() if "||" not in d["ref"] else d["ref"]: d for d in data.get("definitions", [])}
        job["columns"], job["missing"] = schedules.build_columns(sched, resolved)
        job["ctx"] = {"layers": data.get("layers", {}),
                      "ref_to_key": {ref: d["guid"] for ref, d in resolved.items()}}
        if not job["columns"]:
            del self._schedule_jobs[job["id"]]
            self.clear_button_busy(self.btn_schedules)
            self._schedule_status(f"一覧表「{sched['name']}」: 取得できる列がありません")
            return
        types, _unknown = schedules.schedule_types(sched)
        job["stage"] = "elements"
        self._schedule_status(f"一覧表「{sched['name']}」: 対象要素を取得中...")
        self.send_to_ac_async({"command": "get_elements", "reqId": job["id"], "stories": [],
                               "limit": self.SCHEDULE_ELEMENT_LIMIT, **{t: True for t in types}})

    def _schedule_elements(self, job, data):
        sched = job["sched"]
        job["elements"] = data.get("elements", [])
        job["capped"] = bool(data.get("capped"))
        if not job["elements"]:
            del self._schedule_jobs[job["id"]]
            self.clear_button_busy(self.btn_schedules)
            self._schedule_status(f"一覧表「{sched['name']}」: 対象の要素がありません")
            return
        # 列の値 ＋ 条件の判定に使う値（プロパティ / GDL / 配置フロア番号）
        keys = [c["guid"] for c in job["columns"]]
        for c in sched["criteria"]:
            if not schedules.criterion_supported(c):
                continue
            if c["param_type"] == schedules.PT_GDL:
                k = "gdl:" + c["gdl_var"]
            elif c["param_type"] == schedules.PT_PROPERTY:
                ref = schedules.prop_ref(c)
                k = "elem:floorIndex" if ref == schedules.HOME_STORY_PROP else job["ctx"]["ref_to_key"].get(ref)
            else:
                k = None
            if k and k not in keys:
                keys.append(k)
        job["stage"] = "values"
        self._schedule_status(f"一覧表「{sched['name']}」: {len(job['elements'])} 件の値を取得中...")
        self.send_to_ac_async({"command": "get_values", "reqId": job["id"],
                               "guids": [e["guid"] for e in job["elements"]], "propGuids": keys})

    def _schedule_values(self, job, data):
        del self._schedule_jobs[job["id"]]
        self.clear_button_busy(self.btn_schedules)
        sched = job["sched"]
        by_guid = {item["guid"]: item for item in data.get("values", [])}
        elements, rows = [], []
        for e in job["elements"]:
            item = by_guid.get(e["guid"])
            if item is None:
                continue
            if schedules.evaluate(sched["criteria"], e, item.get("props", {}), job["ctx"]):
                elements.append(e)
                col_keys = {c["guid"] for c in job["columns"]}
                rows.append({"guid": e["guid"], "modiStamp": item.get("modiStamp", 0),
                             "props": {k: v for k, v in item.get("props", {}).items() if k in col_keys}})
        if not elements:
            self._schedule_status(f"一覧表「{sched['name']}」: 条件に合う要素がありません")
            return
        # Archicad と同じ行の並び（列を左から順に昇順/降順）
        rows = schedules.sort_rows(rows, schedules.sort_specs(sched, job["columns"]), table_tools.sort_key)
        order = {r["guid"]: i for i, r in enumerate(rows)}
        elements.sort(key=lambda e: order[e["guid"]])

        panel = self._acquire_panel()
        panel.elements      = elements
        panel.condition_id  = None
        panel.current_props = job["columns"]
        panel.window_key    = job["key"]
        title = (f"一覧表: {sched['name']}  #{panel.seq}  "
                 f"({len(elements)}件 × {len(job['columns'])}列)")
        panel.setWindowTitle(title)
        self.db.open_window(panel.window_id, job["key"], title, None, [], {},
                            [e["guid"] for e in elements], [c["guid"] for c in job["columns"]])
        self.activate_panel(panel)
        self._display_values({"values": rows})
        notes = []
        unsupported = schedules.summary(sched)["unsupported"]
        if unsupported:
            notes.append("未対応の条件（絞り込みに未使用）: " + " / ".join(unsupported))
        if job["missing"]:
            notes.append("取得できなかった列: " + "、".join(job["missing"]))
        if job.get("capped"):
            notes.append(f"要素が多いため {self.SCHEDULE_ELEMENT_LIMIT} 件で打ち切り")
        self.status_label.setText(f"一覧表「{sched['name']}」: {len(elements)} 件"
                                  + ("  ※" + "  ※".join(notes) if notes else ""))
        self._schedule_status(f"一覧表「{sched['name']}」: {len(elements)} 件を表示しました")
        append_log(f"[SCHEDULE] shown {sched['name']} elements={len(elements)}/{len(job['elements'])} "
                   f"cols={len(job['columns'])} notes={notes}")

    # ── 検索と置換 ───────────────────────────────────────────────────

    def show_replace_dialog(self):
        if getattr(self, "_replace_dlg", None) is None:
            self._replace_dlg = ReplaceDialog(self, parent=self.data_panel)
            self._replace_dlg._panel = self.data_panel   # どのデータウィンドウの置換か
            self._track_window("replace", self._replace_dlg)
        if self.search_edit.text() and not self._replace_dlg.find_edit.text():
            self._replace_dlg.find_edit.setText(self.search_edit.text())
        self._replace_dlg.show()
        self._replace_dlg.raise_()
        self._replace_dlg.activateWindow()
        self._replace_dlg.find_edit.setFocus()
        return self._replace_dlg

    def _find_replace_cells(self, opts):
        """置換条件に一致するセルを探す。

        戻り値: (一致セル[(表示行, 列)], 置換対象[(表示行, 列, 新しい値)], 編集不可で対象外の数)
        不正な正規表現は警告を出して None。
        """
        if opts.get("selection_only"):
            cells = [(i.row(), i.column()) for i in self._selected_proxy_indexes()]
        else:
            cells = [(r, c) for r in range(self.proxy.rowCount()) for c in range(self.proxy.columnCount())]
        matches, targets, locked = [], [], 0
        try:
            for r, c in cells:
                new = table_tools.replace_text(
                    self.proxy.index(r, c).data(), opts["find"], opts["replace"],
                    match_case=opts["match_case"], whole_cell=opts["whole_cell"], regex=opts["regex"])
                if new is None:
                    continue
                matches.append((r, c))
                prop = self.model.prop_at(c)
                if prop is None or not table_tools.is_editable(prop):
                    locked += 1          # ID 列・編集不可プロパティは置換しない
                else:
                    targets.append((r, c, new))
        except ValueError as e:
            QMessageBox.warning(self.data_panel, "検索と置換", str(e))
            return None
        return matches, targets, locked

    def select_matches(self, opts):
        """一致セルを選択状態にする（置換前の確認用）。一致数を返す。"""
        found = self._find_replace_cells(opts)
        if found is None:
            return None
        matches = found[0]
        sel = QItemSelection()
        for r, c in matches:
            idx = self.proxy.index(r, c)
            sel.select(idx, idx)
        sm = self.table.selectionModel()
        if matches:
            sm.setCurrentIndex(self.proxy.index(*matches[0]), QItemSelectionModel.SelectionFlag.NoUpdate)
            self.table.scrollTo(self.proxy.index(*matches[0]))
        sm.select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self.status_label.setText(f"検索: {len(matches)} セル一致")
        return len(matches)

    def replace_all(self, opts):
        """一致セルをすべて置換（検証付き・Undo 1 回で元に戻せる）。

        戻り値: (置換セル数, 検証でスキップした数, 編集不可で対象外の数)
        """
        found = self._find_replace_cells(opts)
        if found is None:
            return None
        matches, targets, locked = found
        if not matches:
            self.status_label.setText("置換: 一致するセルはありません")
            return 0, 0, 0
        changed, skipped = self._apply_edits(targets, "置換")
        if locked:
            self.status_label.setText(self.status_label.text() + f" / 編集不可で対象外 {locked} セル")
        return changed, len(skipped), locked

    # ── Excel 入出力 ─────────────────────────────────────────────────

    # ── Excel 出力の設定（GUID 列を編集不可 / 非表示。既定はどちらも ON）──────

    def excel_option(self, key):
        val = self._settings().value(key, True)
        return val if isinstance(val, bool) else str(val).lower() in ("true", "1")

    def set_excel_option(self, key, on):
        s = self._settings()
        s.setValue(key, bool(on))
        s.sync()

    def _sync_excel_option_actions(self, panel):
        for act, key in ((panel.act_lock_guid, EXCEL_LOCK_GUID_KEY), (panel.act_hide_guid, EXCEL_HIDE_GUID_KEY)):
            act.blockSignals(True)
            act.setChecked(self.excel_option(key))
            act.blockSignals(False)

    # ── Excel 共通（保存先の記憶・エラーの分かりやすい表示）──────────────

    def excel_save_path(self, parent, title, default_name):
        """保存先を選ぶ。既定名はウィンドウ名から作り、前回のフォルダを開く。キャンセルは None。"""
        s = self._settings()
        start = os.path.join(s.value("excel/last_dir", "") or "", table_tools.safe_file_name(default_name))
        path, _ = QFileDialog.getSaveFileName(parent, title, start, "Excel ファイル (*.xlsx)")
        if not path:
            return None
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        s.setValue("excel/last_dir", os.path.dirname(path))
        return path

    def excel_open_path(self, parent, title):
        s = self._settings()
        path, _ = QFileDialog.getOpenFileName(parent, title, s.value("excel/last_dir", "") or "",
                                              "Excel ファイル (*.xlsx)")
        if not path:
            return None
        s.setValue("excel/last_dir", os.path.dirname(path))
        return path

    @staticmethod
    def excel_error_text(e, writing):
        if isinstance(e, PermissionError):
            return ("ファイルが Excel などで開かれているため" + ("書き込めません" if writing else "読み込めません")
                    + "。\nExcel でそのファイルを閉じてから、もう一度実行してください。")
        return f"{'書き出し' if writing else '読み込み'}に失敗しました:\n{e}"

    def export_excel(self):
        """表示中（検索・フィルタ・並び替え後）の行を .xlsx に書き出す。未反映の変更は赤塗り。"""
        if not self.model._elements:
            QMessageBox.information(self.data_panel, "Excel出力", "出力するデータがありません。")
            return
        path = self.excel_save_path(self.data_panel, "Excel出力",
                                    self.data_panel.windowTitle().split("  (")[0] or "BIMデータ")
        if not path:
            return
        props = self.model._properties
        rows = []
        for r in range(self.proxy.rowCount()):
            g = self.model._elements[self.proxy.mapToSource(self.proxy.index(r, 0)).row()]["guid"]
            rows.append((g, [self.model._values.get((g, p["guid"]), "") for p in props]))
        changed = {k for k, v in self.model._values.items()
                   if str(v) != str(self.model._original_values.get(k))}
        lock_guid = self.excel_option(EXCEL_LOCK_GUID_KEY)
        hide_guid = self.excel_option(EXCEL_HIDE_GUID_KEY)
        try:
            table_tools.export_xlsx(path, props, rows, changed, lock_guid=lock_guid, hide_guid=hide_guid)
            written = table_tools.count_data_rows(path)   # 書いたファイルを読み直して確認
        except Exception as e:
            append_log(f"[EXCEL EXPORT ERROR] {e}\n{traceback.format_exc()}")
            QMessageBox.warning(self.data_panel, "Excel出力", self.excel_error_text(e, writing=True))
            return
        if written != len(rows):
            QMessageBox.warning(self.data_panel, "Excel出力",
                                f"書き出した行数が一致しません（表示 {len(rows)} 行 / ファイル {written} 行）。\n{path}")
            return
        hidden = len(self.model._elements) - len(rows)
        note = f"（検索・フィルタで非表示の {hidden} 行は出力していません）" if hidden else ""
        guid_notes = []
        if lock_guid:
            guid_notes.append("GUID 列と見出し行は編集不可（シート保護・パスワードなし）")
        if hide_guid:
            guid_notes.append("GUID 列は非表示")
        if guid_notes:
            note += "\n" + "・".join(guid_notes) + "（［Excel出力設定］で変更できます）"
        self.status_label.setText(f"Excel出力: {len(rows)} 行 × {len(props)} 列 → {path}")
        QMessageBox.information(self.data_panel, "Excel出力",
                                f"{len(rows)} 行 × {len(props)} 列を書き出しました。{note}\n{path}\n\n"
                                "編集後は［Excel取込］で読み込めます（GUID 列と見出しはそのままにしてください）。")
        append_log(f"[EXCEL EXPORT] {len(rows)} rows -> {path}")

    def import_excel(self):
        """Excel で編集した .xlsx を読み込み、現在の表と値が違うセルだけを確認後に反映する（Undo 可）。

        ArchiCAD への反映は従来どおり [Archicadへ反映] で行う（ここでは表を変更するだけ）。
        """
        if not self.model._elements:
            QMessageBox.information(self.data_panel, "Excel取込", "先に [データ取得] で表を表示してください。")
            return
        path = self.excel_open_path(self.data_panel, "Excel取込")
        if not path:
            return
        try:
            values, unknown_headers = table_tools.read_xlsx(path, self.model._properties)
        except Exception as e:
            append_log(f"[EXCEL IMPORT ERROR] {e}\n{traceback.format_exc()}")
            QMessageBox.warning(self.data_panel, "Excel取込", self.excel_error_text(e, writing=False))
            return

        row_of = {e["guid"]: i for i, e in enumerate(self.model._elements)}
        col_of = {p["guid"]: i + 1 for i, p in enumerate(self.model._properties)}
        valid, skipped, missing = [], [], set()
        per_column = {}
        for (g, pg), v in values.items():
            if g not in row_of:
                missing.add(g)
                continue
            current = self.model._values.get((g, pg), "")
            if table_tools.values_equal(current, v):
                continue
            prop = self.model.prop_at(col_of[pg])
            ok, reason = table_tools.validate_value(prop, v, current)
            if not ok:
                skipped.append(reason)
                continue
            v = table_tools.normalize_value(prop, v)
            if table_tools.values_equal(current, v):
                continue
            valid.append((row_of[g], col_of[pg], v))
            per_column[prop.get("name", pg)] = per_column.get(prop.get("name", pg), 0) + 1

        notes = []
        if skipped:
            uniq = list(dict.fromkeys(skipped))
            notes.append(f"取り込めないセル: {len(skipped)}（編集不可・候補外・対象外・値の形式）\n  " + "\n  ".join(uniq[:8])
                         + (f"\n  … 他 {len(uniq) - 8} 種類" if len(uniq) > 8 else ""))
        if missing:
            notes.append(f"表に無い要素: {len(missing)} 行（無視します）")
        if unknown_headers:
            notes.append("対応するプロパティが無い列: " + ", ".join(unknown_headers[:5]))
        note_txt = ("\n\n" + "\n".join(notes)) if notes else ""

        if not valid:
            QMessageBox.information(self.data_panel, "Excel取込", "表と違う値（変更）はありませんでした。" + note_txt)
            self.status_label.setText("Excel取込: 変更なし")
            return
        detail = "\n".join(f"  {name}: {n} セル" for name, n in list(per_column.items())[:12])
        if QMessageBox.question(
                self.data_panel, "Excel取込の確認",
                f"{len(valid)} セルを表に反映します（赤字になります。Undo で戻せます）。\n{detail}{note_txt}\n\n"
                "反映しますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            self.status_label.setText("Excel取込: 中止しました")
            return
        changed, _ = self._apply_source_edits(valid, "Excel取込", warn=False)
        QMessageBox.information(
            self.data_panel, "Excel取込",
            f"{changed} セルを変更しました（赤字）。\n[Archicadへ反映] で ArchiCAD に書き込みます。")
        append_log(f"[EXCEL IMPORT] changed={changed} skipped={len(skipped)} missing={len(missing)}")
        append_log(f"[EXCEL IMPORT] changed={changed} skipped={len(skipped)} {notes}")

    # ── ズーム ON/OFF ────────────────────────────────────────────────

    def _on_zoom_toggle(self, checked: bool):
        self.btn_zoom_toggle.setText("ズーム ON" if checked else "ズーム OFF")
        self.zoom_slider.setEnabled(checked)
        self.zoom_factor_label.setEnabled(checked)

    def zoom_fit_all(self):
        self.send_to_ac_async({"command": "zoom_fit_all"})
        append_log("zoom_fit_all sent")

    # ── フィルタメニュー ────────────────────────────────────────────

    def show_filter_menu(self, pos):
        col = self.table.horizontalHeader().logicalIndexAt(pos)
        if col < 0:
            return
        menu     = QMenu(self)
        num_menu = menu.addMenu("数値フィルタ")
        act_gte       = QAction("≧ 条件",   self)
        act_lte       = QAction("≦ 条件",   self)
        act_eq        = QAction("＝ 条件",   self)
        act_range     = QAction("範囲指定",  self)
        act_clear_num = QAction("数値フィルタ解除", self)
        num_menu.addActions([act_gte, act_lte, act_eq, act_range])
        num_menu.addSeparator()
        num_menu.addAction(act_clear_num)

        def set_gte():
            v, ok = QInputDialog.getDouble(self, "≧条件", "値")
            if ok: self.proxy.set_numeric_filter(col, ("gte", v))
        def set_lte():
            v, ok = QInputDialog.getDouble(self, "≦条件", "値")
            if ok: self.proxy.set_numeric_filter(col, ("lte", v))
        def set_eq():
            v, ok = QInputDialog.getDouble(self, "＝条件", "値")
            if ok: self.proxy.set_numeric_filter(col, ("eq", v))
        def set_range():
            minv, ok1 = QInputDialog.getDouble(self, "最小値", "min")
            if not ok1: return
            maxv, ok2 = QInputDialog.getDouble(self, "最大値", "max")
            if ok2: self.proxy.set_numeric_filter(col, ("range", minv, maxv))

        act_gte.triggered.connect(set_gte)
        act_lte.triggered.connect(set_lte)
        act_eq.triggered.connect(set_eq)
        act_range.triggered.connect(set_range)
        act_clear_num.triggered.connect(lambda: self.proxy.clear_numeric_filter(col))

        menu.addSeparator()
        values  = sorted({str(self.model.index(r, col).data()) for r in range(self.model.rowCount())})
        actions = []
        act_all = QAction("すべて選択", menu)
        act_all.triggered.connect(lambda: [a.setChecked(True) for _, a in actions])
        menu.addAction(act_all)
        menu.addSeparator()
        for v in values:
            act = QAction(v, menu)
            act.setCheckable(True)
            curr = self.proxy._filters.get(col)
            act.setChecked(curr is None or v in curr)
            menu.addAction(act)
            actions.append((v, act))
        menu.aboutToHide.connect(
            lambda: self.proxy.set_value_filter(col, [v for v, a in actions if a.isChecked()])
        )
        menu.exec(self.table.horizontalHeader().mapToGlobal(pos))

    # ── 競合フラグ操作 ───────────────────────────────────────────────

    def clear_value_conflict_flags(self):
        flag_guids = list(self.model._value_conflict_guids)
        if flag_guids:
            self._send_owned("flag", {
                "command":  "clear_change_flags",
                "guids":    flag_guids,
                "propName": "変更フラグ",
            })
            # ⑦ DB 上の競合フラグもクリア
            try:
                self.db.clear_conflict_flags(flag_guids)
                append_log(f"[DB] conflict flags cleared: {len(flag_guids)}")
            except Exception as e:
                append_log(f"[DB ERROR] clear_conflict_flags: {e}")
        self.model._value_conflict_cells.clear()
        self.model._value_conflict_guids.clear()
        self.model.refresh_view()
        self.btn_clear_flags.setEnabled(False)
        self.status_label.setText("フラグ解除中（ArchiCAD含む）...")

    def on_flag_result(self, data):
        self.activate_panel(self._pop_owner("flag"))
        prop_count = data.get("propCount", 0)
        created    = data.get("created", False)
        if data.get("status") == "ok":
            if created:
                self.status_label.setText(f"ArchiCADフラグ: 「変更フラグ」プロパティを自動作成 → {prop_count}件反映")
            elif prop_count > 0:
                self.status_label.setText(f"ArchiCADフラグ: {prop_count}件反映")
            else:
                self.status_label.setText("ArchiCADフラグ解除完了")
        else:
            self.status_label.setText(f"ArchiCADフラグエラー: {data.get('reason', '')}")

    # ── BIM 表現の上書き ─────────────────────────────────────────────

    def toggle_bim_override(self):
        floor = self._last_archicad_floor if self._last_archicad_floor is not None else -1
        append_log(f"[OVERRIDE] floor={floor} active={self._pyqt_active}")
        self.set_button_busy(self.btn_bim_override)
        if self.btn_bim_override.isChecked():
            self.btn_bim_override.setText("変更強調 ON")
            self.status_label.setText("変更強調適用中...")
            self._send_owned("override", {"command": "apply_bim_override", "floor": floor})
        else:
            self.btn_bim_override.setText("変更強調 OFF")
            self.status_label.setText("変更強調解除中...")
            self._send_owned("override", {"command": "remove_bim_override", "floor": floor})

    def on_bim_override_result(self, data):
        self.activate_panel(self._pop_owner("override"))
        self.clear_button_busy(self.btn_bim_override)
        action = data.get("action", "")
        status = data.get("status", "")
        if action == "setup":
            if status == "ok":
                prop_created  = data.get("propCreated",       False)
                combo_created = data.get("combinationCreated", False)
                cs_guid       = data.get("changeStatusGuid",  "")
                parts = ["BIM表現の上書き設定完了"]
                if prop_created:  parts.append("ChangeStatusプロパティ自動作成")
                if combo_created: parts.append("表現の上書き自動作成")
                msg = "（" + " / ".join(parts[1:]) + "）" if len(parts) > 1 else ""
                self.status_label.setText(parts[0] + msg)
                QMessageBox.information(
                    self, "BIM表現の上書き設定完了",
                    f"{parts[0]}{msg}\n\n"
                    "【ArchiCADで1回だけ手動設定が必要】\n"
                    "① 表現の上書き を開く\n"
                    "② 「BIM変更管理」コンビネーション内\n"
                    "   「BIM変更済」ルール → 条件: ChangeStatus = 1（変更済）\n"
                    "   「BIM確認済」ルール → 条件: ChangeStatus = 2（確認済）\n\n"
                    f"ChangeStatus プロパティ GUID:\n{cs_guid}\n\n"
                    "設定後、「変更強調 ON」ボタンでビューに適用できます。",
                )
            else:
                self.status_label.setText(f"BIM表現の上書き設定エラー: {data.get('reason', '')}")
        elif action in ("applied", "removed"):
            view_count = data.get("viewCount", 0)
            if status == "ok":
                label = "ON" if action == "applied" else "OFF"
                self.status_label.setText(f"変更強調 {label}: {view_count}ビュー更新")
            else:
                self.status_label.setText(f"変更強調エラー: {data.get('reason', '')}")
            # BIMオーバーライドが全フロアをループするため、操作前のフロアに戻す
            if self._floor_before_op is not None:
                append_log(f"[FLOOR RESTORE] goto_floor={self._floor_before_op}")
                self.send_to_ac_async({"command": "goto_floor", "floor": self._floor_before_op})
                self._floor_before_op = None

    # ── ChangeStatus 管理 ────────────────────────────────────────────

    def mark_confirmed(self):
        if not self._selected_guids:
            return
        self._pending_status_change = {"guids": list(self._selected_guids), "value": 2}
        self.set_button_busy(self.btn_confirm)
        self._send_owned("status", {"command": "set_change_status", "guids": self._selected_guids, "status": 2})
        self.status_label.setText(f"確認済設定中: {len(self._selected_guids)}件...")

    def reset_change_status(self):
        if not self._selected_guids:
            return
        self._pending_status_change = {"guids": list(self._selected_guids), "value": 0}
        self.set_button_busy(self.btn_reset_status)
        self._send_owned("status", {"command": "set_change_status", "guids": self._selected_guids, "status": 0})
        self.status_label.setText(f"ステータスリセット中: {len(self._selected_guids)}件...")

    def reset_all_table(self):
        guids = [e["guid"] for e in self.model._elements]
        if not guids:
            return
        if QMessageBox.question(
            self, "全件リセット",
            f"テーブルの全 {len(guids)} 件を未変更（ChangeStatus=0）にリセットしますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._pending_status_change = {"guids": list(guids), "value": 0}
        self._pending_bim_restore   = True   # 完了後に表現の上書きセットを解除
        self._floor_before_op       = self._last_archicad_floor  # リセット前フロアを保存
        self.set_button_busy(self.btn_confirm_all)
        self._send_owned("status", {"command": "set_change_status", "guids": guids, "status": 0})
        self.status_label.setText(f"全件リセット中: {len(guids)}件...")

    def on_change_status_result(self, data):
        self.activate_panel(self._pop_owner("status"))
        for btn in (self.btn_confirm, self.btn_reset_status, self.btn_confirm_all):
            self.clear_button_busy(btn)
        try:
            append_log(f"on_change_status_result: {data}")
            if data.get("status") == "ok":
                val   = data.get("value", -1)
                count = data.get("set", 0)
                label_map = {0: "未変更（リセット）", 1: "変更済", 2: "確認済"}
                label = label_map.get(val, str(val))
                if self._pending_status_change:
                    self.model.update_change_status(
                        self._pending_status_change["guids"],
                        self._pending_status_change["value"],
                    )
                    self._pending_status_change = None
                self.status_label.setText(f"ChangeStatus={label}: {count}件 設定完了")
                # 全件リセット完了 → 表現の上書きセットを解除
                if self._pending_bim_restore:
                    self._pending_bim_restore = False
                    floor = self._floor_before_op if self._floor_before_op is not None else self._last_archicad_floor
                    cmd_floor = floor if floor is not None else -1
                    self._send_owned("override", {"command": "remove_bim_override", "floor": cmd_floor})
                    self.btn_bim_override.setChecked(False)
                    self.btn_bim_override.setText("変更強調 OFF")
                    append_log(f"[RESET-ALL] BIM override removed floor={floor}")
            else:
                self._pending_status_change = None
                self._pending_bim_restore   = False
                self.status_label.setText(f"ChangeStatusエラー: {data.get('reason', '')}")
        except Exception as e:
            append_log(f"on_change_status_result ERROR: {e}\n{traceback.format_exc()}")

    # ── 反映（sync）───────────────────────────────────────────────────

    def sync_data(self):
        changes = []
        ps = {"AbortAll (厳密)": 0, "SkipConflicts (属性)": 0, "ForceOverwrite (強制)": 0}
        stale_guids, unknown_guids = [], []
        props = getattr(self, "current_props", [])
        pm    = {p["guid"]: p for p in props}

        for (g, pg), val in self.model._values.items():
            if str(val) != str(self.model._original_values.get((g, pg))):
                pdef = pm.get(pg, {})
                st   = "element_id" if is_element_id_property(pdef, pg) else ""
                gt   = get_group_type(pdef, pg)
                pol  = ("AbortAll"      if gt in ("element", "parameter")
                        else "ForceOverwrite" if st == "element_id" or gt == "element_id"
                        else "SkipConflicts")
                lbl  = ("AbortAll (厳密)"    if pol == "AbortAll"
                        else "SkipConflicts (属性)" if pol == "SkipConflicts"
                        else "ForceOverwrite (強制)")
                ps[lbl] += 1
                try:   stamp = int(self.model._stamps.get(g, 0))
                except: stamp = 0
                flag = self.model._stamp_flags.get(g, PropertyTableModel.STAMP_OK)
                if flag == PropertyTableModel.STAMP_STALE   and g not in stale_guids:   stale_guids.append(g)
                elif flag == PropertyTableModel.STAMP_UNKNOWN and g not in unknown_guids: unknown_guids.append(g)
                changes.append({
                    "guid": g, "group": gt, "propId": pg,
                    "propName": pdef.get("name", ""), "propGroup": pdef.get("group", ""),
                    "specialType": st, "value": str(val), "modiStamp": stamp,
                })

        if not changes:
            QMessageBox.information(self, "情報", "変更箇所がありません。")
            return

        sum_txt  = "\n".join([f"・{k}: {v}件" for k, v in ps.items() if v > 0])
        warn_txt = ""
        if stale_guids:
            warn_txt += (f"\n\n⚠ 【stamp不一致】 ArchiCAD側で変更済みの要素: {len(stale_guids)}件"
                         f"\n　データ取得後にArchiCAD側で変更された可能性があります。")
        if unknown_guids:
            warn_txt += (f"\n\n⚠ 【stamp未確認】: {len(unknown_guids)}件"
                         f"\n　競合チェックが正確に行われない可能性があります。")

        msg = f"以下の内容をArchicadに反映しますか？\n\n【適用ポリシー統計】\n{sum_txt}{warn_txt}"
        if QMessageBox.question(
            self, "反映の確認", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        self._pending_changes     = changes
        self._pre_verify_original = dict(self.model._original_values)
        self._pre_verify_stamps   = dict(self.model._stamps)
        self._verifying_stamps    = True
        self.set_button_busy(self.btn_sync)   # 反映結果（sync_complete）まで
        self._floor_before_op     = self._last_archicad_floor   # 反映前フロアを保存
        self.btn_sync.setEnabled(False)
        changed_guids = list({c["guid"]   for c in changes})
        changed_props = list({c["propId"] for c in changes if c.get("propId")})
        self.status_label.setText("stamp/値を確認中...")
        self._value_requests[self.data_panel.window_id] = self.data_panel
        self.send_to_ac_async({"command": "get_values", "reqId": self.data_panel.window_id,
                               "guids": changed_guids, "propGuids": changed_props})

    def _on_pre_apply_verify(self, data):
        self.btn_sync.setEnabled(True)
        conflicts    = []
        conflict_keys = set()

        for item in data.get("values", []):
            g            = item["guid"]
            fresh_stamp  = item.get("modiStamp", 0)
            old_stamp    = self._pre_verify_stamps.get(g, 0)

            if fresh_stamp:
                for c in self._pending_changes:
                    if c["guid"] == g:
                        c["modiStamp"] = fresh_stamp
                if old_stamp and fresh_stamp != old_stamp:
                    self.model._stamp_flags[g] = PropertyTableModel.STAMP_STALE
                    self.model._stamps[g]       = fresh_stamp

            for pg, fresh_val in item.get("props", {}).items():
                orig_val = self._pre_verify_original.get((g, pg))
                user_val = self.model._values.get((g, pg))
                if orig_val is None or user_val is None:
                    continue
                if str(user_val) == str(orig_val):
                    continue
                if str(fresh_val) != str(orig_val):
                    prop_name = next(
                        (p["name"] for p in self.model._properties if p["guid"] == pg), pg[:8]
                    )
                    conflicts.append({
                        "guid": g, "guid_short": g[:8],
                        "prop_guid": pg, "prop_name": prop_name,
                        "orig_val": str(orig_val),
                        "ac_val":   str(fresh_val),
                        "user_val": str(user_val),
                    })
                    conflict_keys.add((g, pg))

        pending = self._pending_changes
        self._pending_changes     = None
        self._pre_verify_original = {}
        self._pre_verify_stamps   = {}

        if not conflicts:
            append_log(f"[PRE-VERIFY] 競合なし → 反映 {len(pending)} 件")
            self.status_label.setText("反映中...")
            self._send_owned("sync", {"command": "apply_changes", "changes": pending})
            return

        safe_changes     = [c for c in pending if (c["guid"], c["propId"]) not in conflict_keys]
        conflict_changes = [c for c in pending if (c["guid"], c["propId"]) in conflict_keys]

        self.model.refresh_view()
        lines  = [
            f"  [{c['guid_short']}] {c['prop_name']}\n"
            f"    取得時: {c['orig_val']}  ／  ArchiCAD現在: {c['ac_val']}  ／  あなたの編集: {c['user_val']}"
            for c in conflicts[:8]
        ]
        detail = "\n".join(lines)
        extra  = f"\n  … 他 {len(conflicts) - 8} 件" if len(conflicts) > 8 else ""

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("値の競合を検出")
        msg_box.setIcon(QMessageBox.Icon.Warning)
        msg_box.setText(
            f"以下のプロパティがデータ取得後にArchiCAD側で変更されました:\n\n"
            f"{detail}{extra}\n\n"
            f"競合: {len(conflict_changes)}件  ／  競合なし: {len(safe_changes)}件"
        )
        btn_overwrite = msg_box.addButton("全て上書き",                                QMessageBox.ButtonRole.AcceptRole)
        btn_skip      = msg_box.addButton(f"競合をスキップして反映  ({len(safe_changes)}件)", QMessageBox.ButtonRole.NoRole)
        btn_cancel    = msg_box.addButton("全て中止",                                  QMessageBox.ButtonRole.RejectRole)
        msg_box.exec()
        clicked = msg_box.clickedButton()

        if clicked == btn_overwrite:
            append_log(f"[PRE-VERIFY] 全上書き → {len(pending)} 件")
            self.status_label.setText("反映中（全上書き）...")
            self._send_owned("sync", {"command": "apply_changes", "changes": pending})

        elif clicked == btn_skip:
            for cf in conflicts:
                key = (cf["guid"], cf["prop_guid"])
                self.model._value_conflict_cells[key] = {
                    "orig_val": cf["orig_val"],
                    "ac_val":   cf["ac_val"],
                    "user_val": cf["user_val"],
                }
                self.model._value_conflict_guids.add(cf["guid"])
            self.model.refresh_view()
            self.btn_clear_flags.setEnabled(True)
            self._pending_skip_count = len(conflict_changes)

            # ⑦ 競合フラグを DB に永続化
            try:
                db_conflicts = [
                    {"element_guid":  cf["guid"],
                     "property_guid": cf["prop_guid"],
                     "orig_val":      cf["orig_val"],
                     "ac_val":        cf["ac_val"],
                     "user_val":      cf["user_val"]}
                    for cf in conflicts
                ]
                self.db.save_conflict_flags(db_conflicts)
                append_log(f"[DB] conflict flags saved: {len(db_conflicts)}")
            except Exception as e:
                append_log(f"[DB ERROR] save_conflict_flags: {e}")

            flag_guids = list(self.model._value_conflict_guids)
            if flag_guids:
                self._send_owned("flag", {
                    "command":  "mark_change_flags",
                    "guids":    flag_guids,
                    "flag":     "⚠変更あり",
                    "propName": "変更フラグ",
                })
            if safe_changes:
                append_log(f"[PRE-VERIFY] 競合スキップ → safe {len(safe_changes)} 件 / skip {len(conflict_changes)} 件")
                self.status_label.setText(f"競合スキップして反映中... ({len(safe_changes)}件)")
                self._send_owned("sync", {"command": "apply_changes", "changes": safe_changes})
            else:
                self.status_label.setText("反映キャンセル（全て競合）")
                self.clear_button_busy(self.btn_sync)
        else:
            self.status_label.setText("反映を中止しました")
            self.clear_button_busy(self.btn_sync)

    def on_sync_complete(self, data):
        self.activate_panel(self._pop_owner("sync"))
        append_log("=== SYNC COMPLETE RECEIVED ===")
        self.clear_button_busy(self.btn_sync)
        append_log(f"sync payload keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
        append_log(f"sync results count: {len(data.get('results', []))}")
        try:
            if "results" not in data:
                if "data" in data and "results" in data["data"]:
                    data = data["data"]
                else:
                    append_log("NO RESULTS FOUND")
                    QMessageBox.warning(self, "異常", "結果データが取得できません")
                    self.status_label.setText("待機中")
                    return

            results = data.get("results", [])
            sc, cc, ec, stamp_stale_cnt, details = 0, 0, 0, 0, []
            ok_guids = []
            self.model._conflicts.clear()

            for res in results:
                g = res.get("guid")
                if not g: continue
                s            = res.get("status", "error")
                r            = res.get("reason", "")
                current_stamp = res.get("currentStamp", 0)
                prev_stamp    = self.model._stamps.get(g, 0)

                if s == "success":
                    sc += 1
                    ok_guids.append(g)
                    if r:   # 一部の入力元が対象外などで反映されなかった
                        details.append(f"{g[:8]}: {r}")
                    for pg in [p["guid"] for p in self.model._properties]:
                        if (g, pg) in self.model._values:
                            self.model._original_values[(g, pg)] = self.model._values[(g, pg)]
                    if current_stamp:
                        self.model._stamps[g] = current_stamp
                    self.model._stamp_flags[g]  = PropertyTableModel.STAMP_OK
                    self.model._change_status[g] = 1

                elif s in ("conflict", "skipped"):
                    cc += 1
                    self.model._conflicts[g] = s
                    if current_stamp and current_stamp != prev_stamp:
                        self.model._stamp_flags[g] = PropertyTableModel.STAMP_STALE
                        self.model._stamps[g]       = current_stamp
                        stamp_stale_cnt += 1
                        details.append(f"{g[:8]}: {s} ⚠stamp変化({prev_stamp}→{current_stamp}) ({r})")
                    else:
                        details.append(f"{g[:8]}: {s} ({r})")
                else:
                    ec += 1
                    self.model._conflicts[g] = "error"
                    if current_stamp and current_stamp != prev_stamp:
                        self.model._stamp_flags[g] = PropertyTableModel.STAMP_STALE
                        self.model._stamps[g]       = current_stamp
                        stamp_stale_cnt += 1
                        details.append(f"{g[:8]}: ERROR ⚠stamp変化({prev_stamp}→{current_stamp}) ({r})")
                    else:
                        details.append(f"{g[:8]}: ERROR ({r})")

            self.model.refresh_view()
            # 反映した要素の値を取り直し、自動計算（W/L/H/レベル・式のプロパティ等）を最新にする
            self._refresh_after_sync(ok_guids)

            py_skip     = self._pending_skip_count
            self._pending_skip_count = 0
            stamp_txt   = f" / stamp変化:{stamp_stale_cnt}" if stamp_stale_cnt > 0 else ""
            py_skip_txt = f" / 競合スキップ:{py_skip}"      if py_skip > 0         else ""
            msg         = f"成功:{sc} / 競合:{cc} / エラー:{ec}{stamp_txt}{py_skip_txt}"
            self.status_label.setText(msg)
            d_txt     = "\n".join(details[:10])
            has_issue = ec > 0 or cc > 0 or py_skip > 0

            if not has_issue:
                QMessageBox.information(self, "同期完了", f"{msg}\n\n{d_txt}" if d_txt else msg)
            elif ec == 0:
                QMessageBox.warning(self, "一部競合/スキップ", f"{msg}\n\n{d_txt}" if d_txt else msg)
            else:
                QMessageBox.critical(self, "エラー", f"{msg}\n\n{d_txt}")

            # 成功件数があれば BIM変更管理（表現の上書き）を自動適用
            if sc > 0:
                floor = self._floor_before_op if self._floor_before_op is not None else self._last_archicad_floor
                cmd_floor = floor if floor is not None else -1
                self._send_owned("override", {"command": "apply_bim_override", "floor": cmd_floor})
                append_log(f"[APPLY OVERRIDE] floor={cmd_floor}")
                self.btn_bim_override.setChecked(True)
                self.btn_bim_override.setText("変更強調 ON")
                append_log(f"[SYNC] BIM override auto-applied (sc={sc})")
        except Exception as e:
            append_log(f"SYNC ERROR: {e}\n{traceback.format_exc()}")
            QMessageBox.critical(self, "内部エラー", str(e))

    # ── ウィンドウの大きさ・位置の保存と復元 ──────────────────────────

    def _settings(self):
        return QSettings(str(SETTINGS_PATH), QSettings.Format.IniFormat)

    def _track_window(self, key, widget):
        """前回の大きさ・位置を復元し、以後の移動・サイズ変更を自動保存の対象にする。
        データウィンドウは複数あっても同じキー（最後に動かしたものの大きさを次回に使う）。"""
        self._tracked_windows[widget] = key
        geom = self._settings().value(f"geometry/{key}")
        if geom is not None:
            widget.restoreGeometry(geom)
        widget.installEventFilter(self)

    def _untrack_window(self, widget):
        if self._tracked_windows.pop(widget, None) is not None:
            try:
                widget.removeEventFilter(self)
            except RuntimeError:
                pass
        self._shown_windows.discard(widget)

    def _restore_splitter(self):
        state = self._settings().value("splitter/condition")
        if state is not None:
            self.h_split.restoreState(state)
        self.h_split.splitterMoved.connect(lambda *_: self._state_timer.start())

    def eventFilter(self, obj, event):
        tracked = self.__dict__.get("_tracked_windows", {})
        if obj in tracked:
            t = event.type()
            if t == QEvent.Type.Show:
                self._shown_windows.add(obj)
            if t in (QEvent.Type.Resize, QEvent.Type.Move) and obj in self._shown_windows:
                self._last_moved[tracked[obj]] = obj   # 同じキーは最後に動かしたものを保存
                self._state_timer.start()
        return super().eventFilter(obj, event)

    def save_window_state(self):
        """表示したことのあるウィンドウの大きさ・位置と、条件設定の左右分割を保存する。"""
        s = self._settings()
        chosen = {}
        for widget, key in self._tracked_windows.items():
            if widget in self._shown_windows:   # 今回開いていないものは前回値を残す
                chosen.setdefault(key, widget)
        chosen.update({k: w for k, w in self._last_moved.items()
                       if w in self._shown_windows and w in self._tracked_windows})
        for key, widget in chosen.items():
            s.setValue(f"geometry/{key}", widget.saveGeometry())
        if self in self._shown_windows:
            s.setValue("splitter/condition", self.h_split.saveState())
        s.sync()

    # ── フロアポーリング ──────────────────────────────────────────────

    def changeEvent(self, event):
        from PyQt6.QtCore import QEvent
        if event.type() == QEvent.Type.ActivationChange:
            self._pyqt_active = self.isActiveWindow()
        super().changeEvent(event)

    def _start_floor_polling(self):
        import time
        append_log("[FLOOR POLL] polling thread starting")
        def _loop():
            append_log("[FLOOR POLL] thread running")
            while True:
                try:
                    time.sleep(3.0)
                    self._query_and_store_floor()
                except Exception as e:
                    append_log(f"[FLOOR POLL ERROR] {e}")
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        append_log(f"[FLOOR POLL] thread started name={t.name}")

    def _query_and_store_floor(self):
        # 送信のみ。応答は C++ SenderThread → Python port5000 経由で on_current_floor に来る
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                s.connect(("127.0.0.1", 5001))
                msg = json.dumps({"command": "get_current_floor"}, separators=(",", ":"), ensure_ascii=False) + "\n"
                s.sendall(msg.encode("utf-8"))
            append_log("[FLOOR POLL] sent get_current_floor")
        except Exception as e:
            append_log(f"[FLOOR POLL FAIL] {e}")

    def on_current_floor(self, data):
        floor = data.get("floor", -1)
        valid = data.get("valid", None)
        # valid フラグがある場合はそれを使用（C++が「フロアプランかどうか」を明示）
        # valid フラグがない場合は非ゼロを有効とみなす（後方互換）
        if valid is True:
            # C++ が valid=True を明示 → 0(=1FL) を含む全フロアが有効
            self._last_archicad_floor = floor
            append_log(f"[FLOOR UPDATE] floor={floor}")
        elif valid is None and floor > 0:
            # 旧プロトコル（valid なし）: 非ゼロのみ信用
            self._last_archicad_floor = floor
            append_log(f"[FLOOR UPDATE] floor={floor} (legacy)")
        else:
            append_log(f"[FLOOR UPDATE] skip floor={floor} valid={valid} stored={self._last_archicad_floor}")

    # ── TCP 送信 ─────────────────────────────────────────────────────

    def send_to_ac_async(self, data):
        def _send():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(5.0)
                    s.connect(("127.0.0.1", 5001))
                    msg = json.dumps(data, separators=(",", ":"), ensure_ascii=False) + "\n"
                    append_log(f"[SEND] {msg[:200]}...")
                    s.sendall(msg.encode("utf-8"))
                    try:
                        s.settimeout(2.0)
                        ack = s.recv(1024)
                        append_log(f"[ACK] {ack}")
                    except:
                        append_log("[NO ACK]")
            except Exception as e:
                append_log(f"[SEND ERROR] {e}")
                self.signals.error_occurred.emit(str(e))
        threading.Thread(target=_send, daemon=True).start()

    # ── ウィンドウクローズ ───────────────────────────────────────────

    def closeEvent(self, event):
        try:
            self.save_window_state()   # 閉じる直前の大きさ・位置を確実に保存
        except Exception as e:
            append_log(f"[WINDOW STATE ERROR] {e}")
        # 後片付け中にイベントフィルタが呼ばれないよう監視を外す
        self._state_timer.stop()
        for widget in list(self._tracked_windows):
            self._untrack_window(widget)
        self._tracked_windows = {}
        if self.server_thread:
            self.server_thread.stop()
        # データウィンドウはすべて閉じる（終了時は確認なし）。SQLite の登録も消去
        self._closing = True
        for panel in list(self._panels.values()):
            panel.confirm_close = None
            panel.close()
        try:
            self.db.clear_windows()
        except Exception:
            pass
        try:
            self.db.purge_old_history(keep_rows=5000)
            self.db.close()
        except Exception:
            pass
        event.accept()


# ─────────────────────────────────────────────────────────────────────
# エントリポイント
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _install_exception_hooks()
    append_log("--- Starting Application ---")
    append_log(f"Python {sys.version}")
    app           = QApplication(sys.argv)
    win           = MainWindow()
    server_thread = TcpServerThread(win.signals)
    win.server_thread = server_thread
    server_thread.start()
    # 起動時は条件設定パネルのみ表示（データパネルは [データ取得] で開く）
    win.show()
    sys.exit(app.exec())
