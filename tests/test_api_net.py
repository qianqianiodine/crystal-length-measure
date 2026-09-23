"""手机传图那块地址的拼装：二维码和旁边那段文字 URL 必须一致，
而且都要带上「你在图库看的那个文件夹」。"""
import pytest
from fastapi.testclient import TestClient

from app import config, main
from app.api import mobile


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "data" / "images" / "originals")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "data" / "images" / "cache")
    monkeypatch.setattr(config, "THUMBS_DIR", tmp_path / "data" / "thumbs")
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "data" / "staging")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "app.db")
    # 这台机器上有哪些网卡不归测试管：给一个固定的，地址才可比
    monkeypatch.setattr(mobile.storage, "local_ips", lambda: ["192.168.1.5"])
    config.ensure_dirs()
    with TestClient(main.app) as c:
        c.app.state.port = 8510
        yield c


def test_地址带folder时拼进URL(client):
    d = client.get("/api/net/info?folder=3").json()
    assert d["urls"] and all(u.endswith("?folder=3") for u in d["urls"]), d["urls"]


def test_不带folder时URL不带问号(client):
    d = client.get("/api/net/info").json()
    assert d["urls"] and all("?" not in u for u in d["urls"]), d["urls"]


def test_二维码接口接受folder(client):
    r = client.get("/api/net/qrcode?folder=3")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/png")
