"""试用页接口测试。

覆盖的核心行为：**用户改圆 → 比例尺变 → 已有测量值自动重算，且线不用重画。**
"""
import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import annotate, config, imageio, main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """一个指向临时样品集、临时数据库的测试客户端。"""
    root = tmp_path / "样品集"
    root.mkdir()
    img = np.full((400, 400, 3), 30, np.uint8)
    cv2.circle(img, (200, 200), 100, (215, 215, 215), -1)
    imageio.save_rgb(root / "孔.png", img)

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(main, "SAMPLES_ROOT", root)
    with TestClient(main.app) as c:
        yield c


def _open(client, rel="孔.png") -> dict:
    r = client.post("/api/open", json={"rel": rel})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------- 照片列表与打开 ----------------

def test_list_photos(client):
    d = client.get("/api/photos").json()
    assert [p["rel"] for p in d["photos"]] == ["孔.png"]


def test_open_returns_state_with_circle_and_scale(client):
    st = _open(client)
    assert st["width"] == 400 and st["height"] == 400
    assert st["lines"] == []
    c = st["circle"]
    assert c is not None and c["a"] > 0 and c["b"] > 0
    # 比例尺必须由那个圆的长轴换算而来
    assert st["um_per_px"] == pytest.approx(3400.0 / (2 * max(c["a"], c["b"])))


def test_open_twice_reuses_image_and_keeps_work(client):
    st = _open(client)
    iid = st["image_id"]
    client.put(f"/api/image/{iid}/circle",
               json={"cx": 200, "cy": 200, "a": 100, "b": 100, "th": 0})
    client.post(f"/api/image/{iid}/lines",
                json={"x1": 0, "y1": 0, "x2": 10, "y2": 0, "note": "晶体A"})

    again = _open(client)                      # 重新打开同一张
    assert again["image_id"] == iid
    assert again["circle"]["a"] == pytest.approx(100)
    assert again["um_per_px"] == pytest.approx(17.0)
    assert [L["note"] for L in again["lines"]] == ["晶体A"]


def test_open_rejects_path_outside_samples(client):
    assert client.post("/api/open", json={"rel": "../../../windows/win.ini"}).status_code == 404
    assert client.post("/api/open", json={"rel": "不存在.png"}).status_code == 404


def test_image_file_is_served(client):
    iid = _open(client)["image_id"]
    r = client.get(f"/api/image/{iid}/file")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"


# ---------------- 定圆 → 比例尺 ----------------

def test_put_circle_sets_scale_from_major_axis(client):
    iid = _open(client)["image_id"]
    d = client.put(f"/api/image/{iid}/circle",
                   json={"cx": 200, "cy": 200, "a": 100, "b": 80, "th": 0.3}).json()
    # 短轴不参与换算：只认长轴 200px → 3400/200
    assert d["um_per_px"] == pytest.approx(17.0)


def test_put_circle_recomputes_existing_lines(client):
    """★ 核心：改圆之后，已经画好的线自动换算出新长度。"""
    iid = _open(client)["image_id"]
    client.put(f"/api/image/{iid}/circle",
               json={"cx": 200, "cy": 200, "a": 100, "b": 100, "th": 0})
    rec = client.post(f"/api/image/{iid}/lines",
                      json={"x1": 0, "y1": 0, "x2": 100, "y2": 0,
                            "note": "晶体A"}).json()
    assert rec["measured_um"] == pytest.approx(1700.0)

    # 圆改小一半 → 比例尺翻倍 → 同一条线应该变成两倍长
    d = client.put(f"/api/image/{iid}/circle",
                   json={"cx": 200, "cy": 200, "a": 50, "b": 50, "th": 0}).json()
    assert d["um_per_px"] == pytest.approx(34.0)
    line = d["lines"][0]
    assert line["measured_um"] == pytest.approx(3400.0)
    assert line["calib_snapshot"] == pytest.approx(34.0)
    assert line["note"] == "晶体A"          # 备注不受影响


def test_put_circle_rejects_zero_size(client):
    iid = _open(client)["image_id"]
    r = client.put(f"/api/image/{iid}/circle",
                   json={"cx": 200, "cy": 200, "a": 0, "b": 0, "th": 0})
    assert r.status_code == 400


def test_put_circle_unknown_image_is_404(client):
    r = client.put("/api/image/999/circle",
                   json={"cx": 0, "cy": 0, "a": 50, "b": 50, "th": 0})
    assert r.status_code == 404


# ---------------- 测量线 ----------------

def test_line_lifecycle(client):
    iid = _open(client)["image_id"]
    a = client.post(f"/api/image/{iid}/lines",
                    json={"x1": 0, "y1": 0, "x2": 30, "y2": 40}).json()
    b = client.post(f"/api/image/{iid}/lines",
                    json={"x1": 0, "y1": 0, "x2": 60, "y2": 0}).json()
    assert a["seq"] == 1 and b["seq"] == 2

    sid = client.get(f"/api/image/{iid}").json()["lines"]
    assert len(sid) == 2

    assert client.delete(f"/api/lines/{a['id']}").status_code == 200
    assert len(client.get(f"/api/image/{iid}").json()["lines"]) == 1

    assert client.post(f"/api/image/{iid}/undo").json()["removed"] is True
    assert client.get(f"/api/image/{iid}").json()["lines"] == []


def test_line_note_roundtrip(client):
    iid = _open(client)["image_id"]
    rec = client.post(f"/api/image/{iid}/lines",
                      json={"x1": 0, "y1": 0, "x2": 10, "y2": 0}).json()
    client.put(f"/api/lines/{rec['id']}/note", json={"note": "疑似盐晶"})
    assert client.get(f"/api/image/{iid}").json()["lines"][0]["note"] == "疑似盐晶"


def test_label_offset_defaults_to_zero_and_roundtrips(client):
    """长度标签的位置：默认贴在线段中点，拖过之后要能存下来、读回来。"""
    iid = _open(client)["image_id"]
    rec = client.post(f"/api/image/{iid}/lines",
                      json={"x1": 0, "y1": 0, "x2": 100, "y2": 0}).json()
    assert (rec["label_dx"], rec["label_dy"]) == (0.0, 0.0)

    assert client.put(f"/api/lines/{rec['id']}/label",
                      json={"dx": -30.5, "dy": 12.0}).status_code == 200

    line = client.get(f"/api/image/{iid}").json()["lines"][0]
    assert line["label_dx"] == pytest.approx(-30.5)
    assert line["label_dy"] == pytest.approx(12.0)
    # 挪标签只动显示位置，不能碰到测量值
    assert line["measured_um"] == pytest.approx(rec["measured_um"])
    assert line["seq"] == rec["seq"]


def test_init_db_adds_new_columns_to_an_old_database(tmp_path):
    """老库没有 label_dx/label_dy/circle 列，打开时要自动补上。

    CREATE TABLE IF NOT EXISTS 对已存在的表什么都不做，所以必须靠 ALTER。
    """
    import sqlite3

    from app import db

    p = tmp_path / "old.db"
    old = sqlite3.connect(str(p))
    old.executescript("""
        CREATE TABLE images (id INTEGER PRIMARY KEY, name TEXT, import_time INTEGER);
        CREATE TABLE calibrations (id INTEGER PRIMARY KEY, image_id INTEGER);
        CREATE TABLE measurements (id INTEGER PRIMARY KEY, image_id INTEGER, seq INTEGER);
    """)
    old.commit()
    old.close()

    conn = db.get_conn(p)
    db.init_db(conn)
    meas_cols = {r["name"] for r in conn.execute("PRAGMA table_info(measurements)")}
    calib_cols = {r["name"] for r in conn.execute("PRAGMA table_info(calibrations)")}
    assert {"label_dx", "label_dy"} <= meas_cols
    assert "circle" in calib_cols
    conn.close()


def test_scale_bar_position_roundtrips(client):
    """标尺拖到哪要能存下来、读回来，导出时才画得对。"""
    iid = _open(client)["image_id"]
    assert _open(client)["scale_bar"] is None

    assert client.put(f"/api/image/{iid}/scale-bar",
                      json={"x": 120.0, "y": 340.5}).status_code == 200
    assert client.get(f"/api/image/{iid}").json()["scale_bar"] == {"x": 120.0, "y": 340.5}

    # 传 null 恢复默认角落
    client.put(f"/api/image/{iid}/scale-bar", json={"x": None, "y": None})
    assert client.get(f"/api/image/{iid}").json()["scale_bar"] is None


def test_scale_bar_position_survives_recalibration(client):
    """重画定圆不该把用户摆好的标尺位置冲掉。"""
    iid = _open(client)["image_id"]
    client.put(f"/api/image/{iid}/scale-bar", json={"x": 100.0, "y": 200.0})
    client.put(f"/api/image/{iid}/circle",
               json={"cx": 200, "cy": 200, "a": 90, "b": 90, "th": 0})
    assert client.get(f"/api/image/{iid}").json()["scale_bar"] == {"x": 100.0, "y": 200.0}


def test_export_png_downloads_with_chinese_filename(client):
    iid = _open(client)["image_id"]
    client.post(f"/api/image/{iid}/lines", json={"x1": 0, "y1": 0, "x2": 100, "y2": 0})

    r = client.get(f"/api/image/{iid}/export.png", params={"supersample": 1})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    # 中文文件名要走 RFC 5987，否则浏览器下载下来叫 "download"
    assert "filename*=utf-8''" in r.headers["content-disposition"]

    back = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    assert back.shape == (400, 400, 3)


def test_export_png_focus_crop_is_smaller(client):
    iid = _open(client)["image_id"]
    client.post(f"/api/image/{iid}/lines", json={"x1": 190, "y1": 200, "x2": 210, "y2": 200})
    r = client.get(f"/api/image/{iid}/export.png",
                   params={"crop": "focus:1", "supersample": 1})
    assert r.status_code == 200
    back = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    assert back.shape[0] < 400 and back.shape[1] < 400


def test_export_png_clamps_supersample(client):
    """倍率超过 4 会被夹到 4，而不是照做 —— 照做能把内存吃满。"""
    iid = _open(client)["image_id"]
    r = client.get(f"/api/image/{iid}/export.png", params={"supersample": 99})
    assert r.status_code == 200
    back = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    assert back.shape == (1600, 1600, 3)          # 400 × 4


def test_export_png_reports_oversize_as_400(client, monkeypatch):
    """图太大时给一个看得懂的中文 400，不是 500 加一堆堆栈。"""
    monkeypatch.setattr(annotate, "MAX_OUTPUT_PIXELS", 1000)
    iid = _open(client)["image_id"]
    r = client.get(f"/api/image/{iid}/export.png", params={"supersample": 1})
    assert r.status_code == 400
    assert "太大" in r.json()["detail"]


def test_export_png_manual_rect_crop(client):
    """页面上拖出来的框走的是 rect: 这条路径。"""
    iid = _open(client)["image_id"]
    client.post(f"/api/image/{iid}/lines", json={"x1": 50, "y1": 50, "x2": 100, "y2": 50})
    r = client.get(f"/api/image/{iid}/export.png",
                   params={"crop": "rect:40,40,160,120", "supersample": 1})
    assert r.status_code == 200
    back = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    assert back.shape == (80, 120, 3)


def test_export_honours_moved_scale_bar(client):
    """用户把标尺拖到别处，导出图上也得在别处。"""
    iid = _open(client)["image_id"]
    before = client.get(f"/api/image/{iid}/export.png",
                        params={"supersample": 1}).content
    client.put(f"/api/image/{iid}/scale-bar", json={"x": 120.0, "y": 300.0})
    after = client.get(f"/api/image/{iid}/export.png",
                       params={"supersample": 1}).content
    assert before != after


def test_export_png_unknown_image_is_404(client):
    assert client.get("/api/image/999/export.png").status_code == 404


def test_export_csv_downloads(client):
    iid = _open(client)["image_id"]
    client.post(f"/api/image/{iid}/lines",
                json={"x1": 0, "y1": 0, "x2": 100, "y2": 0, "note": "晶体A"})

    r = client.get(f"/api/image/{iid}/export.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert r.text.startswith("﻿")          # Excel 要靠 BOM 认出 UTF-8
    assert "晶体A" in r.text
    assert "实际长度(μm)" in r.text


def test_export_does_not_change_anything(client):
    """★ 导出是只读的：导完再读状态，圆、比例尺、测量值一个都没动。"""
    iid = _open(client)["image_id"]
    client.put(f"/api/image/{iid}/circle",
               json={"cx": 200, "cy": 200, "a": 100, "b": 100, "th": 0})
    client.post(f"/api/image/{iid}/lines", json={"x1": 0, "y1": 0, "x2": 100, "y2": 0})
    before = client.get(f"/api/image/{iid}").json()

    client.get(f"/api/image/{iid}/export.png", params={"supersample": 1})
    client.get(f"/api/image/{iid}/export.csv")

    assert client.get(f"/api/image/{iid}").json() == before


def test_clear_lines(client):
    iid = _open(client)["image_id"]
    for i in range(3):
        client.post(f"/api/image/{iid}/lines",
                    json={"x1": 0, "y1": 0, "x2": 10 + i, "y2": 0})
    assert client.post(f"/api/image/{iid}/clear-lines").json()["removed"] == 3
    assert client.get(f"/api/image/{iid}").json()["lines"] == []


def test_zero_length_line_rejected(client):
    iid = _open(client)["image_id"]
    r = client.post(f"/api/image/{iid}/lines",
                    json={"x1": 5, "y1": 5, "x2": 5, "y2": 5})
    assert r.status_code == 400
