"""打标签 API 测试（`POST /api/images/batch-tags`），以及图库列表带上标签。

⚠️ 单独一个文件，不追加到 tests/test_api_images.py ——
那个文件正被另一条工作流改着（见 project 约定「共享测试文件不再碰」）。

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


# ---------------- 造图（抄自 tests/test_api_crop.py:35-49） ----------------

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


# ---------------- 打标签 ----------------

def test_批量打标签(client):
    i1, i2 = _one(client), _one(client)
    r = client.post("/api/images/batch-tags", json={"items": [
        {"id": i1, "tags": ["A1-1"]},
        {"id": i2, "tags": ["A1-2"]},
    ]})
    assert r.status_code == 200 and r.json()["updated"] == 2
    got = {it["id"]: it["tags"] for it in client.get("/api/images").json()["items"]}
    assert got[i1] == ["A1-1"] and got[i2] == ["A1-2"]


def test_打标签_能清空(client):
    iid = _one(client)
    client.post("/api/images/batch-tags", json={"items": [{"id": iid, "tags": ["A1-1"]}]})
    client.post("/api/images/batch-tags", json={"items": [{"id": iid, "tags": []}]})
    assert client.get("/api/images").json()["items"][0]["tags"] == []


def test_打标签_不存在的_id_跳过(client):
    iid = _one(client)
    r = client.post("/api/images/batch-tags", json={"items": [
        {"id": iid, "tags": ["B2-1"]}, {"id": 99999, "tags": ["C3-2"]},
    ]})
    assert r.status_code == 200 and r.json()["updated"] == 1


@pytest.mark.parametrize("坏", ["Z9", "A99", "A1", "A1-3", "a1-1", "A0-1", "甲", "",
                                "A1-1\n"])
def test_打标签_形状不对就_400(client, 坏):
    iid = _one(client)
    r = client.post("/api/images/batch-tags", json={"items": [{"id": iid, "tags": [坏]}]})
    assert r.status_code == 400


def test_打标签_收两位数的列号(client):
    """列号 10~12 是两位数 —— 这条专门钉住「收」的这一侧。

    只测「拒」是不够的：把正则里的 `1[0-2]` 削成 `[1-9]`，所有拒绝用例照样全绿，
    而 A10-1 这种真实孔位会被 400 挡在门外，谁都没发现。
    """
    i1, i2 = _one(client), _one(client)
    r = client.post("/api/images/batch-tags", json={"items": [
        {"id": i1, "tags": ["A10-1"]},
        {"id": i2, "tags": ["H12-2"]},
    ]})
    assert r.status_code == 200 and r.json()["updated"] == 2
    got = {it["id"]: it["tags"] for it in client.get("/api/images").json()["items"]}
    assert got[i1] == ["A10-1"] and got[i2] == ["H12-2"]


def test_打标签_一个坏标签整批不写(client):
    """两个 pass 的设计：先验**全部**，再写。所以混进一个坏标签时，合法的那条也不许动。

    要是改成一验一写，用户看到 400 以为整批没生效，其实前几张已经被悄悄改掉了。
    """
    i1, i2 = _one(client), _one(client)
    client.post("/api/images/batch-tags", json={"items": [{"id": i1, "tags": ["A1-1"]}]})

    r = client.post("/api/images/batch-tags", json={"items": [
        {"id": i1, "tags": ["B2-1"]},        # 合法，但不该被写进去
        {"id": i2, "tags": ["坏标签"]},       # 非法 → 整批 400
    ]})
    assert r.status_code == 400
    got = {it["id"]: it["tags"] for it in client.get("/api/images").json()["items"]}
    assert got[i1] == ["A1-1"] and got[i2] == []


def test_打标签_空列表报_400(client):
    assert client.post("/api/images/batch-tags", json={"items": []}).status_code == 400
