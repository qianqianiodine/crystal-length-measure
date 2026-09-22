"""批量操作接口（图库多选 → 批量归档 / 批量删除）。

⚠️ 必须 monkeypatch config 里的目录，否则会往用户真实的 data/ 里写东西。
"""
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, db, main


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


@pytest.fixture()
def conn(client):
    """和 TestClient 同一份库的另一条连接 —— 断言「库里真删了没」要用它。"""
    c = db.get_conn(config.DB_PATH)
    yield c
    c.close()


def _png(val=200) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (80, 60), (val, val, val)).save(buf, format="PNG")
    return buf.getvalue()


def _add(client, name, val=200) -> int:
    r = client.post("/api/images/upload",
                    files=[("files", (f"{name}.png", _png(val), "image/png"))])
    return r.json()["ok"][0]["id"]


def _measure(client, conn, iid):
    db.add_measurement(conn, iid, 0, 0, 10, 0, 10.0, 5.0, 0.5)


# ---------------- GET /api/images/ids ----------------

def test_ids_matches_the_grid(client):
    """「选中全部」选的东西必须和格子墙筛出来的是同一批。"""
    _add(client, "甲", 10)
    _add(client, "乙", 20)
    f1 = client.post("/api/folders", json={"name": "实验A"}).json()["id"]
    third = _add(client, "丙", 30)
    client.put(f"/api/images/{third}/folders", json={"folder_ids": [f1]})

    for params in ({}, {"status": "unmeasured"}, {"status": "measured"},
                   {"folder": f1}, {"folder": 0}, {"q": "甲"}):
        grid = [i["id"] for i in client.get(
            "/api/images", params={**params, "limit": 100}).json()["items"]]
        ids = client.get("/api/images/ids", params=params).json()["ids"]
        assert sorted(ids) == sorted(grid), params


def test_ids_uncategorized(client):
    _add(client, "甲", 10)
    f1 = client.post("/api/folders", json={"name": "实验A"}).json()["id"]
    b = _add(client, "乙", 20)
    client.put(f"/api/images/{b}/folders", json={"folder_ids": [f1]})
    j = client.get("/api/images/ids?folder=0").json()
    assert j["total"] == 1
    assert len(j["ids"]) == 1


# ---------------- limit 上限 ----------------

def test_list_limit_can_reach_1000(client):
    """测量页的下拉框要一次列 1000 张。上限卡在 500 的话它会被 422 顶回来。"""
    assert client.get("/api/images?limit=1000").status_code == 200
    assert client.get("/api/images?limit=1001").status_code == 422


# ---------------- batch-delete ----------------

def test_batch_delete_dry_run_changes_nothing(client, conn):
    ids = [_add(client, "甲", 10), _add(client, "乙", 20), _add(client, "丙", 30)]
    j = client.post("/api/images/batch-delete",
                    json={"image_ids": ids, "dry_run": True}).json()
    assert j == {"count": 3, "measured": 0}
    assert db.count_images(conn) == 3                      # 一张都没动


def test_batch_delete_dry_run_counts_measured(client, conn):
    a, b = _add(client, "甲", 10), _add(client, "乙", 20)
    _measure(client, conn, a)
    j = client.post("/api/images/batch-delete",
                    json={"image_ids": [a, b], "dry_run": True}).json()
    assert j == {"count": 2, "measured": 1}


def test_batch_delete_removes_and_trashes(client, conn):
    ids = [_add(client, "甲", 10), _add(client, "乙", 20)]
    j = client.post("/api/images/batch-delete", json={"image_ids": ids}).json()
    assert j == {"deleted": 2, "moved_to_trash": 2}
    assert db.count_images(conn) == 0
    assert len(list((config.DATA_DIR / "trash").glob("*"))) == 2


def test_batch_delete_cascades_measurements(client, conn):
    a = _add(client, "甲", 10)
    _measure(client, conn, a)
    client.post("/api/images/batch-delete", json={"image_ids": [a]})
    assert conn.execute("SELECT COUNT(*) c FROM measurements").fetchone()["c"] == 0


def test_batch_delete_skips_missing_ids(client, conn):
    """照片可能在别的窗口被删了。跳过它，其余的照删 —— 为它让整批失败不值得。"""
    a, b = _add(client, "甲", 10), _add(client, "乙", 20)
    j = client.post("/api/images/batch-delete",
                    json={"image_ids": [a, 9999, b]}).json()
    assert j["deleted"] == 2
    assert db.count_images(conn) == 0


def test_batch_delete_empty_is_400(client):
    assert client.post("/api/images/batch-delete", json={"image_ids": []}).status_code == 400


def test_batch_delete_leaves_sample_folder_alone(client, conn, tmp_path):
    """老记录指向样品集/ —— 铁律是绝不碰那些原图。

    造一条绝对路径的记录，指向一个真文件；删完之后文件必须还在、内容没变。
    """
    outside = tmp_path / "样品集"
    outside.mkdir()
    f = outside / "样品2_1.png"
    f.write_bytes(_png(7))
    before = f.stat().st_mtime
    iid = db.create_image(conn, "老记录", "样品2_1.png", str(f), "sample")

    j = client.post("/api/images/batch-delete", json={"image_ids": [iid]}).json()
    assert j == {"deleted": 1, "moved_to_trash": 0}        # 一个都没挪
    assert f.is_file() and f.read_bytes() == _png(7)
    assert f.stat().st_mtime == before


# ---------------- batch-folders ----------------

def test_batch_folders_add_keeps_old(client, conn):
    f1 = client.post("/api/folders", json={"name": "实验A"}).json()["id"]
    f2 = client.post("/api/folders", json={"name": "实验B"}).json()["id"]
    a = _add(client, "甲", 10)
    client.put(f"/api/images/{a}/folders", json={"folder_ids": [f1]})

    j = client.post("/api/images/batch-folders",
                    json={"image_ids": [a], "folder_ids": [f2], "mode": "add"}).json()
    assert j == {"updated": 1}
    assert db.image_folders(conn, a) == sorted([f1, f2])


def test_batch_folders_replace_drops_old(client, conn):
    f1 = client.post("/api/folders", json={"name": "实验A"}).json()["id"]
    f2 = client.post("/api/folders", json={"name": "实验B"}).json()["id"]
    a = _add(client, "甲", 10)
    client.put(f"/api/images/{a}/folders", json={"folder_ids": [f1]})

    client.post("/api/images/batch-folders",
                json={"image_ids": [a], "folder_ids": [f2], "mode": "replace"})
    assert db.image_folders(conn, a) == [f2]


def test_batch_folders_replace_with_nothing_is_uncategorized(client, conn):
    f1 = client.post("/api/folders", json={"name": "实验A"}).json()["id"]
    a = _add(client, "甲", 10)
    client.put(f"/api/images/{a}/folders", json={"folder_ids": [f1]})

    client.post("/api/images/batch-folders",
                json={"image_ids": [a], "folder_ids": [], "mode": "replace"})
    assert db.image_folders(conn, a) == []


def test_batch_folders_unknown_folder_is_400(client):
    a = _add(client, "甲", 10)
    r = client.post("/api/images/batch-folders",
                    json={"image_ids": [a], "folder_ids": [9999], "mode": "add"})
    assert r.status_code == 400


def test_batch_folders_empty_ids_is_400(client):
    r = client.post("/api/images/batch-folders",
                    json={"image_ids": [], "folder_ids": [], "mode": "add"})
    assert r.status_code == 400


def test_batch_folders_bad_mode_is_400(client):
    a = _add(client, "甲", 10)
    r = client.post("/api/images/batch-folders",
                    json={"image_ids": [a], "folder_ids": [], "mode": "overwrite"})
    assert r.status_code == 400
