"""pytest 共通設定。

- 画面を出さない Qt (offscreen) で実行
- server.py のログは一時フォルダへ（本番 server_log.txt を汚さない）
- DB は各テストごとの一時ファイル（本番 bim_manager.db を汚さない）
- ArchiCAD への TCP 送信は記録用スタブに差し替え（実機 ArchiCAD に命令を送らない）
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# server.py の import 前に設定する必要がある
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["BIM_LOG_DIR"] = tempfile.mkdtemp(prefix="bim_test_log_")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import db_manager  # noqa: E402
import server  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def db(tmp_path):
    mgr = db_manager.DbManager(tmp_path / "test.db")
    yield mgr
    mgr.close()


class SentCommands(list):
    """send_to_ac_async に渡されたコマンドを記録する。"""

    def commands(self):
        return [d.get("command") for d in self]

    def last(self, command):
        return next(d for d in reversed(self) if d.get("command") == command)


class MessageBoxRecorder:
    """QMessageBox の静的メソッドを置き換え、表示内容を記録する。"""

    def __init__(self):
        self.calls = []
        self.question_answer = QMessageBox.StandardButton.Yes

    def _record(self, kind):
        def _f(parent, title, text, *args, **kwargs):
            self.calls.append((kind, title, text))
            if kind == "question":
                return self.question_answer
            return QMessageBox.StandardButton.Ok
        return _f

    def kinds(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def msgbox(monkeypatch):
    rec = MessageBoxRecorder()
    for kind in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(server.QMessageBox, kind, staticmethod(rec._record(kind)))
    return rec


@pytest.fixture
def main_window(qapp, tmp_path, monkeypatch, msgbox):
    monkeypatch.setattr(db_manager, "DB_PATH", tmp_path / "main.db")
    # ウィンドウ位置・大きさの保存先もテストごとに分ける
    monkeypatch.setattr(server, "SETTINGS_PATH", tmp_path / "window.ini")
    # 3 秒ごとの実ポート 5001 へのフロア問い合わせスレッドを止める
    monkeypatch.setattr(server.MainWindow, "_start_floor_polling", lambda self: None)
    sent = SentCommands()
    monkeypatch.setattr(server.MainWindow, "send_to_ac_async", lambda self, data: sent.append(data))

    win = server.MainWindow()
    win.sent = sent
    win.msgbox = msgbox
    yield win
    win.close()          # データウィンドウもすべて閉じ、DB も閉じる
    win.db.close()
    win.deleteLater()
