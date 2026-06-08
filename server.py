import sys
import socket
import json
import threading
import traceback
import faulthandler
import uuid

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTableView, QVBoxLayout,
    QWidget, QHBoxLayout, QPushButton, QGroupBox, QLabel,
    QTreeWidget, QTreeWidgetItem, QSplitter, QMessageBox, QHeaderView,
    QTreeWidgetItemIterator, QMenu, QInputDialog, QAbstractItemView,
    QStyledItemDelegate, QComboBox, QDialog, QTableWidget, QTableWidgetItem,
    QDialogButtonBox, QFrame,
)
from PyQt6.QtCore import Qt, QAbstractTableModel, pyqtSignal, QObject, QSortFilterProxyModel
from PyQt6.QtGui import QColor, QAction, QFont
from pathlib import Path

try:
    from PyQt6.QtGui import QUndoStack
except ImportError:
    from PyQt6.QtWidgets import QUndoStack  # フォールバック

from db_manager import DbManager
from undo_commands import EditValueCommand

LOG_PATH   = Path(__file__).resolve().parent / "server_log.txt"
FAULT_LOG_PATH = Path(__file__).resolve().parent / "fault_log.txt"


def append_log(message: str):
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fp:
            fp.write(f"[PY] {message}\n")
    except Exception:
        pass


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
            return QColor("darkRed") if guid in self._conflicts else QColor("black")

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
                        self.dataChanged.emit(idx, idx)
                        return

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not (index.isValid() and role == Qt.ItemDataRole.EditRole):
            return False
        row, col = index.row(), index.column()
        if col == 0:
            return False
        g       = self._elements[row]["guid"]
        pg      = self._properties[col - 1]["guid"]
        old_val = str(self._values.get((g, pg), ""))
        new_val = str(value)
        if new_val == old_val:
            return False
        if self._undo_stack and self._db and self._session_id:
            cmd = EditValueCommand(
                self, self._db, self._session_id,
                g, pg, old_val, new_val, self._current_condition_id,
            )
            self._undo_stack.push(cmd)   # redo() が自動呼ばれ set_value_direct + DB 保存
        else:
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
        if index.column() == 0:
            return Qt.ItemFlag.ItemIsEnabled
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable

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
    selection_changed            = pyqtSignal(dict)
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
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)


class PlaceholderClearDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        return super().createEditor(parent, option, index)

    def setEditorData(self, editor, index):
        val = index.data(Qt.ItemDataRole.EditRole) or ""
        if hasattr(editor, "setText"):
            editor.setText(val)

    def setModelData(self, editor, model, index):
        if hasattr(editor, "text"):
            model.setData(index, editor.text(), Qt.ItemDataRole.EditRole)
        else:
            super().setModelData(editor, model, index)


# ─────────────────────────────────────────────────────────────────────
# DiffDialog  ⑥ 差分表示
# ─────────────────────────────────────────────────────────────────────

class DiffDialog(QDialog):
    """選択要素の編集履歴（差分）を一覧表示するダイアログ"""

    _SOURCE_LABELS = {
        "user":     "ユーザー編集",
        "undo":     "取消(Undo)",
        "redo":     "やり直し(Redo)",
        "archicad": "ArchiCAD",
    }

    def __init__(self, history: list, element_guid: str = "", parent=None):
        super().__init__(parent)
        short = f"[{element_guid[:8]}]" if element_guid else "[全件]"
        self.setWindowTitle(f"編集履歴・差分  {short}")
        self.resize(950, 520)
        layout = QVBoxLayout(self)

        if not history:
            layout.addWidget(QLabel("編集履歴はありません。"))
        else:
            tbl = QTableWidget(len(history), 5, self)
            tbl.setHorizontalHeaderLabels(["日時", "プロパティ名", "変更前", "変更後", "操作"])
            tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            tbl.setAlternatingRowColors(True)
            tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            hdr = tbl.horizontalHeader()
            hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

            for row, h in enumerate(history):
                ts        = h.get("timestamp", "")[:19].replace("T", " ")
                prop_name = h.get("prop_name") or h.get("property_guid", "")[:8]
                old_v     = h.get("old_value", "")
                new_v     = h.get("new_value", "")
                src       = self._SOURCE_LABELS.get(h.get("source", ""), h.get("source", ""))

                for col, val in enumerate([ts, prop_name, old_v, new_v, src]):
                    item = QTableWidgetItem(str(val))
                    if col == 3 and new_v != old_v:
                        item.setForeground(QColor("red"))
                    if h.get("source") == "undo":
                        item.setBackground(QColor("#F0F0F0"))
                    tbl.setItem(row, col, item)

            layout.addWidget(tbl)

        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn_box.rejected.connect(self.accept)
        layout.addWidget(btn_box)


# ─────────────────────────────────────────────────────────────────────
# MainWindow
# ─────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        # ① DB・セッション・Undo スタックを setup_ui より先に初期化
        self.db                  = DbManager()
        self.session_id          = str(uuid.uuid4())
        self.undo_stack          = QUndoStack(self)
        self.undo_stack.setUndoLimit(200)
        self.current_condition_id = None

        self.setWindowTitle("Archicad BIM - 条件設定")
        self.resize(620, 700)

        # ② UI 構築
        self.setup_ui()

        # モデルに DB / Undo 参照を注入
        self.model._undo_stack  = self.undo_stack
        self.model._db          = self.db
        self.model._session_id  = self.session_id

        # ⑤ Undo/Redo ボタン ←→ Undo スタック 自動連動
        self.undo_stack.canUndoChanged.connect(self.btn_undo.setEnabled)
        self.undo_stack.canRedoChanged.connect(self.btn_redo.setEnabled)
        self.btn_undo.clicked.connect(self.undo_stack.undo)
        self.btn_redo.clicked.connect(self.undo_stack.redo)

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
        h_split = QSplitter(Qt.Orientation.Horizontal)

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
        self.btn_search = QPushButton("要素を抽出")
        self.btn_search.clicked.connect(self.search_elements)
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
        self.prop_tree = QTreeWidget()
        self.prop_tree.setHeaderLabel("プロパティ")
        self.prop_tree.itemChanged.connect(self.on_prop_item_changed)
        self.btn_get_defs   = QPushButton("プロパティ一覧を取得")
        self.btn_get_defs.clicked.connect(self.request_definitions)
        self.btn_get_values = QPushButton("データ取得")
        self.btn_get_values.clicked.connect(self.get_values)
        sp_lay.addWidget(lbl_prop)
        sp_lay.addWidget(self.prop_tree)
        sp_lay.addWidget(self.btn_get_defs)
        sp_lay.addWidget(self.btn_get_values)

        h_split.addWidget(sect_extract)
        h_split.addWidget(sect_prop)
        h_split.setSizes([1, 1])   # 等分で初期表示
        c_lay.addWidget(h_split)

        # ════════════════════════════════════════════════════════════
        # 右：データ表示・編集パネル（独立ウィンドウ）
        # ════════════════════════════════════════════════════════════
        self.data_panel = QWidget()
        self.data_panel.setWindowFlags(Qt.WindowType.Window)
        self.data_panel.setWindowTitle("Archicad BIM - データ表示・編集")
        self.data_panel.resize(1050, 720)

        dp_lay = QVBoxLayout(self.data_panel)
        dp_lay.setContentsMargins(4, 4, 4, 4)
        dp_lay.setSpacing(4)

        self.status_label = QLabel("待機中")
        dp_lay.addWidget(self.status_label)

        self.table = QTableView()
        self.model = PropertyTableModel()
        self.proxy = ExcelFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.table.setModel(self.proxy)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.horizontalHeader().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.horizontalHeader().customContextMenuRequested.connect(self.show_filter_menu)
        dp_lay.addWidget(self.table)

        # 行1: 反映・競合フラグ
        btn_row1 = QWidget()
        brl1 = QHBoxLayout(btn_row1); brl1.setContentsMargins(0, 0, 0, 0)
        self.btn_sync = QPushButton("Archicadへ反映")
        self.btn_sync.clicked.connect(self.sync_data)
        self.btn_clear_flags = QPushButton("競合フラグ解除")
        self.btn_clear_flags.setEnabled(False)
        self.btn_clear_flags.clicked.connect(self.clear_value_conflict_flags)
        brl1.addWidget(self.btn_sync)
        brl1.addWidget(self.btn_clear_flags)

        # 行2: 変更強調・差分比較
        btn_row2 = QWidget()
        brl2 = QHBoxLayout(btn_row2); brl2.setContentsMargins(0, 0, 0, 0)
        self.btn_bim_override = QPushButton("変更強調 OFF")
        self.btn_bim_override.setCheckable(True)
        self.btn_bim_override.clicked.connect(self.toggle_bim_override)
        self.btn_diff = QPushButton("差分を比較")
        self.btn_diff.setEnabled(False)
        self.btn_diff.clicked.connect(self.show_diff_dialog)
        brl2.addWidget(self.btn_bim_override)
        brl2.addWidget(self.btn_diff)

        # 行3: 確認状態管理
        btn_row3 = QWidget()
        brl3 = QHBoxLayout(btn_row3); brl3.setContentsMargins(0, 0, 0, 0)
        self.btn_confirm = QPushButton("確認済 (2)")
        self.btn_confirm.setToolTip("選択行を ChangeStatus=2（緑）に設定")
        self.btn_confirm.setEnabled(False)
        self.btn_confirm.clicked.connect(self.mark_confirmed)
        self.btn_reset_status = QPushButton("リセット (0)")
        self.btn_reset_status.setToolTip("選択行を ChangeStatus=0（未変更）に戻す")
        self.btn_reset_status.setEnabled(False)
        self.btn_reset_status.clicked.connect(self.reset_change_status)
        self.btn_confirm_all = QPushButton("全件リセット")
        self.btn_confirm_all.setToolTip("テーブル全要素を ChangeStatus=0（未変更）にリセット")
        self.btn_confirm_all.setEnabled(False)
        self.btn_confirm_all.clicked.connect(self.reset_all_table)
        brl3.addWidget(self.btn_confirm)
        brl3.addWidget(self.btn_reset_status)
        brl3.addWidget(self.btn_confirm_all)

        # 行4: Undo / Redo
        btn_row4 = QWidget()
        brl4 = QHBoxLayout(btn_row4); brl4.setContentsMargins(0, 0, 0, 0)
        self.btn_undo = QPushButton("戻る (Undo)")
        self.btn_undo.setEnabled(False)
        self.btn_redo = QPushButton("進む (Redo)")
        self.btn_redo.setEnabled(False)
        brl4.addWidget(self.btn_undo)
        brl4.addWidget(self.btn_redo)

        dp_lay.addWidget(btn_row1)
        dp_lay.addWidget(btn_row2)
        dp_lay.addWidget(btn_row3)
        dp_lay.addWidget(btn_row4)

        self.table.clicked.connect(self.on_table_clicked)

    # ── プロパティ順序ソート ─────────────────────────────────────────

    def _sort_props_by_dialog_order(self, props: list, element_type: str = "") -> list:
        order    = self._type_overrides.get(element_type, self._group_order)
        rank     = {g: i for i, g in enumerate(order)}
        fallback = len(order)

        def sort_key(p):
            return (rank.get(p.get("group", ""), fallback), p.get("group", ""), p.get("name", ""))

        return sorted(props, key=sort_key)

    # ── エラー処理 ───────────────────────────────────────────────────

    def on_error(self, m):
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
        self.status_label.setText("要素抽出中...")
        self.send_to_ac_async({"command": "get_elements", "stories": stories, **types})

    def on_config(self, data):
        self.filter_tree.clear()
        sr = QTreeWidgetItem(self.filter_tree, ["階層"])
        for s in data.get("stories", []):
            c = QTreeWidgetItem(sr, [s["name"]])
            c.setData(0, Qt.ItemDataRole.UserRole, s["index"])
            c.setCheckState(0, Qt.CheckState.Unchecked)
        tr = QTreeWidgetItem(self.filter_tree, ["要素タイプ"])
        for n, cnt in data.get("elementTypes", {}).items():
            c = QTreeWidgetItem(tr, [f"{n} ({cnt})"])
            c.setData(0, Qt.ItemDataRole.UserRole, n)
            c.setCheckState(0, Qt.CheckState.Unchecked)
        self.filter_tree.expandAll()

    def on_elements(self, data):
        self.current_elements = data.get("elements", [])
        types = {e.get("type", "") for e in self.current_elements if e.get("type")}
        self._current_element_type = next(iter(types), "") if len(types) == 1 else ""
        cnt = len(self.current_elements)
        self.status_label.setText(
            f"要素: {cnt}件 抽出完了 → 次: プロパティ一覧を取得 してください" if cnt else "該当なし。条件を変えて再試行してください"
        )
        # ③ DB に要素を保存
        try:
            if self.current_elements and self.current_condition_id:
                self.db.upsert_elements(self.current_elements, self.current_condition_id)
                append_log(f"[DB] elements saved: {len(self.current_elements)}")
        except Exception as e:
            append_log(f"[DB ERROR] on_elements: {e}")

    # ── プロパティ定義 ───────────────────────────────────────────────

    def request_definitions(self):
        if hasattr(self, "current_elements") and self.current_elements:
            self.send_to_ac_async({
                "command": "get_definitions",
                "guids":   [e["guid"] for e in self.current_elements[:1]],
            })

    def on_definitions(self, data):
        self.prop_tree.clear()
        groups      = {}
        sorted_defs = self._sort_props_by_dialog_order(
            data.get("definitions", []),
            element_type=self._current_element_type,
        )
        for d in sorted_defs:
            gn = d.get("group", "Other")
            if gn not in groups:
                p = QTreeWidgetItem(self.prop_tree, [gn])
                p.setCheckState(0, Qt.CheckState.Unchecked)
                groups[gn] = p
            c = QTreeWidgetItem(groups[gn], [d["name"]])
            c.setData(0, Qt.ItemDataRole.UserRole, d)
            c.setCheckState(0, Qt.CheckState.Unchecked)
        self.prop_tree.expandAll()
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
        if sel and hasattr(self, "current_elements"):
            self.current_props = self._sort_props_by_dialog_order(
                sel, element_type=self._current_element_type
            )
            self.send_to_ac_async({
                "command":   "get_values",
                "guids":     [e["guid"] for e in self.current_elements],
                "propGuids": [p["guid"] for p in self.current_props],
            })

    def on_values(self, data):
        if self._verifying_stamps:
            self._verifying_stamps = False
            self._on_pre_apply_verify(data)
            return
        v, s = {}, {}
        for item in data.get("values", []):
            g = item["guid"]
            s[g] = item.get("modiStamp", 0)
            for pg, val in item["props"].items():
                v[(g, pg)] = val
        self.model.set_data(self.current_elements, self.current_props, v, s)
        # モデルに現在の condition_id を反映
        self.model._current_condition_id = self.current_condition_id
        self.btn_confirm_all.setEnabled(bool(self.model._elements))
        self.btn_diff.setEnabled(bool(self.model._elements))
        self._apply_enum_delegates()
        # 右ペインのステータスに件数を表示（左右のリンクを明示）
        elem_cnt = len(self.model._elements)
        prop_cnt = len(self.model._properties)
        self.status_label.setText(
            f"データ取得完了:  要素 {elem_cnt}件  ×  プロパティ {prop_cnt}列  （セルをダブルクリックで編集）"
        )
        # [データ取得] で初めてデータパネルを開く（以後は既に開いているので raise のみ）
        self.data_panel.show()
        self.data_panel.raise_()
        # ③ SQLite に保存
        try:
            self.db.upsert_elements(self.current_elements, self.current_condition_id, s)
            self.db.upsert_properties(getattr(self, "current_props", []))
            self.db.upsert_values(v, s)
            append_log(f"[DB] values saved: {len(v)} cells")
        except Exception as e:
            append_log(f"[DB ERROR] on_values: {e}")

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
            history = self.db.get_history(element_guid=guid)
            dlg     = DiffDialog(history, element_guid=guid or "", parent=self)
            dlg.exec()
        except Exception as e:
            QMessageBox.warning(self, "エラー", f"差分取得失敗: {e}")
            append_log(f"[DIFF ERROR] {e}")

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
        except Exception as e:
            append_log(f"on_table_clicked ERROR: {e}\n{traceback.format_exc()}")

    def on_archicad_selection_changed(self, payload):
        try:
            guid = payload.get("guid", "")
            if not guid:
                return
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
            self.send_to_ac_async({
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
        if self.btn_bim_override.isChecked():
            self.btn_bim_override.setText("変更強調 ON")
            self.status_label.setText("変更強調適用中...")
            self.send_to_ac_async({"command": "apply_bim_override"})
        else:
            self.btn_bim_override.setText("変更強調 OFF")
            self.status_label.setText("変更強調解除中...")
            self.send_to_ac_async({"command": "remove_bim_override"})

    def on_bim_override_result(self, data):
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

    # ── ChangeStatus 管理 ────────────────────────────────────────────

    def mark_confirmed(self):
        if not self._selected_guids:
            return
        self._pending_status_change = {"guids": list(self._selected_guids), "value": 2}
        self.send_to_ac_async({"command": "set_change_status", "guids": self._selected_guids, "status": 2})
        self.status_label.setText(f"確認済設定中: {len(self._selected_guids)}件...")

    def reset_change_status(self):
        if not self._selected_guids:
            return
        self._pending_status_change = {"guids": list(self._selected_guids), "value": 0}
        self.send_to_ac_async({"command": "set_change_status", "guids": self._selected_guids, "status": 0})
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
        self.send_to_ac_async({"command": "set_change_status", "guids": guids, "status": 0})
        self.status_label.setText(f"全件リセット中: {len(guids)}件...")

    def on_change_status_result(self, data):
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
            else:
                self._pending_status_change = None
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
        self.btn_sync.setEnabled(False)
        changed_guids = list({c["guid"]   for c in changes})
        changed_props = list({c["propId"] for c in changes if c.get("propId")})
        self.status_label.setText("stamp/値を確認中...")
        self.send_to_ac_async({"command": "get_values", "guids": changed_guids, "propGuids": changed_props})

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
            self.send_to_ac_async({"command": "apply_changes", "changes": pending})
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
            self.send_to_ac_async({"command": "apply_changes", "changes": pending})

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
                self.send_to_ac_async({
                    "command":  "mark_change_flags",
                    "guids":    flag_guids,
                    "flag":     "⚠変更あり",
                    "propName": "変更フラグ",
                })
            if safe_changes:
                append_log(f"[PRE-VERIFY] 競合スキップ → safe {len(safe_changes)} 件 / skip {len(conflict_changes)} 件")
                self.status_label.setText(f"競合スキップして反映中... ({len(safe_changes)}件)")
                self.send_to_ac_async({"command": "apply_changes", "changes": safe_changes})
            else:
                self.status_label.setText("反映キャンセル（全て競合）")
        else:
            self.status_label.setText("反映を中止しました")

    def on_sync_complete(self, data):
        append_log("=== SYNC COMPLETE RECEIVED ===")
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

            py_skip     = self._pending_skip_count
            self._pending_skip_count = 0
            stamp_txt   = f" / stamp変化:{stamp_stale_cnt}" if stamp_stale_cnt > 0 else ""
            py_skip_txt = f" / 競合スキップ:{py_skip}"      if py_skip > 0         else ""
            msg         = f"成功:{sc} / 競合:{cc} / エラー:{ec}{stamp_txt}{py_skip_txt}"
            self.status_label.setText(msg)
            d_txt     = "\n".join(details[:10])
            has_issue = ec > 0 or cc > 0 or py_skip > 0

            if not has_issue:
                QMessageBox.information(self, "同期完了", msg)
            elif ec == 0:
                QMessageBox.warning(self, "一部競合/スキップ", f"{msg}\n\n{d_txt}" if d_txt else msg)
            else:
                QMessageBox.critical(self, "エラー", f"{msg}\n\n{d_txt}")
        except Exception as e:
            append_log(f"SYNC ERROR: {e}\n{traceback.format_exc()}")
            QMessageBox.critical(self, "内部エラー", str(e))

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
        if self.server_thread:
            self.server_thread.stop()
        try:
            self.db.close()
        except Exception:
            pass
        self.data_panel.close()
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
