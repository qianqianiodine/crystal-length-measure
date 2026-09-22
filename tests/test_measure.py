"""测量记录管理测试。"""
import pytest

from app import calibrate, config, db, measure


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    """把「用户设的孔直径」隔离到 tmp_path。

    ⚠️ 不隔离的话，from_well_diameter(100.0) 会去读**用户真实的**
    `data/settings.json`（见 app/settings.py）—— 用户把孔直径改成 2.5mm 之后，
    下面断言 34 um/px 的用例会集体变红。
    """
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")


@pytest.fixture()
def ready_conn(tmp_path):
    """已建库、已建图、已标定 34 um/px 的连接。"""
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(c)
    iid = db.create_image(c, "A", "a.jpg", "p.jpg", "drag")
    calibrate.apply_calibration(c, iid, calibrate.from_well_diameter(100.0))
    yield c, iid
    c.close()


def test_add_line_records_and_computes(ready_conn):
    conn, iid = ready_conn
    rec = measure.add_line(conn, iid, 0, 0, 3, 4, note="晶体A")
    assert rec["seq"] == 1
    assert rec["pixel_length"] == pytest.approx(5.0)
    assert rec["measured_um"] == pytest.approx(170.0)     # 5 × 34
    assert rec["calib_snapshot"] == pytest.approx(34.0)
    assert rec["note"] == "晶体A"


def test_add_line_without_calibration_raises(tmp_path):
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(c)
    iid = db.create_image(c, "A", "a.jpg", "p.jpg", "drag")
    with pytest.raises(ValueError):
        measure.add_line(c, iid, 0, 0, 10, 10)
    c.close()


def test_add_line_rejects_zero_length(ready_conn):
    conn, iid = ready_conn
    with pytest.raises(ValueError):
        measure.add_line(conn, iid, 10, 10, 10, 10)


def test_sequence_and_colors(ready_conn):
    conn, iid = ready_conn
    a = measure.add_line(conn, iid, 0, 0, 10, 0)
    b = measure.add_line(conn, iid, 0, 0, 20, 0)
    c = measure.add_line(conn, iid, 0, 0, 30, 0)
    assert [a["seq"], b["seq"], c["seq"]] == [1, 2, 3]
    assert len({a["color"], b["color"], c["color"]}) >= 2   # 前几条颜色应不同


def test_undo_last(ready_conn):
    conn, iid = ready_conn
    measure.add_line(conn, iid, 0, 0, 10, 0)
    measure.add_line(conn, iid, 0, 0, 20, 0)
    assert measure.undo_last(conn, iid) is True
    lines = measure.list_lines(conn, iid)
    assert len(lines) == 1 and lines[0]["seq"] == 1
    measure.undo_last(conn, iid)
    assert measure.undo_last(conn, iid) is False          # 已空


def test_clear_all(ready_conn):
    conn, iid = ready_conn
    for i in range(4):
        measure.add_line(conn, iid, 0, 0, 10 + i, 0)
    assert measure.clear_all(conn, iid) == 4
    assert measure.list_lines(conn, iid) == []


def test_delete_single_line(ready_conn):
    conn, iid = ready_conn
    a = measure.add_line(conn, iid, 0, 0, 10, 0)
    measure.add_line(conn, iid, 0, 0, 20, 0)
    measure.delete_line(conn, a["id"])
    lines = measure.list_lines(conn, iid)
    assert len(lines) == 1
    assert lines[0]["seq"] == 2       # 剩余记录的 seq 不重排（保持历史编号）


def test_update_note(ready_conn):
    conn, iid = ready_conn
    rec = measure.add_line(conn, iid, 0, 0, 10, 0)
    measure.update_note(conn, rec["id"], "疑似盐晶")
    assert measure.list_lines(conn, iid)[0]["note"] == "疑似盐晶"


def test_meets_precision_target():
    assert measure.meets_precision_target(102.0, 100.0, 2.0) is True
    assert measure.meets_precision_target(103.0, 100.0, 2.0) is False
    assert measure.meets_precision_target(100.0, 100.0, 2.0) is True


def test_color_assignment_cycles():
    """颜色表应循环使用且不越界。"""
    assert measure.color_for_seq(1) != measure.color_for_seq(2)
    c1 = measure.color_for_seq(1)
    c2 = measure.color_for_seq(1 + len(measure.LINE_COLORS))
    assert c1 == c2
