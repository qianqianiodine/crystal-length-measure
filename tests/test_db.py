"""数据库 schema 与仓储层测试。"""
import json

import pytest

from app import db


@pytest.fixture()
def conn(tmp_path):
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(c)
    yield c
    c.close()


def test_create_and_get_image(conn):
    iid = db.create_image(conn, name="实验A", original_filename="IMG_1.jpg",
                          path="data/images/originals/1.jpg", import_source="drag")
    row = db.get_image(conn, iid)
    assert row["name"] == "实验A"
    assert row["favorite"] == 0
    assert row["transform"] is None
    assert row["import_time"] > 0


def test_init_db_is_idempotent(conn):
    db.init_db(conn)      # 二次调用不应报错
    db.init_db(conn)
    assert db.count_images(conn) == 0


def test_calibration_unique_per_image(conn):
    """同一张图重复标定应覆盖，不产生两行。"""
    iid = db.create_image(conn, "A", "a.jpg", "p.jpg", "drag")
    db.set_calibration(conn, iid, "well_diameter", 77.0, 3400.0)
    db.set_calibration(conn, iid, "manual", 100.0, 3400.0)
    rows = conn.execute("SELECT COUNT(*) c FROM calibrations WHERE image_id=?", (iid,)).fetchone()
    assert rows["c"] == 1
    assert db.get_calibration(conn, iid)["um_per_px"] == pytest.approx(34.0)


def test_calibration_computes_um_per_px(conn):
    iid = db.create_image(conn, "A", "a.jpg", "p.jpg", "drag")
    db.set_calibration(conn, iid, "well_diameter", 106.25, 3400.0)
    assert db.get_calibration(conn, iid)["um_per_px"] == pytest.approx(32.0)


def test_measurement_seq_increments(conn):
    iid = db.create_image(conn, "A", "a.jpg", "p.jpg", "drag")
    assert db.next_seq(conn, iid) == 1
    db.add_measurement(conn, iid, 0, 0, 10, 10, 14.14, 100.0, 7.07, "晶体A")
    assert db.next_seq(conn, iid) == 2
    db.add_measurement(conn, iid, 5, 5, 20, 20, 21.21, 150.0, 7.07)
    ms = db.list_measurements(conn, iid)
    assert [m["seq"] for m in ms] == [1, 2]
    assert ms[0]["note"] == "晶体A"


def test_delete_image_cascades(conn):
    """删图必须同时删掉其标定与测量记录。"""
    iid = db.create_image(conn, "A", "a.jpg", "p.jpg", "drag")
    db.set_calibration(conn, iid, "manual", 100.0, 3400.0)
    db.add_measurement(conn, iid, 0, 0, 10, 10, 14.14, 100.0, 34.0)
    db.delete_image(conn, iid)
    assert db.get_image(conn, iid) is None
    assert db.get_calibration(conn, iid) is None
    assert db.list_measurements(conn, iid) == []


def test_update_image_only_allows_whitelist(conn):
    iid = db.create_image(conn, "A", "a.jpg", "p.jpg", "drag")
    db.update_image(conn, iid, name="新名", favorite=1,
                    transform=json.dumps({"H": None}))
    row = db.get_image(conn, iid)
    assert row["name"] == "新名"
    assert row["favorite"] == 1
    assert json.loads(row["transform"])["H"] is None
    with pytest.raises(ValueError):
        db.update_image(conn, iid, id=999)     # 非白名单字段必须拒绝


def test_list_images_pagination(conn):
    for i in range(5):
        db.create_image(conn, f"图{i}", f"{i}.jpg", f"p{i}.jpg", "drag")
    assert db.count_images(conn) == 5
    page = db.list_images(conn, limit=2, offset=0)
    assert len(page) == 2


def test_crop_column_migration(tmp_path):
    """老库（没有 crop 列）打开后自动补上，老记录是 NULL = 不裁剪。"""
    import sqlite3

    from app import db

    p = tmp_path / "old.db"
    # 手工造一个"加 crop 列之前"的库：images 表照 SCHEMA 建，但把 crop 去掉
    old = sqlite3.connect(str(p))
    old.execute("""CREATE TABLE images (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        original_filename TEXT NOT NULL, path TEXT NOT NULL,
        import_time INTEGER NOT NULL, import_source TEXT NOT NULL DEFAULT 'unknown',
        tags TEXT NOT NULL DEFAULT '[]', favorite INTEGER NOT NULL DEFAULT 0,
        transform TEXT, thumb_path TEXT, enhance TEXT)""")
    old.execute("INSERT INTO images (name, original_filename, path, import_time)"
                " VALUES ('老照片','a.jpg','a.jpg',1)")
    old.commit()
    old.close()

    conn = db.get_conn(p)
    db.init_db(conn)                     # 幂等：建表 + 补列
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(images)")}
    assert "crop" in cols
    assert db.get_image(conn, 1)["crop"] is None
    db.init_db(conn)                     # 再跑一次不该炸（幂等）
    conn.close()


def test_new_db_has_crop_column(tmp_path):
    from app import db

    conn = db.get_conn(tmp_path / "new.db")
    db.init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(images)")}
    assert "crop" in cols
    conn.close()
