"""批量导出图片 API 测试（`POST /api/images/export-images`）。

⚠️ 单独一个文件，不追加到 tests/test_api_images.py ——
那个文件正被另一条工作流改着（见 project 约定「共享测试文件不再碰」）。

⚠️ 必须 monkeypatch config 里的目录，否则会往用户真实的 data/ 和 导出/ 里写东西。
"""
import os

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import config, main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """一个把数据目录全挪进 tmp_path 的测试客户端。

    比 tests/test_api_crop.py 的那份多两样：
    - `EXPORT_DIR`（本文件独有 —— 别的文件不导出，不需要它）
    - `os.startfile` 的**挡板**。不加的话每跑一条测试，用户桌面上就真的弹出
      一个资源管理器窗口（弹的还是 pytest 临时目录，跑完就被删了）。
      8 条测试 = 8 个窗口。挡板只能加在这里，不要去改端点的逻辑。
    """
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "data" / "images" / "originals")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "data" / "images" / "cache")
    monkeypatch.setattr(config, "THUMBS_DIR", tmp_path / "data" / "thumbs")
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "data" / "staging")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "app.db")
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "导出")
    monkeypatch.setattr(os, "startfile", lambda p: None, raising=False)
    config.ensure_dirs()
    with TestClient(main.app) as c:
        yield c


# ---------------- 造图（抄自 tests/test_api_crop.py:35-49，各文件自带一份是本仓库的既有约定） ----------------

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


def _out_dir(j) -> "config.Path":
    """这次导出的落地目录。`dir` 是「导出/<时间戳>」，取最后一段。"""
    return config.EXPORT_DIR / j["dir"].split("/")[-1]


# ---------------- 批量导出图片 ----------------

def test_导出图片_建出文件夹和文件(client):
    """最全的一条：两层文件夹 + 标签命名 + 没标签时退回原文件名。"""
    from app import db

    a = client.post("/api/folders", json={"name": "样品A", "parent_id": None}).json()
    b = client.post("/api/folders", json={"name": "6月批次", "parent_id": a["id"]}).json()
    i1 = _one(client)
    i2 = _one(client)
    r = client.post("/api/images/batch-folders",
                    json={"image_ids": [i1, i2], "folder_ids": [b["id"]], "mode": "replace"})
    assert r.status_code == 200, r.text

    # ⚠️ 标签**直接写库**，不要调 `POST /api/images/batch-tags` —— 那个端点是 Task 10
    # 才做的，本任务里它还 404。走 API 的话这一行会静默失败（没断言状态码），
    # i1 拿不到标签，下面那条断言就会红得让人摸不着头脑。
    # `images.tags` 就是一列 JSON 数组字符串，`tags` 在 db.UPDATABLE_FIELDS 白名单里。
    conn = db.get_conn(config.DB_PATH)
    db.update_image(conn, i1, tags='["A1-1"]')
    conn.close()

    r = client.post("/api/images/export-images", json={"image_ids": [i1, i2]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["count"] == 2 and j["renamed"] == 0 and j["failed"] == []

    d = _out_dir(j)
    assert (d / "样品A" / "6月批次" / "6月批次_A1-1.png").is_file()
    assert (d / "样品A" / "6月批次" / "6月批次_照片.png").is_file()   # 没标签的用原文件名


def test_导出图片_每个文件都是能打开的_PNG(client):
    iid = _one(client)
    r = client.post("/api/images/export-images", json={"image_ids": [iid]})
    assert r.status_code == 200, r.text

    d = _out_dir(r.json())
    f = next(d.rglob("*.png"))
    img = cv2.imdecode(np.frombuffer(f.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    assert img is not None and img.shape[0] > 0, f"{f} 不是合法 PNG"


def test_导出图片_没归档的进未分类(client):
    iid = _one(client)
    r = client.post("/api/images/export-images", json={"image_ids": [iid]})
    assert r.status_code == 200, r.text
    assert (_out_dir(r.json()) / "未分类").is_dir()


def test_导出图片_空列表报400(client):
    r = client.post("/api/images/export-images", json={"image_ids": []})
    assert r.status_code == 400, r.text


def test_导出图片_不存在的id跳过其余照常(client):
    iid = _one(client)
    r = client.post("/api/images/export-images", json={"image_ids": [iid, 99999]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["count"] == 1 and j["failed"] == [99999]


def test_导出图片_一张读不出来其余照常导(client, monkeypatch):
    """一张原图在磁盘上被删了/损坏了，**不能拖垮整批**。

    用户勾 30 张、点导出，其中第 12 张的原图被别的程序删了。如果这时整批中止，
    用户拿到的是：硬盘上一个只写了一半的文件夹 + 浏览器里一句英文
    `Internal Server Error` + 文件夹不弹 + **完全不知道是哪张的问题**。
    所以坏的那张记进 `failed`，其余照导，文件夹照样弹。
    """
    from app import db, exporter

    good, bad = _one(client), _one(client)

    conn = db.get_conn(config.DB_PATH)
    row = db.get_image(conn, bad)
    conn.close()
    # 用端点自己那个解析器定位文件，保证删的就是它待会儿要读的那个
    src = exporter._source_path(row["path"])
    assert src.is_file(), src
    src.unlink()

    # 覆盖 fixture 里的挡板，改成一个记录器 —— 顺便把「文件夹还弹不弹」测了
    # （`opened` 字段此前没有任何测试）
    calls = []
    monkeypatch.setattr(os, "startfile", lambda p: calls.append(p), raising=False)

    r = client.post("/api/images/export-images", json={"image_ids": [good, bad]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["count"] == 1, "读不出来的那张不该算进导出张数"
    assert j["failed"] == [bad], "坏掉的那张要如实报出来，不能悄悄吞掉"
    assert calls, "有照片没导出来时文件夹仍然要弹 —— 好的那些图已经在硬盘上了"

    d = _out_dir(j)
    pngs = list(d.rglob("*.png"))
    assert len(pngs) == 1, f"应该只导出好的那一张，实际 {[p.name for p in pngs]}"


def test_导出图片_全都不存在也是400(client):
    r = client.post("/api/images/export-images", json={"image_ids": [99998, 99999]})
    assert r.status_code == 400, r.text


def test_导出图片_不动数据库也不动原图(client):
    """导出是只读操作。改了库或动了原图，这条就红。"""
    from app import db

    iid = _one(client)
    before = client.get(f"/api/image/{iid}").json()
    conn = db.get_conn(config.DB_PATH)
    n_before = conn.execute("SELECT COUNT(*) c FROM images").fetchone()["c"]
    conn.close()

    r = client.post("/api/images/export-images", json={"image_ids": [iid]})
    assert r.status_code == 200, r.text

    assert client.get(f"/api/image/{iid}").json() == before
    conn = db.get_conn(config.DB_PATH)
    assert conn.execute("SELECT COUNT(*) c FROM images").fetchone()["c"] == n_before
    conn.close()


def test_导出图片_库里的坏文件夹名写不到外面(client, tmp_path):
    """最要紧的一条：库里的文件夹叫 `..\\..\\Windows` 时，文件绝不能跑到导出目录外面。

    ⚠️ **不能走 `POST /api/folders` 来造这个名字** —— `\\` 和 `/` 都在
    `storage.BAD_NAME_CHARS` 里（`'\\/:*?"<>|'`），那个端点直接 400，测试会死在 KeyError 上。
    这里**直接写库**，模拟「老版本建出来的、或别的代码路径写进去的」坏名字 ——
    `_safe_segment` 存在的意义正是它：库里已经有坏数据时，导出这一步不能被打穿。
    （实测 `_safe_segment("..\\..\\Windows")` 给出 `"Windows"`，所以坏名字是被修好，不是被拒绝。）
    """
    from app import db

    iid = _one(client)
    conn = db.get_conn(config.DB_PATH)
    fid = db.create_folder(conn, "..\\..\\Windows", None)     # 绕开 API 校验，只走库
    db.set_image_folders(conn, iid, [fid])
    conn.close()

    r = client.post("/api/images/export-images", json={"image_ids": [iid]})
    assert r.status_code == 200, r.text
    d = _out_dir(r.json())

    for f in d.rglob("*.png"):
        assert str(f.resolve()).startswith(str(d.resolve())), f"写到外面去了：{f}"
    # 上一级（tmp_path）底下除了既有那几样，不该多出别的东西
    assert {p.name for p in tmp_path.iterdir()} <= {"导出", "data"}


def test_导出图片_拼出来的路径越界就中止(client, tmp_path, monkeypatch):
    """单独钉第六道闸（`commonpath`）。

    `_safe_segment` 是「尽量修好」，闸门是「修不好就别写」。上面那条测试走的是**真实输入**，
    恶意文件夹名在半路就被 `_safe_segment` 修好了 —— 也就是说**闸门那两行从来没被执行过**，
    把它删掉上面那条测试照样全绿。这条直接把 `export_plan` 换掉，模拟「消毒函数哪天出了 bug、
    真放了个 `..` 出来」，确认闸门拦得住。

    （Task 8 的实施者做过变异测试：规则 3 单独拿掉时 21 条测试全绿，因为规则 5 把它兜住了。
     同一个盲区在这里重演了一遍 —— 所以要一条直捣闸门的测试。）
    """
    from app import exporter

    iid = _one(client)
    monkeypatch.setattr(exporter, "export_plan", lambda *a, **k: [
        {"image_id": iid, "parts": ["..", "..", "越界"], "name": "x.png", "renamed": False},
    ])

    r = client.post("/api/images/export-images", json={"image_ids": [iid]})

    assert r.status_code == 500, r.text
    assert "不安全" in r.json()["detail"]
    # out_dir 是 <tmp>/导出/<时间戳>，往上两级正好是 <tmp>
    assert not (tmp_path / "越界").exists(), "闸门没拦住，文件写到导出目录外面去了"
