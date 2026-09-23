"""待确认导入表的仓储层测试。"""
import json

import pytest

from app import config, db


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "data" / "staging")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "app.db")
    c = db.get_conn()
    db.init_db(c)
    yield c
    c.close()


def test_统一前缀的表已经没有了(conn):
    """2026-09-23 用户要求去掉统一前缀，`pending_state` 表跟着删了。

    老库（升级前建的）里那张表还留着，SQLite 会当没看见 —— 不影响。
    这条钉住的是**新库不再建它**，以及仓储层不再有那两个函数。
    """
    assert not hasattr(db, "get_prefix") and not hasattr(db, "set_prefix")
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_state'"
    ).fetchone() is None


def test_init_db_is_idempotent(tmp_path, monkeypatch):
    """每次启动都会再跑一遍 init_db —— 不能报错，也不能把已有的数据冲掉。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "data" / "staging")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "app.db")
    c = db.get_conn()
    db.init_db(c)
    pid = db.add_pending(c, "A1", "a.png", 10, "x.png", "ok", "", "upload")
    db.init_db(c)
    assert [r["id"] for r in db.list_pending(c)] == [pid]
    c.close()


def test_add_and_list_in_upload_order(conn):
    a = db.add_pending(conn, "甲", "甲.png", 10, "x.png", "ok", "", "upload")
    b = db.add_pending(conn, "乙", "乙.png", 20, "y.png", "ok", "", "upload")
    assert [r["id"] for r in db.list_pending(conn)] == [a, b]
    assert [r["name"] for r in db.list_pending(conn)] == ["甲", "乙"]


def test_failed_row_has_no_staged_file(conn):
    pid = db.add_pending(conn, "坏的", "坏.png", 0, None, "failed", "这不是图片", "upload")
    row = db.get_pending(conn, pid)
    assert row["staged_path"] is None
    assert row["status"] == "failed"
    assert row["reason"] == "这不是图片"


def test_set_pending_name(conn):
    pid = db.add_pending(conn, "旧", "旧.png", 1, "x.png", "ok", "", "upload")
    db.set_pending_name(conn, pid, "新")
    assert db.get_pending(conn, pid)["name"] == "新"


def test_set_pending_reason_marks_the_row_failed(conn):
    pid = db.add_pending(conn, "甲", "甲.png", 1, "x.png", "ok", "", "upload")
    db.set_pending_reason(conn, pid, "暂存的文件找不到了")
    row = db.get_pending(conn, pid)
    assert row["status"] == "failed"
    assert "找不到" in row["reason"]


def test_delete_and_clear(conn):
    a = db.add_pending(conn, "甲", "甲.png", 1, "x.png", "ok", "", "upload")
    db.add_pending(conn, "乙", "乙.png", 1, "y.png", "ok", "", "upload")
    db.delete_pending(conn, a)
    assert [r["name"] for r in db.list_pending(conn)] == ["乙"]
    assert db.clear_pending(conn) == 1
    assert db.list_pending(conn) == []


# ---------- folder_id / tags（2026-09-23） ----------

def test_add_pending_keeps_folder_and_tags(conn):
    pid = db.add_pending(conn, "A1-1", "IMG.jpg", 10, "x.png", "ok", "", "mobile",
                         folder_id=3, tags='["A1-1"]')
    row = db.get_pending(conn, pid)
    assert row["folder_id"] == 3
    assert json.loads(row["tags"]) == ["A1-1"]


def test_pending_defaults_to_no_folder_and_no_tags(conn):
    pid = db.add_pending(conn, "甲", "甲.png", 10, "x.png", "ok", "", "upload")
    row = db.get_pending(conn, pid)
    assert row["folder_id"] is None
    assert json.loads(row["tags"]) == []


def test_set_pending_folder_only_touches_one_row(conn):
    a = db.add_pending(conn, "甲", "甲.png", 10, "x.png", "ok", "", "upload")
    b = db.add_pending(conn, "乙", "乙.png", 10, "y.png", "ok", "", "upload")
    db.set_pending_folder(conn, a, 3)
    assert db.get_pending(conn, a)["folder_id"] == 3
    assert db.get_pending(conn, b)["folder_id"] is None


def test_set_all_pending_folders_touches_every_row(conn):
    a = db.add_pending(conn, "甲", "甲.png", 10, "x.png", "ok", "", "upload")
    b = db.add_pending(conn, "乙", "乙.png", 10, "y.png", "ok", "", "upload")
    db.set_all_pending_folders(conn, 5)
    assert db.get_pending(conn, a)["folder_id"] == 5
    assert db.get_pending(conn, b)["folder_id"] == 5
    db.set_all_pending_folders(conn, None)
    assert db.get_pending(conn, a)["folder_id"] is None


def test_create_image_stores_tags(conn):
    iid = db.create_image(conn, "A1-1", "a.jpg", "p.png", "mobile", tags='["A1-1"]')
    assert json.loads(db.get_image(conn, iid)["tags"]) == ["A1-1"]


def test_create_image_defaults_to_empty_tags(conn):
    iid = db.create_image(conn, "甲", "a.jpg", "p.png", "mobile")
    assert json.loads(db.get_image(conn, iid)["tags"]) == []


def test_migration_adds_the_two_new_columns_to_an_old_db(tmp_path):
    """老库（没有 folder_id / tags 两列）跑一遍 init_db 就得能用。

    SCHEMA 里的 CREATE TABLE IF NOT EXISTS 对已存在的表什么都不做 ——
    补列全靠 MIGRATIONS，这条测试钉住它真的补上了。
    """
    c = db.get_conn(tmp_path / "old.db")
    c.executescript("""
        CREATE TABLE pending_imports (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            name              TEXT    NOT NULL,
            original_filename TEXT    NOT NULL,
            size              INTEGER NOT NULL DEFAULT 0,
            staged_path       TEXT,
            status            TEXT    NOT NULL DEFAULT 'ok',
            reason            TEXT    NOT NULL DEFAULT '',
            import_source     TEXT    NOT NULL DEFAULT 'unknown',
            created_time      INTEGER NOT NULL
        );
    """)
    db.init_db(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(pending_imports)")}
    assert {"folder_id", "tags"} <= cols
    c.close()
