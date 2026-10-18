"""server.py のログ・例外フックのテスト。"""
import faulthandler
import sys
import threading

import server


def test_log_dir_is_redirected_for_tests():
    # conftest で BIM_LOG_DIR を一時フォルダに向けている（本番 server_log.txt を汚さない）
    assert server.LOG_PATH.parent != server.Path(server.__file__).resolve().parent


def test_append_log_writes_with_prefix():
    server.append_log("テスト行")
    assert server.LOG_PATH.read_text(encoding="utf-8").splitlines()[-1] == "[PY] テスト行"


def test_append_log_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "LOG_PATH", tmp_path / "no_such_dir" / "log.txt")
    server.append_log("書けなくても例外にしない")


def test_install_exception_hooks(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "FAULT_LOG_PATH", tmp_path / "fault.txt")
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr(sys, "__excepthook__", lambda *a: None)
    logged = []
    monkeypatch.setattr(server, "append_log", logged.append)
    try:
        server._install_exception_hooks()
        assert faulthandler.is_enabled()
        try:
            raise ValueError("main boom")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        t = threading.Thread(target=lambda: (_ for _ in ()).throw(KeyError("thread boom")))
        t.start()
        t.join()
    finally:
        faulthandler.disable()
    assert logged[0].startswith("[UNCAUGHT EXCEPTION]") and "main boom" in logged[0]
    assert logged[1].startswith("[THREAD EXCEPTION]") and "thread boom" in logged[1]
