try:
    from PyQt6.QtGui import QUndoCommand
except ImportError:
    from PyQt6.QtWidgets import QUndoCommand  # フォールバック


class EditValueCommand(QUndoCommand):
    """テーブルセルの値編集を Undo/Redo 可能にするコマンド。

    model は PropertyTableModel のインスタンスを期待するが、循環インポートを避けるため
    型ヒントなしのダックタイピングで参照する。
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
        self._model = model
        self._db = db
        self._session_id = session_id
        self._element_guid = element_guid
        self._prop_guid = prop_guid
        self._old_value = old_value
        self._new_value = new_value
        self._condition_id = condition_id

    def redo(self):
        self._model.set_value_direct(self._element_guid, self._prop_guid, self._new_value)
        self._db.push_history(
            self._session_id, self._element_guid, self._prop_guid,
            self._old_value, self._new_value, "user", self._condition_id,
        )
        self._db.update_value(self._element_guid, self._prop_guid, self._new_value)

    def undo(self):
        self._model.set_value_direct(self._element_guid, self._prop_guid, self._old_value)
        self._db.push_history(
            self._session_id, self._element_guid, self._prop_guid,
            self._new_value, self._old_value, "undo", self._condition_id,
        )
        self._db.update_value(self._element_guid, self._prop_guid, self._old_value)
