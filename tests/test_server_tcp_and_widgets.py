"""TcpServerThread（受信と振り分け）/ デリゲート / DiffDialog のテスト。"""
import json
import socket
import threading
import time

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox, QLineEdit, QStyleOptionViewItem, QWidget

import server


# ── TcpServerThread.handle_client ──────────────────────────────────

class SignalSpy:
    """CommunicationSignals の全シグナルの発火を記録する。"""

    NAMES = ["config_received", "elements_received", "definitions_received", "values_received",
             "sync_complete", "flag_result_received", "bim_override_result_received",
             "change_status_result_received", "current_floor_received", "selection_changed",
             "show_window", "error_occurred"]

    def __init__(self):
        self.signals = server.CommunicationSignals()
        self.events = []
        for n in self.NAMES:
            getattr(self.signals, n).connect(lambda *a, _n=n: self.events.append((_n, a[0] if a else None)))

    def names(self):
        return [e[0] for e in self.events]


def _deliver(spy, payload_bytes):
    """socketpair 経由で handle_client に生バイトを流し、返信を返す。"""
    a, b = socket.socketpair()
    thread = server.TcpServerThread(spy.signals)
    a.sendall(payload_bytes)
    a.shutdown(socket.SHUT_WR)
    thread.handle_client(b)
    reply = a.recv(100)
    a.close()
    return reply


def _lines(*objs):
    return ("\n".join(json.dumps(o, ensure_ascii=False) for o in objs) + "\n").encode("utf-8")


@pytest.mark.parametrize("msg_type, signal", [
    ("elements", "elements_received"),
    ("property_definitions", "definitions_received"),
    ("property_values", "values_received"),
    ("sync_complete", "sync_complete"),
    ("sync_result", "sync_complete"),
    ("apply_result", "sync_complete"),
    ("result", "sync_complete"),
    ("flag_result", "flag_result_received"),
    ("bim_override_result", "bim_override_result_received"),
    ("change_status_result", "change_status_result_received"),
    ("selection_changed", "selection_changed"),
    ("current_floor", "current_floor_received"),
])
def test_handle_client_routes_by_type(qapp, msg_type, signal):
    spy = SignalSpy()
    assert _deliver(spy, _lines({"type": msg_type, "x": 1})) == b"OK\n"
    assert spy.names() == [signal]
    assert spy.events[0][1]["x"] == 1


def test_handle_client_project_config_shows_window(qapp):
    spy = SignalSpy()
    _deliver(spy, _lines({"type": "project_config", "stories": []}))
    assert spy.names() == ["config_received", "show_window"]


def test_handle_client_infers_type_and_unwraps_data(qapp):
    spy = SignalSpy()
    _deliver(spy, _lines(
        {"results": []},                                 # → sync_complete
        {"elements": [{"guid": "E"}]},                   # → elements
        {"data": {"type": "current_floor", "floor": 2}},  # data ラッパーを外す
    ))
    assert spy.names() == ["sync_complete", "elements_received", "current_floor_received"]
    assert spy.events[2][1]["floor"] == 2


def test_handle_client_ignores_bad_json_unknown_and_blank_lines(qapp):
    spy = SignalSpy()
    body = b"\n  \n{not json}\n" + _lines({"type": "nope"}, {"type": "flag_result"})
    assert _deliver(spy, body) == b"OK\n"
    assert spy.names() == ["flag_result_received"]


def test_handle_client_multibyte_payload(qapp):
    spy = SignalSpy()
    _deliver(spy, _lines({"type": "elements", "name": "７ Floor (パラペット天端)" * 5000}))
    assert spy.events[0][1]["name"].startswith("７ Floor")


def test_handle_client_empty_connection_sends_nothing(qapp):
    spy = SignalSpy()
    assert _deliver(spy, b"") == b""
    assert spy.events == []


class _BoomConn:
    def __init__(self, first=b""):
        self._first = first
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def settimeout(self, t): pass
    def recv(self, n):
        if self._first:
            data, self._first = self._first, b""
            return data
        raise OSError("boom")
    def sendall(self, b): raise OSError("closed")


def test_handle_client_recv_error_without_data(qapp):
    spy = SignalSpy()
    server.TcpServerThread(spy.signals).handle_client(_BoomConn())
    assert spy.events == []


def test_handle_client_recv_error_after_data_still_processes(qapp):
    spy = SignalSpy()
    server.TcpServerThread(spy.signals).handle_client(_BoomConn(_lines({"type": "flag_result"})))
    assert spy.names() == ["flag_result_received"]


def test_handle_client_recv_timeout_processes_partial(qapp):
    spy = SignalSpy()

    class _TimeoutConn(_BoomConn):
        def recv(self, n):
            if self._first:
                data, self._first = self._first, b""
                return data
            raise socket.timeout()

    server.TcpServerThread(spy.signals).handle_client(_TimeoutConn(_lines({"type": "current_floor"})))
    assert spy.names() == ["current_floor_received"]


# ── TcpServerThread.run（実ソケット: 5000 番の代わりに空きポートへ bind）──

class _PortRedirectSocket(socket.socket):
    bound = []

    def bind(self, addr):
        super().bind((addr[0], 0))
        _PortRedirectSocket.bound.append(self.getsockname()[1])


class _FakeSocketModule:
    AF_INET, SOCK_STREAM = socket.AF_INET, socket.SOCK_STREAM
    SOL_SOCKET, SO_REUSEADDR = socket.SOL_SOCKET, socket.SO_REUSEADDR
    timeout = socket.timeout
    socket = _PortRedirectSocket


def test_run_accepts_and_dispatches(qapp, monkeypatch):
    monkeypatch.setattr(server, "socket", _FakeSocketModule)
    _PortRedirectSocket.bound.clear()
    spy = SignalSpy()
    th = server.TcpServerThread(spy.signals)
    th.start()
    for _ in range(100):
        if _PortRedirectSocket.bound:
            break
        time.sleep(0.02)
    port = _PortRedirectSocket.bound[0]

    with socket.create_connection(("127.0.0.1", port)) as c:
        c.sendall(_lines({"type": "flag_result"}))
        c.shutdown(socket.SHUT_WR)
        assert c.recv(10) == b"OK\n"

    th.stop()
    th.join(timeout=5)
    assert not th.is_alive()
    for _ in range(50):
        qapp.processEvents()
        if spy.events:
            break
        time.sleep(0.02)
    assert spy.names() == ["flag_result_received"]


def test_run_bind_error_emits_error(qapp, monkeypatch):
    class _FailSocket(socket.socket):
        def bind(self, addr):
            raise OSError("address in use")

    class _Mod(_FakeSocketModule):
        socket = _FailSocket

    monkeypatch.setattr(server, "socket", _Mod)
    spy = SignalSpy()
    th = server.TcpServerThread(spy.signals)
    th.run()  # 同期実行: bind 失敗で即 return
    assert spy.names() == ["error_occurred"]
    assert "Server Bind Error" in spy.events[0][1]


# ── デリゲート ─────────────────────────────────────────────────────

@pytest.fixture
def table_model(qapp):
    m = server.PropertyTableModel()
    m.set_data([{"guid": "E1"}], [{"guid": "P1", "name": "耐火"}], {("E1", "P1"): "2時間"})
    return m


def test_enum_delegate_roundtrip(table_model):
    parent = QWidget()
    d = server.EnumComboBoxDelegate(["1時間", "2時間", "なし"])
    idx = table_model.index(0, 1)
    editor = d.createEditor(parent, QStyleOptionViewItem(), idx)
    assert isinstance(editor, QComboBox) and editor.count() == 3
    d.setEditorData(editor, idx)
    assert editor.currentText() == "2時間"
    editor.setCurrentIndex(2)
    d.setModelData(editor, table_model, idx)
    assert table_model._values[("E1", "P1")] == "なし"
    opt = QStyleOptionViewItem()
    d.updateEditorGeometry(editor, opt, idx)
    assert editor.geometry() == opt.rect


def test_enum_delegate_unknown_value_keeps_index(table_model):
    d = server.EnumComboBoxDelegate(["A", "B"])
    parent = QWidget()  # 親を保持しないと GC で editor ごと破棄される
    editor = d.createEditor(parent, QStyleOptionViewItem(), table_model.index(0, 1))
    d.setEditorData(editor, table_model.index(0, 1))  # "2時間" は候補に無い
    assert editor.currentIndex() == 0


def test_placeholder_delegate_roundtrip(table_model):
    d = server.PlaceholderClearDelegate()
    idx = table_model.index(0, 1)
    parent = QWidget()
    editor = d.createEditor(parent, QStyleOptionViewItem(), idx)
    assert isinstance(editor, QLineEdit)
    d.setEditorData(editor, idx)
    assert editor.text() == "2時間"
    editor.setText("3時間")
    d.setModelData(editor, table_model, idx)
    assert table_model._values[("E1", "P1")] == "3時間"


def test_placeholder_delegate_non_text_editor_falls_back(table_model, monkeypatch):
    d = server.PlaceholderClearDelegate()
    called = []
    monkeypatch.setattr(server.QStyledItemDelegate, "setModelData",
                        lambda self, e, m, i: called.append(True))
    d.setEditorData(QWidget(), table_model.index(0, 1))  # setText 無し → 何もしない
    d.setModelData(QWidget(), table_model, table_model.index(0, 1))
    assert called == [True]


# ── DiffDialog ─────────────────────────────────────────────────────

HISTORY = [  # get_history の既定と同じく新→古
    {"timestamp": "2026-10-18T14:03:00", "prop_name": "幅", "property_guid": "P1",
     "old_value": "200", "new_value": "100", "source": "undo"},
    {"timestamp": "2026-10-18T14:02:00", "prop_name": None, "property_guid": "PROPGUID-XYZ",
     "old_value": "a", "new_value": "a", "source": "archicad"},
    {"timestamp": "2026-10-18T14:01:00", "prop_name": "幅", "property_guid": "P1",
     "old_value": "100", "new_value": "200", "source": "custom"},
]


def _row(dlg, r):
    return [dlg._tbl.item(r, c).text() for c in range(5)]


def test_diff_dialog_rows_and_styles(qapp):
    dlg = server.DiffDialog(HISTORY, element_guid="ABCDEFGH-1234")
    assert dlg.windowTitle() == "編集履歴・差分  [ABCDEFGH]"
    assert dlg._count_label.text() == "履歴: 3 件"
    assert dlg._tbl.rowCount() == 3
    assert _row(dlg, 0) == ["2026-10-18 14:03:00", "幅", "200", "100", "取消(Undo)"]
    assert _row(dlg, 1)[1] == "PROPGUID"       # prop_name 無し → GUID 先頭 8 文字
    assert _row(dlg, 2)[4] == "custom"          # 未知の source はそのまま
    assert dlg._tbl.item(0, 3).font().bold()    # 値が変わった「変更後」は太字
    assert not dlg._tbl.item(1, 3).font().bold()
    assert dlg._tbl.item(0, 2).foreground().color().name() == "#888888"  # undo の変更前はグレー


def test_diff_dialog_sort_toggle(qapp):
    dlg = server.DiffDialog(HISTORY)
    assert dlg.windowTitle().endswith("[全件]")
    dlg._chk_asc.setChecked(True)
    assert _row(dlg, 0)[0] == "2026-10-18 14:01:00"
    dlg._chk_asc.setChecked(False)
    assert _row(dlg, 0)[0] == "2026-10-18 14:03:00"


def test_diff_dialog_empty(qapp):
    dlg = server.DiffDialog([])
    assert dlg._tbl.rowCount() == 1
    assert dlg._tbl.item(0, 0).text() == "編集履歴はありません。"


def test_handle_client_routes_site_summary(qapp):
    spy = SignalSpy()
    sig = []
    spy.signals.site_summary_received.connect(sig.append)
    _deliver(spy, _lines({"type": "site_summary", "elements": [], "projectInfo": []}))
    assert sig and sig[0]["type"] == "site_summary"
