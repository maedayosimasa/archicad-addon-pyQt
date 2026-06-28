import sqlite3
import json
import threading
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).resolve().parent / "bim_manager.db"


class DbManager:
    """SQLite 永続化層。WAL モード + スレッドローカル接続でスレッドセーフに動作する。"""

    def __init__(self, db_path: Path = None):
        self._db_path = db_path or DB_PATH
        self._local = threading.local()
        self._init_db()

    # ── 接続管理 ─────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conditions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT NOT NULL,
                story_filter  TEXT,
                type_filter   TEXT
            );

            CREATE TABLE IF NOT EXISTS elements (
                guid          TEXT PRIMARY KEY,
                condition_id  INTEGER REFERENCES conditions(id),
                elem_type     TEXT,
                layer         TEXT,
                modi_stamp    INTEGER DEFAULT 0,
                fetched_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS properties (
                guid          TEXT PRIMARY KEY,
                name          TEXT,
                group_name    TEXT,
                prop_type     TEXT,
                enums         TEXT,
                editable      INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS element_values (
                element_guid  TEXT REFERENCES elements(guid),
                property_guid TEXT REFERENCES properties(guid),
                value         TEXT,
                status        INTEGER DEFAULT 0,
                last_updated  TEXT,
                PRIMARY KEY (element_guid, property_guid)
            );

            CREATE TABLE IF NOT EXISTS edit_history (
                history_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id    TEXT NOT NULL,
                element_guid  TEXT,
                property_guid TEXT,
                old_value     TEXT,
                new_value     TEXT,
                source        TEXT DEFAULT 'user',
                timestamp     TEXT NOT NULL,
                condition_id  INTEGER REFERENCES conditions(id)
            );

            CREATE TABLE IF NOT EXISTS conflict_flags (
                element_guid  TEXT,
                property_guid TEXT,
                orig_val      TEXT,
                ac_val        TEXT,
                user_val      TEXT,
                flagged_at    TEXT,
                cleared       INTEGER DEFAULT 0,
                PRIMARY KEY (element_guid, property_guid)
            );

            -- パフォーマンス用インデックス
            CREATE INDEX IF NOT EXISTS idx_edit_history_element
                ON edit_history(element_guid);
            CREATE INDEX IF NOT EXISTS idx_edit_history_session
                ON edit_history(session_id);
            CREATE INDEX IF NOT EXISTS idx_edit_history_timestamp
                ON edit_history(timestamp);
            CREATE INDEX IF NOT EXISTS idx_element_values_element
                ON element_values(element_guid);
            CREATE INDEX IF NOT EXISTS idx_elements_condition
                ON elements(condition_id);
        """)
        conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ── Conditions ───────────────────────────────────────────────────

    def save_condition(self, stories: list, types: dict) -> int:
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO conditions (timestamp, story_filter, type_filter) VALUES (?,?,?)",
            (self._now(),
             json.dumps(stories, ensure_ascii=False),
             json.dumps(types, ensure_ascii=False)),
        )
        conn.commit()
        return cur.lastrowid

    def get_conditions(self, limit: int = 50) -> list:
        rows = self._conn().execute(
            "SELECT * FROM conditions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Elements ─────────────────────────────────────────────────────

    def upsert_elements(self, elements: list, condition_id: int, stamps: dict = None):
        conn = self._conn()
        stamps = stamps or {}
        now = self._now()
        conn.executemany(
            """INSERT INTO elements (guid, condition_id, elem_type, layer, modi_stamp, fetched_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(guid) DO UPDATE SET
                   condition_id = excluded.condition_id,
                   elem_type    = excluded.elem_type,
                   layer        = excluded.layer,
                   modi_stamp   = excluded.modi_stamp,
                   fetched_at   = excluded.fetched_at""",
            [
                (e["guid"], condition_id, e.get("type", ""), e.get("layer", ""),
                 stamps.get(e["guid"], 0), now)
                for e in elements
            ],
        )
        conn.commit()

    # ── Properties ───────────────────────────────────────────────────

    def upsert_properties(self, props: list):
        conn = self._conn()
        conn.executemany(
            """INSERT INTO properties (guid, name, group_name, prop_type, enums, editable)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(guid) DO UPDATE SET
                   name       = excluded.name,
                   group_name = excluded.group_name,
                   prop_type  = excluded.prop_type,
                   enums      = excluded.enums,
                   editable   = excluded.editable""",
            [
                (p["guid"], p.get("name", ""), p.get("group", ""), p.get("type", ""),
                 json.dumps(p["enums"], ensure_ascii=False) if p.get("enums") else None,
                 1 if p.get("editable", True) else 0)
                for p in props
            ],
        )
        conn.commit()

    # ── Values ───────────────────────────────────────────────────────

    def upsert_values(self, values: dict, stamps: dict = None):
        """values: {(element_guid, property_guid): value}"""
        conn = self._conn()
        now = self._now()
        conn.executemany(
            """INSERT INTO element_values (element_guid, property_guid, value, status, last_updated)
               VALUES (?,?,?,0,?)
               ON CONFLICT(element_guid, property_guid) DO UPDATE SET
                   value        = excluded.value,
                   last_updated = excluded.last_updated""",
            [(eg, pg, str(val), now) for (eg, pg), val in values.items()],
        )
        if stamps:
            conn.executemany(
                "UPDATE elements SET modi_stamp=? WHERE guid=?",
                [(v, k) for k, v in stamps.items()],
            )
        conn.commit()

    def update_value(self, element_guid: str, property_guid: str, value: str):
        """単一セルの値を更新（Undo/Redo コマンドから呼ばれる）"""
        conn = self._conn()
        conn.execute(
            """INSERT INTO element_values (element_guid, property_guid, value, status, last_updated)
               VALUES (?,?,?,0,?)
               ON CONFLICT(element_guid, property_guid) DO UPDATE SET
                   value        = excluded.value,
                   last_updated = excluded.last_updated""",
            (element_guid, property_guid, value, self._now()),
        )
        conn.commit()

    def load_values(self, element_guids: list = None) -> dict:
        """Returns {(element_guid, property_guid): value}"""
        conn = self._conn()
        if element_guids:
            ph = ",".join("?" * len(element_guids))
            rows = conn.execute(
                f"SELECT element_guid, property_guid, value FROM element_values"
                f" WHERE element_guid IN ({ph})",
                element_guids,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT element_guid, property_guid, value FROM element_values"
            ).fetchall()
        return {(r["element_guid"], r["property_guid"]): r["value"] for r in rows}

    # ── EditHistory ──────────────────────────────────────────────────

    def push_history(self, session_id: str, element_guid: str, property_guid: str,
                     old_value: str, new_value: str, source: str = "user",
                     condition_id: int = None):
        conn = self._conn()
        conn.execute(
            """INSERT INTO edit_history
               (session_id, element_guid, property_guid, old_value, new_value, source, timestamp, condition_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (session_id, element_guid, property_guid,
             str(old_value) if old_value is not None else "",
             str(new_value) if new_value is not None else "",
             source, self._now(), condition_id),
        )
        conn.commit()

    def get_history(self, element_guid: str = None, limit: int = 300,
                    ascending: bool = False) -> list:
        """編集履歴を取得。ascending=True で古い順。"""
        order = "ASC" if ascending else "DESC"
        conn = self._conn()
        if element_guid:
            rows = conn.execute(
                f"""SELECT h.*, p.name AS prop_name
                    FROM edit_history h
                    LEFT JOIN properties p ON h.property_guid = p.guid
                    WHERE h.element_guid = ?
                    ORDER BY h.history_id {order} LIMIT ?""",
                (element_guid, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT h.*, p.name AS prop_name
                    FROM edit_history h
                    LEFT JOIN properties p ON h.property_guid = p.guid
                    ORDER BY h.history_id {order} LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_history_count(self, element_guid: str = None) -> int:
        """履歴レコード件数を返す（DiffDialog のヘッダー表示用）。"""
        conn = self._conn()
        if element_guid:
            return conn.execute(
                "SELECT COUNT(*) FROM edit_history WHERE element_guid=?",
                (element_guid,),
            ).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM edit_history").fetchone()[0]

    def purge_old_history(self, keep_rows: int = 5000):
        """edit_history が keep_rows を超えたら古い行を削除する。"""
        conn = self._conn()
        total = conn.execute("SELECT COUNT(*) FROM edit_history").fetchone()[0]
        if total > keep_rows:
            delete_count = total - keep_rows
            conn.execute(
                "DELETE FROM edit_history WHERE history_id IN "
                "(SELECT history_id FROM edit_history ORDER BY history_id ASC LIMIT ?)",
                (delete_count,),
            )
            conn.commit()

    # ── ConflictFlags ────────────────────────────────────────────────

    def save_conflict_flags(self, conflicts: list):
        """conflicts: list of {element_guid, property_guid, orig_val, ac_val, user_val}"""
        conn = self._conn()
        now = self._now()
        conn.executemany(
            """INSERT INTO conflict_flags
               (element_guid, property_guid, orig_val, ac_val, user_val, flagged_at, cleared)
               VALUES (?,?,?,?,?,?,0)
               ON CONFLICT(element_guid, property_guid) DO UPDATE SET
                   orig_val  = excluded.orig_val,
                   ac_val    = excluded.ac_val,
                   user_val  = excluded.user_val,
                   flagged_at= excluded.flagged_at,
                   cleared   = 0""",
            [(c["element_guid"], c["property_guid"],
              c.get("orig_val", ""), c.get("ac_val", ""), c.get("user_val", ""), now)
             for c in conflicts],
        )
        conn.commit()

    def clear_conflict_flags(self, guids: list):
        conn = self._conn()
        ph = ",".join("?" * len(guids))
        conn.execute(
            f"UPDATE conflict_flags SET cleared=1 WHERE element_guid IN ({ph})", guids
        )
        conn.commit()

    def load_uncleared_flags(self) -> list:
        rows = self._conn().execute(
            "SELECT * FROM conflict_flags WHERE cleared=0"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Lifecycle ────────────────────────────────────────────────────

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
