"""db_manager.DbManager の単体テスト（一時 SQLite ファイルを使用）。"""
import json
import threading

import db_manager


def seed(db, elems=("E1", "E2", "E9"), props=("P1", "P2", "P9", "PX")):
    """アプリと同じく 要素・プロパティ → 値 の順で登録した状態を作る。"""
    cid = db.save_condition([], {})
    db.upsert_elements([{"guid": g} for g in elems], cid)
    db.upsert_properties([{"guid": g, "name": g} for g in props])
    return cid


def test_init_creates_all_tables(db):
    names = {r[0] for r in db._conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"conditions", "elements", "properties", "element_values",
            "edit_history", "conflict_flags"} <= names


def test_wal_mode_enabled(db):
    assert db._conn().execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_default_path_uses_module_db_path(tmp_path, monkeypatch):
    monkeypatch.setattr(db_manager, "DB_PATH", tmp_path / "default.db")
    mgr = db_manager.DbManager()
    try:
        assert mgr._db_path == tmp_path / "default.db"
        assert (tmp_path / "default.db").exists()
    finally:
        mgr.close()


def test_init_is_idempotent(tmp_path):
    path = tmp_path / "same.db"
    a = db_manager.DbManager(path)
    cid = a.save_condition([0], {"Wall": True})
    a.close()
    b = db_manager.DbManager(path)  # 既存 DB を再オープンしてもデータが残る
    try:
        assert b.get_conditions()[0]["id"] == cid
    finally:
        b.close()


# ── conditions ─────────────────────────────────────────────────────

def test_save_and_get_conditions_newest_first(db):
    id1 = db.save_condition([0, 1], {"Wall": True, "Column": False})
    id2 = db.save_condition([2], {"Beam": True})
    rows = db.get_conditions()
    assert [r["id"] for r in rows] == [id2, id1]
    assert json.loads(rows[1]["story_filter"]) == [0, 1]
    assert json.loads(rows[1]["type_filter"]) == {"Wall": True, "Column": False}


def test_get_conditions_limit(db):
    for i in range(5):
        db.save_condition([i], {})
    assert len(db.get_conditions(limit=3)) == 3


def test_conditions_keep_japanese(db):
    db.save_condition(["１階"], {"壁": True})
    row = db.get_conditions()[0]
    assert "１階" in row["story_filter"] and "壁" in row["type_filter"]


# ── elements ───────────────────────────────────────────────────────

def test_upsert_elements_insert_and_update(db):
    cid = db.save_condition([], {})
    db.upsert_elements([{"guid": "E1", "type": "Wall", "layer": "L1"}], cid, {"E1": 10})
    db.upsert_elements([{"guid": "E1", "type": "Column", "layer": "L2"}], cid, {"E1": 20})
    rows = db._conn().execute("SELECT * FROM elements").fetchall()
    assert len(rows) == 1
    assert (rows[0]["elem_type"], rows[0]["layer"], rows[0]["modi_stamp"]) == ("Column", "L2", 20)


def test_upsert_elements_defaults_without_stamps(db):
    cid = db.save_condition([], {})
    db.upsert_elements([{"guid": "E1"}], cid)
    row = db._conn().execute("SELECT * FROM elements").fetchone()
    assert (row["elem_type"], row["layer"], row["modi_stamp"]) == ("", "", 0)


# ── properties ─────────────────────────────────────────────────────

def test_upsert_properties_with_enums_and_editable(db):
    db.upsert_properties([
        {"guid": "P1", "name": "耐火", "group": "一般", "type": "enum",
         "enums": ["1時間", "2時間"], "editable": False},
        {"guid": "P2", "name": "名前"},
    ])
    rows = {r["guid"]: r for r in db._conn().execute("SELECT * FROM properties")}
    assert json.loads(rows["P1"]["enums"]) == ["1時間", "2時間"]
    assert rows["P1"]["editable"] == 0
    assert rows["P2"]["enums"] is None
    assert rows["P2"]["editable"] == 1


def test_upsert_properties_updates_existing(db):
    db.upsert_properties([{"guid": "P1", "name": "旧"}])
    db.upsert_properties([{"guid": "P1", "name": "新"}])
    assert db._conn().execute("SELECT name FROM properties").fetchall()[0][0] == "新"


# ── values ─────────────────────────────────────────────────────────

def test_upsert_and_load_values(db):
    seed(db)
    db.upsert_values({("E1", "P1"): "A", ("E1", "P2"): 3, ("E2", "P1"): "B"})
    assert db.load_values() == {("E1", "P1"): "A", ("E1", "P2"): "3", ("E2", "P1"): "B"}
    assert db.load_values(["E2"]) == {("E2", "P1"): "B"}


def test_upsert_values_overwrites_and_updates_stamps(db):
    cid = seed(db)
    db.upsert_elements([{"guid": "E1"}], cid, {"E1": 1})
    db.upsert_values({("E1", "P1"): "old"})
    db.upsert_values({("E1", "P1"): "new"}, stamps={"E1": 99})
    assert db.load_values() == {("E1", "P1"): "new"}
    assert db._conn().execute("SELECT modi_stamp FROM elements").fetchone()[0] == 99


def test_update_value_insert_then_update(db):
    seed(db)
    db.update_value("E1", "P1", "x")
    db.update_value("E1", "P1", "y")
    assert db.load_values() == {("E1", "P1"): "y"}


def test_load_values_empty_list_returns_all(db):
    seed(db)
    db.update_value("E1", "P1", "x")
    assert db.load_values([]) == {("E1", "P1"): "x"}


# ── history ────────────────────────────────────────────────────────

def test_push_and_get_history_order_and_join(db):
    db.upsert_properties([{"guid": "P1", "name": "幅"}])
    db.push_history("S", "E1", "P1", "1", "2")
    db.push_history("S", "E1", "P1", "2", "3", source="undo", condition_id=None)
    db.push_history("S", "E2", "PX", None, None)

    desc = db.get_history()
    assert [h["new_value"] for h in desc] == ["", "3", "2"]
    asc = db.get_history(ascending=True)
    assert [h["new_value"] for h in asc] == ["2", "3", ""]

    e1 = db.get_history(element_guid="E1")
    assert len(e1) == 2 and all(h["prop_name"] == "幅" for h in e1)
    assert e1[0]["source"] == "undo"
    # 定義のないプロパティは prop_name が None
    assert db.get_history(element_guid="E2")[0]["prop_name"] is None


def test_get_history_limit(db):
    for i in range(10):
        db.push_history("S", "E1", "P1", i, i + 1)
    assert len(db.get_history(limit=4)) == 4
    assert len(db.get_history(element_guid="E1", limit=4)) == 4


def test_get_history_count(db):
    db.push_history("S", "E1", "P1", "a", "b")
    db.push_history("S", "E2", "P1", "a", "b")
    assert db.get_history_count() == 2
    assert db.get_history_count("E1") == 1
    assert db.get_history_count("NONE") == 0


def test_purge_old_history_keeps_newest(db):
    for i in range(10):
        db.push_history("S", "E1", "P1", str(i), str(i + 1))
    db.purge_old_history(keep_rows=3)
    assert db.get_history_count() == 3
    assert [h["new_value"] for h in db.get_history(ascending=True)] == ["8", "9", "10"]


def test_purge_old_history_noop_when_under_limit(db):
    db.push_history("S", "E1", "P1", "a", "b")
    db.purge_old_history(keep_rows=5)
    assert db.get_history_count() == 1


# ── conflict flags ─────────────────────────────────────────────────

def test_conflict_flags_save_load_clear(db):
    db.save_conflict_flags([
        {"element_guid": "E1", "property_guid": "P1", "orig_val": "o", "ac_val": "a", "user_val": "u"},
        {"element_guid": "E2", "property_guid": "P1"},
    ])
    flags = {f["element_guid"]: f for f in db.load_uncleared_flags()}
    assert set(flags) == {"E1", "E2"}
    assert (flags["E1"]["orig_val"], flags["E1"]["ac_val"], flags["E1"]["user_val"]) == ("o", "a", "u")
    assert flags["E2"]["orig_val"] == ""

    db.clear_conflict_flags(["E1"])
    assert [f["element_guid"] for f in db.load_uncleared_flags()] == ["E2"]


def test_conflict_flag_resave_reactivates(db):
    db.save_conflict_flags([{"element_guid": "E1", "property_guid": "P1", "ac_val": "1"}])
    db.clear_conflict_flags(["E1"])
    db.save_conflict_flags([{"element_guid": "E1", "property_guid": "P1", "ac_val": "2"}])
    flags = db.load_uncleared_flags()
    assert len(flags) == 1 and flags[0]["ac_val"] == "2"


# ── lifecycle / threading ──────────────────────────────────────────

def test_close_and_reopen_connection(db):
    seed(db)
    db.update_value("E1", "P1", "v")
    db.close()
    assert db._local.conn is None
    db.close()  # 二重 close でも例外にならない
    assert db.load_values() == {("E1", "P1"): "v"}  # 自動で再接続


def test_each_thread_gets_own_connection(db):
    seed(db)
    main_conn = db._conn()
    result = {}

    def worker():
        result["conn"] = db._conn()
        db.update_value("E9", "P9", "thread")
        db.close()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert result["conn"] is not main_conn
    assert db.load_values(["E9"]) == {("E9", "P9"): "thread"}


def test_foreign_keys_disabled(db):
    assert db._conn().execute("PRAGMA foreign_keys").fetchone()[0] == 0


def test_values_saved_even_without_registered_parents(db):
    # 外部キー制約は無効: 要素/プロパティ未登録でも例外にならず保存できる
    # （以前は IntegrityError → Undo 中だと PyQt6 がアプリを強制終了していた）
    db.update_value("UNKNOWN", "P?", "x")
    db.upsert_values({("UNKNOWN2", "P?"): "y"}, stamps={"UNKNOWN2": 1})
    db.push_history("S", "UNKNOWN", "P?", "a", "b", condition_id=999)
    assert db.load_values() == {("UNKNOWN", "P?"): "x", ("UNKNOWN2", "P?"): "y"}
    assert db.get_history_count("UNKNOWN") == 1