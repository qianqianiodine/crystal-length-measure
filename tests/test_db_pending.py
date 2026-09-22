"""待确认导入表的仓储层测试。"""
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


def test_prefix_defaults_to_empty(conn):
    assert db.get_prefix(conn) == ""


def test_set_prefix_round_trips(conn):
    db.set_prefix(conn, "EXP0615_")
    assert db.get_prefix(conn) == "EXP0615_"
    db.set_prefix(conn, "")
    assert db.get_prefix(conn) == ""


def test_init_db_is_idempotent_and_keeps_the_prefix(tmp_path, monkeypatch):
    """每次启动都会再跑一遍 init_db —— 前缀那行不能越插越多，也不能被冲掉。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "data" / "staging")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "app.db")
    c = db.get_conn()
    db.init_db(c)
    db.set_prefix(c, "X_")
    db.init_db(c)
    assert c.execute("SELECT COUNT(*) n FROM pending_state").fetchone()["n"] == 1
    assert db.get_prefix(c) == "X_"
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
