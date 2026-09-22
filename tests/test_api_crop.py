"""裁剪的写入口：`PUT /api/image/{id}/crop`，以及打开照片 / 图库列表带上裁剪框。

⚠️ 单独一个文件，不追加到 tests/test_api.py / tests/test_api_images.py ——
那两个文件正被另一条工作流改着（见 project 约定「共享测试文件不再碰」）。

⚠️ 必须 monkeypatch config 里的目录，否则会往用户真实的 data/ 里写东西。
"""
import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import config, main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """一个把数据目录全挪进 tmp_path 的测试客户端。

    抄自 tests/test_api_images.py:16-27（同一个形状：六个目录 + ensure_dirs）。
    """
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "data" / "images" / "originals")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "data" / "images" / "cache")
    monkeypatch.setattr(config, "THUMBS_DIR", tmp_path / "data" / "thumbs")
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "data" / "staging")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "app.db")
    config.ensure_dirs()
    with TestClient(main.app) as c:
        yield c


# ---------------- 造图（抄自 tests/test_api_images.py 的 _png / _upload / _one） ----------------

def _png(w=80, h=60, val=200) -> bytes:
    ok, buf = cv2.imencode(".png", np.full((h, w, 3), val, np.uint8))
    assert ok
    return buf.tobytes()


def _upload(client, name="照片.png", data=None):
    return client.post("/api/images/upload",
                       files=[("files", (name, data if data is not None else _png(),
                                         "image/png"))])


def _one(client) -> int:
    """上传一张 80×60 的灰图，返回它的 id。"""
    return _upload(client).json()["ok"][0]["id"]


# ---------------- 白名单 ----------------

def test_update_image_能写_crop(tmp_path):
    """加列的时候忘了加白名单 —— update_image 会直接抛 ValueError。
    这条钉住它们俩必须一起在。"""
    from app import db

    conn = db.get_conn(tmp_path / "t.db")
    db.init_db(conn)
    iid = db.create_image(conn, "甲", "a.jpg", "a.jpg", "upload")
    db.update_image(conn, iid, crop='{"x0":1,"y0":2,"x1":41,"y1":32}')
    assert db.get_image(conn, iid)["crop"] == '{"x0":1,"y0":2,"x1":41,"y1":32}'
    db.update_image(conn, iid, crop=None)          # 取消裁剪也要写得进
    assert db.get_image(conn, iid)["crop"] is None
    conn.close()


def test_update_image_白名单外照样拒绝(tmp_path):
    from app import db

    conn = db.get_conn(tmp_path / "t.db")
    db.init_db(conn)
    iid = db.create_image(conn, "甲", "a.jpg", "a.jpg", "upload")
    with pytest.raises(ValueError):
        db.update_image(conn, iid, id=999)          # 白名单本身没被放松
    conn.close()


# ---------------- 裁剪 ----------------

def test_put_crop_存得进读得出(client):
    image_id = _one(client)
    r = client.put(f"/api/image/{image_id}/crop",
                   json={"x0": 10, "y0": 20, "x1": 60, "y1": 80})
    assert r.status_code == 200
    assert r.json()["crop"] == {"x0": 10.0, "y0": 20.0, "x1": 60.0, "y1": 80.0}

    d = client.get(f"/api/image/{image_id}").json()
    assert d["crop"] == {"x0": 10.0, "y0": 20.0, "x1": 60.0, "y1": 80.0}


def test_put_crop_全不给等于取消(client):
    image_id = _one(client)
    client.put(f"/api/image/{image_id}/crop", json={"x0": 10, "y0": 20, "x1": 60, "y1": 80})
    r = client.put(f"/api/image/{image_id}/crop", json={})
    assert r.status_code == 200 and r.json()["crop"] is None
    assert client.get(f"/api/image/{image_id}").json()["crop"] is None


def test_put_crop_只给一半就报错(client):
    image_id = _one(client)
    r = client.put(f"/api/image/{image_id}/crop", json={"x0": 10, "y0": 20, "x1": 60})
    assert r.status_code == 400
    assert "裁剪" in r.json()["detail"]


def test_put_crop_太小的框不要(client):
    image_id = _one(client)
    r = client.put(f"/api/image/{image_id}/crop",
                   json={"x0": 10, "y0": 20, "x1": 14, "y1": 80})     # 宽只有 4
    assert r.status_code == 400
    assert "太小" in r.json()["detail"]


def test_put_crop_不是有限数(client):
    """`NaN` / `Infinity` 不是合法 JSON，得用字符串蒙混 —— 服务端必须挡住，
    不然它会一路写进库，渲染时才炸。"""
    image_id = _one(client)
    r = client.put(f"/api/image/{image_id}/crop",
                   content='{"x0": 1, "y0": 2, "x1": NaN, "y1": 50}',
                   headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_put_crop_图不存在(client):
    assert client.put("/api/image/99999/crop",
                      json={"x0": 0, "y0": 0, "x1": 50, "y1": 50}).status_code == 404


# ---------------- 跨端点：缩略图与图库列表 ----------------

def _thumb_src(client):
    """上传一张图，返回（id, 原图路径, 缩略图路径）。抄自 tests/test_api_images.py:518-526。"""
    from app import db, storage, thumbs

    iid = _one(client)
    conn = db.get_conn(config.DB_PATH)
    row = db.get_image(conn, iid)
    conn.close()
    return iid, storage.resolve_path(row["path"]), thumbs.path_for(iid)


def test_裁完缩略图立刻换新(client):
    """和图库卡片对得上 —— 裁完去图库看到的就是裁过的。"""
    iid, src, dst = _thumb_src(client)
    first = client.get(f"/api/images/{iid}/thumb").content

    r = client.put(f"/api/image/{iid}/crop",
                   json={"x0": 10, "y0": 5, "x1": 50, "y1": 35})
    assert r.status_code == 200
    assert not dst.exists(), "裁完旧缩略图必须删掉"
    assert client.get(f"/api/images/{iid}/thumb").content != first


def test_图库列表带_crop(client):
    iid, src, dst = _thumb_src(client)
    assert client.get("/api/images").json()["items"][0]["crop"] is None

    client.put(f"/api/image/{iid}/crop", json={"x0": 1, "y0": 2, "x1": 41, "y1": 32})
    assert client.get("/api/images").json()["items"][0]["crop"] == \
        {"x0": 1.0, "y0": 2.0, "x1": 41.0, "y1": 32.0}
