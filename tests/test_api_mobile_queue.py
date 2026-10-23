"""手机离线队列上传：name / tag / uid。

这些是 2026-10-22「手机离线拍照、回有网自动补传」加的参数。
重点只有两个：
1. 拍照时填的名字、孔位、文件夹必须原样入库；
2. 同一个 uid 重传（网络差时很常见）绝不能变成两张照片、两份原图。
"""
import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import config, main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "data" / "images" / "originals")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "data" / "images" / "cache")
    monkeypatch.setattr(config, "THUMBS_DIR", tmp_path / "data" / "thumbs")
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "data" / "staging")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "app.db")
    config.ensure_dirs()
    with TestClient(main.app) as c:
        yield c


def _png(val=200) -> bytes:
    ok, buf = cv2.imencode(".png", np.full((60, 80, 3), val, np.uint8))
    assert ok
    return buf.tobytes()


def _upload(client, *, name=None, tag=None, uid=None, folder=None,
            data=None, filename="IMG_0001.png"):
    params = {"mode": "direct"}
    if folder is not None:
        params["folder"] = str(folder)
    form = {}
    if name is not None:
        form["name"] = name
    if tag is not None:
        form["tag"] = tag
    if uid is not None:
        form["uid"] = uid
    return client.post(f"/m/{main.app.state.mobile_token}/upload",
                       params=params,
                       files=[("files", (filename, data if data is not None else _png(),
                                         "image/png"))],
                       data=form)


def _items(client) -> list[dict]:
    return client.get("/api/images").json()["items"]


def test_offline_name_is_used_as_is(client):
    r = _upload(client, name="20261022-3-B-5-1")
    assert r.status_code == 200, r.text
    j = r.json()
    assert [i["name"] for i in j["ok"]] == ["20261022-3-B-5-1"]
    assert [i["name"] for i in _items(client)] == ["20261022-3-B-5-1"]


def test_offline_tag_is_written(client):
    r = _upload(client, name="20261022-3-B-5-1", tag="B5-1")
    assert r.status_code == 200, r.text
    assert _items(client)[0]["tags"] == ["B5-1"]


def test_bad_tag_is_rejected_with_a_chinese_reason(client):
    r = _upload(client, tag="B5")
    assert r.status_code == 400
    assert "A1-1" in r.json()["detail"]
    assert _items(client) == []


def test_without_name_still_falls_back_to_the_filename(client):
    r = _upload(client, filename="IMG_7788.png")
    assert r.status_code == 200
    assert [i["name"] for i in r.json()["ok"]] == ["IMG_7788"]


def test_bad_name_lands_in_failed_not_a_500(client):
    r = _upload(client, name="x" * 101)
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] == []
    assert "100" in j["failed"][0]["reason"]
    assert _items(client) == []


def test_same_uid_imports_only_once(client):
    uid = "phone-3f2a"
    r1 = _upload(client, name="A-1-1", uid=uid)
    r2 = _upload(client, name="A-1-1", uid=uid)
    assert r1.json()["duplicate"] is False
    assert r2.json()["duplicate"] is True
    assert [i["name"] for i in _items(client)] == ["A-1-1"]
    # 第二次在读文件之前就判掉了：原图目录里只能有一份
    assert len(list(config.IMAGES_DIR.glob("*"))) == 1


def test_same_uid_keeps_the_first_name(client):
    """重试时名字可能被用户改过；已经入库的那张不能被第二次的名字覆盖。"""
    uid = "phone-keep-first"
    _upload(client, name="第一次的名字", uid=uid)
    r2 = _upload(client, name="第二次的名字", uid=uid)
    assert r2.json()["duplicate"] is True
    assert [i["name"] for i in _items(client)] == ["第一次的名字"]


def test_without_uid_two_uploads_are_two_photos(client):
    _upload(client, name="A", data=_png(10))
    _upload(client, name="A", data=_png(20))
    assert len(_items(client)) == 2


def test_offline_upload_keeps_the_folder(client):
    fid = client.post("/api/folders", json={"name": "晶体房"}).json()["id"]
    r = _upload(client, name="A-1-1", folder=fid)
    assert r.status_code == 200
    assert _items(client)[0]["folders"] == [fid]
