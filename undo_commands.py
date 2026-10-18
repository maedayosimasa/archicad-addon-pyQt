import sys
import traceback

try:
    from PyQt6.QtGui import QUndoCommand
except ImportError:
    from PyQt6.QtWidgets import QUndoCommand


def _log(message: str):
    # 読み込み済みの server.append_log を使う。server.py は直接起動すると "__main__" になるため、
    # import し直すと server.py が二重に実行されてしまう → sys.modules から探すだけにする
    for name in ("__main__", "server"):
        append_log = getattr(sys.modules.get(name), "append_log", None)
        if append_log:
            append_log(message)
            return


class EditValueCommand(QUndoCommand):
    """テーブルセルの値編集を Undo/Redo 可能にするコマンド。

    push() 時に Qt が自動で redo() を呼ぶため、初回呼び出しは source="user"、
    それ以降（redo 操作）は source="redo" として履歴に記録する。

    redo()/undo() は Qt(C++) から呼ばれるため、ここで例外が漏れると PyQt6 が
    アプリごと強制終了する。DB 保存は失敗してもログのみとし、画面上の編集は継続する。
    """

    def __init__(self, model, db, session_id: str,
                 element_guid: str, prop_guid: str,
                 old_value: str, new_value: str,
                 condition_id: int = None):
        prop_name = next(
            (p["name"] for p in model._properties if p["guid"] == prop_guid),
            prop_guid[:8],
        )
        super().__init__(f"編集: {prop_name} → {new_value}")
        self._model         = model
        self._db            = db
        self._session_id    = session_id
        self._element_guid  = element_guid
        self._prop_guid     = prop_guid
        self._old_value     = old_value
        self._new_value     = new_value
        self._condition_id  = condition_id
        # push() 時の自動 redo() を「ユーザー編集」と区別するフラグ
        self._is_first_redo = True

    def _save(self, old_value, new_value, source):
        try:
            self._db.push_history(
                self._session_id, self._element_guid, self._prop_guid,
                old_value, new_value, source, self._condition_id,
            )
            self._db.update_value(self._element_guid, self._prop_guid, new_value)
        except Exception as e:
            _log(f"[UNDO DB ERROR] {source} {self._element_guid[:8]}/{self._prop_guid[:8]}: {e}\n"
                 f"{traceback.format_exc()}")

    # ── redo ──────────────────────────────────────────────────────────

    def redo(self):
        source = "user" if self._is_first_redo else "redo"
        self._is_first_redo = False
        self._model.set_value_direct(self._element_guid, self._prop_guid, self._new_value)
        self._save(self._old_value, self._new_value, source)

    # ── undo ──────────────────────────────────────────────────────────

    def undo(self):
        # undo 後に再 redo された場合は "redo" ソースになるよう False のまま維持
        self._is_first_redo = False
        self._model.set_value_direct(self._element_guid, self._prop_guid, self._old_value)
        self._save(self._new_value, self._old_value, "undo")
