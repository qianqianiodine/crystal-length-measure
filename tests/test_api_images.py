"""图像导入与管理 API 测试。

⚠️ 必须 monkeypatch config 里的目录，否则会往用户真实的 data/ 里写东西。
"""
from io import BytesIO

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, db, main, storage, thumbs


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


def _png(w=80, h=60, val=200) -> bytes:
    ok, buf = cv2.imencode(".png", np.full((h, w, 3), val, np.uint8))
    assert ok
    return buf.tobytes()


def _upload(client, name="照片.png", data=None):
    return client.post("/api/images/upload",
                       files=[("files", (name, data if data is not None else _png(), "image/png"))])


# ---------------- 上传 ----------------

def test_upload_single(client):
    j = _upload(client).json()
    assert len(j["ok"]) == 1
    assert j["failed"] == []
    assert j["ok"][0]["name"] == "照片"
    assert j["ok"][0]["import_source"] == "upload"


def test_upload_multiple(client):
    # mode=direct 显式钉死"直接入库"这条路 —— 默认的 auto 在 ≥2 张时会走待确认列表
    r = client.post("/api/images/upload?mode=direct", files=[
        ("files", ("甲.png", _png(val=10), "image/png")),
        ("files", ("乙.png", _png(val=20), "image/png")),
    ])
    j = r.json()
    assert [i["name"] for i in j["ok"]] == ["甲", "乙"]


def test_upload_rejects_garbage(client):
    j = _upload(client, "坏的.png", b"this is not an image").json()
    assert j["ok"] == []
    assert len(j["failed"]) == 1
    assert j["failed"][0]["name"] == "坏的.png"
    assert "识别" in j["failed"][0]["reason"]


def test_upload_rejects_empty(client):
    """空文件是 cv2.imdecode 会抛异常、而不是返回 None 的那种输入。"""
    j = _upload(client, "空的.png", b"").json()
    assert j["ok"] == []
    assert "空" in j["failed"][0]["reason"]


def test_upload_rejects_heic(client):
    """iPhone 的 HEIC 要给一句能照做的提示，不能只说"不是图片"。"""
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32
    j = _upload(client, "IMG_0001.HEIC", heic).json()
    assert j["ok"] == []
    assert "HEIC" in j["failed"][0]["reason"]


def test_upload_accepts_mpo(client):
    """手机相册里选出来的照片常常是 MPO（多图 JPEG），它是**完全正常的 JPEG**。

    ⚠️ 双摄 / 人像 / 部分 HDR 模式写出来的就是它：扩展名 .jpg、MIME image/jpeg、
    头 12 字节跟普通 JPEG 一模一样，只有往里探一层才知道里面装了不止一张图。
    手机显示"jpeg"没显示错 —— 所以按 Pillow 的 format 一刀切会误伤它。
    """
    buf = BytesIO()
    Image.new("RGB", (80, 60), (200, 30, 30)).save(
        buf, format="MPO", save_all=True,
        append_images=[Image.new("RGB", (80, 60), (30, 30, 200))])
    r = client.post("/api/images/upload",
                    files=[("files", ("IMG_0001.jpg", buf.getvalue(), "image/jpeg"))])
    j = r.json()
    assert j["failed"] == []
    assert j["ok"][0]["name"] == "IMG_0001"


def test_upload_mixed_counts_both(client):
    r = client.post("/api/images/upload?mode=direct", files=[
        ("files", ("好的.png", _png(), "image/png")),
        ("files", ("坏的.png", b"garbage", "image/png")),
    ])
    j = r.json()
    assert len(j["ok"]) == 1 and len(j["failed"]) == 1


def test_upload_strips_bad_chars_from_filename(client):
    """手机/相机给的文件名可能带非法字符，剥掉就行，不该拦住导入。"""
    j = _upload(client, '照:片*名?字.png').json()
    assert len(j["ok"]) == 1
    assert j["ok"][0]["name"] == "照片名字"


def test_upload_duplicate_name_gets_suffix(client):
    _upload(client, "同一张.png")
    j = _upload(client, "同一张.png").json()
    assert j["ok"][0]["name"] == "同一张 (2)"


def test_upload_extension_comes_from_content_not_filename(client):
    """文件名撒谎（说是 jpg、其实是 png）时，存盘扩展名按**内容**走。"""
    j = _upload(client, "其实是png.jpg").json()
    iid = j["ok"][0]["id"]
    with TestClient(main.app) as c:
        row = db.get_image(db.get_conn(config.DB_PATH), iid)
    assert row["path"].endswith(".png")


# ---------------- 粘贴 ----------------

def test_paste(client):
    import base64
    url = "data:image/png;base64," + base64.b64encode(_png()).decode()
    j = client.post("/api/images/paste", json={"data_url": url}).json()
    assert len(j["ok"]) == 1
    assert j["ok"][0]["import_source"] == "paste"


def test_paste_rejects_non_image(client):
    r = client.post("/api/images/paste", json={"data_url": "data:text/plain,hello"})
    assert r.status_code == 400
    assert "剪贴板" in r.json()["detail"]


# ---------------- 列表 / 改名 ----------------

def test_list_images(client):
    _upload(client, "甲.png")
    _upload(client, "乙.png")
    j = client.get("/api/images").json()
    assert j["total"] == 2
    assert {i["name"] for i in j["items"]} == {"甲", "乙"}


def test_rename(client):
    iid = _upload(client, "旧名.png").json()["ok"][0]["id"]
    j = client.patch(f"/api/images/{iid}", json={"name": "实验A_20260920"}).json()
    assert j["name"] == "实验A_20260920"


def test_rename_rejects_bad_chars(client):
    iid = _upload(client, "甲.png").json()["ok"][0]["id"]
    r = client.patch(f"/api/images/{iid}", json={"name": "a/b"})
    assert r.status_code == 400


def test_rename_duplicate_gets_suffix(client):
    _upload(client, "甲.png")
    iid = _upload(client, "乙.png").json()["ok"][0]["id"]
    assert client.patch(f"/api/images/{iid}", json={"name": "甲"}).json()["name"] == "甲 (2)"


# ---------------- 删除 ----------------

def test_delete_moves_file_to_trash(client):
    iid = _upload(client, "要删的.png").json()["ok"][0]["id"]
    conn = db.get_conn(config.DB_PATH)
    stored = storage.resolve_path(db.get_image(conn, iid)["path"])
    conn.close()

    assert client.delete(f"/api/images/{iid}").json() == {
        "deleted": True, "file_moved_to_trash": True}
    assert not stored.exists()
    assert (config.DATA_DIR / "trash" / stored.name).is_file()
    assert client.get(f"/api/images").json()["total"] == 0


def test_delete_leaves_outside_files_alone(client, tmp_path):
    """老记录的照片留在 样品集/ —— 那是用户的原始资料，删记录可以，删文件不行。"""
    outside = tmp_path / "样品集"
    outside.mkdir()
    src = outside / "老照片.png"
    src.write_bytes(_png())

    conn = db.get_conn(config.DB_PATH)
    iid = db.create_image(conn, "老照片", "老照片.png", str(src), "sample")
    conn.close()

    j = client.delete(f"/api/images/{iid}").json()
    assert j["file_moved_to_trash"] is False
    assert src.is_file(), "样品集/ 里的原图被动了"


# ---------------- 打开 ----------------

def test_imported_image_is_openable(client):
    """回归：库里存相对路径后，_state 和 /file 都必须还能找到文件。"""
    iid = _upload(client, "新导入.png").json()["ok"][0]["id"]
    assert client.get(f"/api/image/{iid}").status_code == 200
    r = client.get(f"/api/image/{iid}/file")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")


def test_open_by_image_id_gets_a_circle(client):
    """回归：按 id 打开也必须自动放起点圆 —— 界面里没有从零画圆的入口。"""
    iid = _upload(client, "新导入.png").json()["ok"][0]["id"]
    d = client.post("/api/open", json={"image_id": iid}).json()
    assert d["image_id"] == iid
    assert d["circle"] is not None
    assert d["um_per_px"] and d["um_per_px"] > 0


def test_open_by_rel_still_works(client, tmp_path, monkeypatch):
    """老的按路径打开不能坏。"""
    root = tmp_path / "样品集"
    root.mkdir()
    (root / "孔.png").write_bytes(_png())
    monkeypatch.setattr(main, "SAMPLES_ROOT", root)

    d = client.post("/api/open", json={"rel": "孔.png"}).json()
    assert d["name"] == "孔"
    assert d["circle"] is not None


def test_open_rejects_empty_body(client):
    assert client.post("/api/open", json={}).status_code == 400


# ---------------- 局域网守卫 ----------------

class _Peer:
    def __init__(self, host):
        self.host = host


class _Req:
    def __init__(self, host):
        self.client = _Peer(host) if host is not None else None


@pytest.mark.parametrize("host,local", [
    ("127.0.0.1", True),
    ("127.0.0.2", True),      # 整个 127/8 都是环回
    ("::1", True),
    ("testclient", True),     # TestClient 报的就是这个串
    ("10.0.0.5", False),
    ("192.168.1.7", False),
    ("", False),
    (None, False),            # 判不出来一律当外部
])
def test_is_local(host, local):
    assert main._is_local(_Req(host)) is local


def test_mobile_page_is_served(client):
    token = main.app.state.mobile_token
    r = client.get(f"/m/{token}")
    assert r.status_code == 200
    assert "传照片到电脑" in r.text


def test_net_info_reports_off_by_default(client):
    j = client.get("/api/net/info").json()
    assert j["on"] is False
    assert j["expires_in"] == 0


def test_net_enable_opens_window(client):
    j = client.post("/api/net/enable").json()
    assert j["on"] is True
    assert j["expires_in"] > 0


def test_net_qrcode_is_png(client):
    r = client.get("/api/net/qrcode")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------- 缩略图（画廊格子墙）----------------

def _decode(data: bytes):
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def test_thumb_is_generated_then_cached(client):
    iid = _upload(client, "照片.png", _png(640, 480)).json()["ok"][0]["id"]

    r = client.get(f"/api/images/{iid}/thumb")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert max(_decode(r.content).shape[:2]) == 320      # 长边压到 320

    made = thumbs.path_for(iid)
    assert made.is_file()

    # 第二次直接用缓存，不重做
    assert client.get(f"/api/images/{iid}/thumb").content == r.content
    # 临时文件不能留在目录里 —— 它是写一半的中间态
    assert not list(config.THUMBS_DIR.glob("*.new.jpg"))


def test_thumb_does_not_enlarge_small_images(client):
    """本来就比 320 小的图不放大 —— 放大只会糊，还白占磁盘。"""
    iid = _upload(client, "小的.png", _png(100, 80)).json()["ok"][0]["id"]
    r = client.get(f"/api/images/{iid}/thumb")
    assert _decode(r.content).shape[:2] == (80, 100)


def test_thumb_404_for_missing_image(client):
    assert client.get("/api/images/9999/thumb").status_code == 404


def test_thumb_404_when_original_is_gone(client):
    """原图被挪走了要给 404 而不是 500 —— 画廊里一张坏了不该让整页打不开。"""
    iid = _upload(client, "会丢的.png").json()["ok"][0]["id"]
    conn = db.get_conn(config.DB_PATH)
    stored = storage.resolve_path(db.get_image(conn, iid)["path"])
    conn.close()
    stored.unlink()

    r = client.get(f"/api/images/{iid}/thumb")
    assert r.status_code == 404
    assert "读不出来" in r.json()["detail"]


def test_thumb_of_old_sample_record(client, tmp_path):
    """老记录的照片留在 样品集/（绝对路径）—— 缩略图照样要做出来，而且不许动原图。"""
    outside = tmp_path / "样品集"
    outside.mkdir()
    src = outside / "老照片.png"
    src.write_bytes(_png(400, 300))
    conn = db.get_conn(config.DB_PATH)
    iid = db.create_image(conn, "老照片", "老照片.png", str(src), "sample")
    conn.close()

    assert client.get(f"/api/images/{iid}/thumb").status_code == 200
    assert src.is_file()


def test_gallery_page_is_served(client):
    r = client.get("/gallery")
    assert r.status_code == 200
    assert "图库" in r.text


# ---------------- 画廊的排序 / 筛选 / 搜索 ----------------

def _measure_one(client, name: str) -> int:
    """上传一张并真的量一条线 —— 「已测量」是按有没有测量记录算的。"""
    iid = _upload(client, name).json()["ok"][0]["id"]
    client.post("/api/open", json={"image_id": iid})          # 自动定圆 = 标定好了
    r = client.post(f"/api/image/{iid}/lines",
                    json={"x1": 10, "y1": 10, "x2": 50, "y2": 10, "note": ""})
    assert r.status_code == 200, r.text
    return iid


def test_list_filters_by_measured_status(client):
    _measure_one(client, "量过的.png")
    _upload(client, "没量的.png")

    assert client.get("/api/images").json()["total"] == 2

    j = client.get("/api/images", params={"status": "measured"}).json()
    assert j["total"] == 1 and j["items"][0]["name"] == "量过的"

    j = client.get("/api/images", params={"status": "unmeasured"}).json()
    assert j["total"] == 1 and j["items"][0]["name"] == "没量的"


def test_list_searches_by_name(client):
    _upload(client, "EXP0615_A1.png")
    _upload(client, "别的.png")
    j = client.get("/api/images", params={"q": "EXP0615"}).json()
    assert j["total"] == 1 and j["items"][0]["name"] == "EXP0615_A1"


def test_list_search_treats_wildcards_as_plain_text(client):
    """% 和 _ 是 LIKE 的通配符。用户真打出它们时要当普通字符搜，否则搜 % 会搜出全部。"""
    _upload(client, "甲.png")
    _upload(client, "乙.png")
    assert client.get("/api/images", params={"q": "%"}).json()["total"] == 0
    assert client.get("/api/images", params={"q": "_"}).json()["total"] == 0


def test_list_sorts_by_name(client):
    for n in ("丙.png", "甲.png", "乙.png"):
        _upload(client, n)
    j = client.get("/api/images", params={"order": "name_asc"}).json()
    # SQLite 默认按字节序：丙(U+4E19) < 乙(U+4E59) < 甲(U+7532)
    assert [i["name"] for i in j["items"]] == ["丙", "乙", "甲"]


def test_list_bad_order_falls_back_instead_of_failing(client):
    """order 是用户能改的网址参数 —— 认不出来就退回默认，绝不能拼进 SQL 或报错。"""
    _upload(client, "甲.png")
    r = client.get("/api/images", params={"order": "'; DROP TABLE images--"})
    assert r.status_code == 200
    assert r.json()["total"] == 1
    assert client.get("/api/images").json()["total"] == 1      # 表还在


def test_list_total_is_filtered_not_page_size(client):
    for i in range(5):
        _upload(client, f"图{i}.png")
    j = client.get("/api/images", params={"limit": 2, "offset": 0}).json()
    assert j["total"] == 5          # total 是筛选后的总数，翻页要用它算页数
    assert len(j["items"]) == 2
    j2 = client.get("/api/images", params={"limit": 2, "offset": 4}).json()
    assert len(j2["items"]) == 1    # 最后一页只剩一张


# ---------------- 画质调整（亮度/对比度/饱和度）----------------

def _stored_enhance(iid: int):
    conn = db.get_conn(config.DB_PATH)
    v = db.get_image(conn, iid)["enhance"]
    conn.close()
    return v


def test_enhance_defaults_to_neutral(client):
    """没调过的图，参数一律是中性值 —— 界面直接照这个画。"""
    iid = _upload(client, "照片.png").json()["ok"][0]["id"]
    assert client.get(f"/api/image/{iid}").json()["enhance"] == {"b": 1.0, "c": 1.0, "s": 1.0}


def test_enhance_round_trip(client):
    iid = _upload(client, "照片.png").json()["ok"][0]["id"]
    r = client.put(f"/api/image/{iid}/enhance", json={"b": 1.4, "c": 1.8, "s": 0.6})
    assert r.status_code == 200
    assert client.get(f"/api/image/{iid}").json()["enhance"] == {"b": 1.4, "c": 1.8, "s": 0.6}


def test_enhance_neutral_is_stored_as_null(client):
    """调回原样就存 NULL —— 库里不该留下"用户调过"的假象。"""
    iid = _upload(client, "照片.png").json()["ok"][0]["id"]
    client.put(f"/api/image/{iid}/enhance", json={"b": 1.5, "c": 1.0, "s": 1.0})
    assert _stored_enhance(iid) is not None

    client.put(f"/api/image/{iid}/enhance", json={"b": 1.0, "c": 1.0, "s": 1.0})
    assert _stored_enhance(iid) is None


def test_enhance_clamps_out_of_range(client):
    """滑块拖不出范围，但网址是可以手改的 —— 越界值必须夹回去，不能原样落库。"""
    iid = _upload(client, "照片.png").json()["ok"][0]["id"]
    j = client.put(f"/api/image/{iid}/enhance", json={"b": 99, "c": -5, "s": 0}).json()
    assert j["enhance"] == {"b": 2.0, "c": 0.5, "s": 0.0}


def test_enhance_rejects_non_finite(client):
    """NaN 会一路传进库里、再传进 canvas 滤镜，把整串滤镜废掉。要在门口挡住。

    浏览器发不出这个值（JSON.stringify(NaN) 得到的是 null），但 Python 的
    json.loads 默认认 `NaN` 这个词 —— 所以用 json= 传会被客户端挡下，
    得手工构造原始报文才试得出来。
    """
    iid = _upload(client, "照片.png").json()["ok"][0]["id"]
    r = client.put(f"/api/image/{iid}/enhance",
                   content='{"b": NaN}',
                   headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert _stored_enhance(iid) is None          # 坏值一个字节都没落库


def test_enhance_404_for_missing_image(client):
    assert client.put("/api/image/9999/enhance", json={}).status_code == 404


def test_enhance_survives_corrupt_stored_json(client):
    """存档坏了不该让这张图打不开 —— 退回中性值就行。"""
    iid = _upload(client, "照片.png").json()["ok"][0]["id"]
    conn = db.get_conn(config.DB_PATH)
    conn.execute("UPDATE images SET enhance=? WHERE id=?", ("{不是 json", iid))
    conn.commit()
    conn.close()
    assert client.get(f"/api/image/{iid}").json()["enhance"] == {"b": 1.0, "c": 1.0, "s": 1.0}


def test_enhance_never_changes_measurements(client):
    """safe 底线：画质调整只决定"画出来什么样"，测量值和比例尺一个都不许动。"""
    iid = _measure_one(client, "量过的.png")
    before = client.get(f"/api/image/{iid}").json()
    assert before["lines"]                      # 先确认真的量过一条

    client.put(f"/api/image/{iid}/enhance", json={"b": 1.6, "c": 1.8, "s": 0.4})

    after = client.get(f"/api/image/{iid}").json()
    assert after["lines"] == before["lines"]
    assert after["um_per_px"] == before["um_per_px"]
    assert after["circle"] == before["circle"]


# ---------------- 缩略图跟着裁剪 / 画质调整走 ----------------

def _one(client):
    """上传一张 80×60 的灰图，返回它的 id。"""
    return _upload(client).json()["ok"][0]["id"]


def _thumb_src(client):
    """上传一张图，返回（id, 原图路径, 缩略图路径）。"""
    from app import config, db, storage, thumbs

    iid = _one(client)
    conn = db.get_conn(config.DB_PATH)
    row = db.get_image(conn, iid)
    conn.close()
    return iid, storage.resolve_path(row["path"]), thumbs.path_for(iid)


def test_缩略图按裁剪框渲染(client):
    from app import imageio, thumbs

    iid, src, dst = _thumb_src(client)
    full = thumbs.ensure_at(src, dst)
    assert imageio.load_rgb(full).shape[:2] == (60, 80)

    dst.unlink()
    cut = thumbs.ensure_at(src, dst, crop_raw='{"x0": 10, "y0": 5, "x1": 50, "y1": 35}')
    assert imageio.load_rgb(cut).shape[:2] == (30, 40)      # 40 宽 × 30 高


def test_缩略图越界裁剪被夹住不炸(client):
    from app import imageio, thumbs

    iid, src, dst = _thumb_src(client)
    p = thumbs.ensure_at(src, dst, crop_raw='{"x0": -99, "y0": -99, "x1": 9999, "y1": 9999}')
    assert p is not None
    assert imageio.load_rgb(p).shape[:2] == (60, 80)


def test_缩略图裁剪存档坏了就按原图来(client):
    from app import imageio, thumbs

    iid, src, dst = _thumb_src(client)
    p = thumbs.ensure_at(src, dst, crop_raw="{ 不是 JSON")
    assert imageio.load_rgb(p).shape[:2] == (60, 80)


def test_缩略图带上画质调整(client):
    """照片被调暗之后，图库卡片也得跟着暗 —— 和裁剪是同一类"看起来什么样"。"""
    from app import imageio, thumbs

    iid, src, dst = _thumb_src(client)
    base = thumbs.ensure_at(src, dst)
    assert imageio.load_rgb(base).mean() > 150

    dst.unlink()
    dark = thumbs.ensure_at(src, dst, enhance_raw='{"b": 0.5, "c": 1.0, "s": 1.0}')
    # b 是伽马曲线（LUT[i] = 255*(i/255)**(1/b)），不是直接乘：200 灰在 b=0.5
    # —— ENHANCE_RANGE 允许的最暗值 —— 下变成 157（实测 b=0.6→170、0.8→188）。
    # 所以阈值取 170：既证明确实变暗了，又不依赖"b 是乘法"这个错的前提。
    assert imageio.load_rgb(dark).mean() < 170


def test_缩略图画质中性值等于不调(client):
    from app import imageio, thumbs

    iid, src, dst = _thumb_src(client)
    a = thumbs.ensure_at(src, dst)
    first = imageio.load_rgb(a)

    dst.unlink()
    b = thumbs.ensure_at(src, dst, enhance_raw='{"b": 1.0, "c": 1.0, "s": 1.0}')
    assert (imageio.load_rgb(b) == first).all()


def test_drop_删掉缓存(client):
    from app import thumbs

    iid, src, dst = _thumb_src(client)
    p = thumbs.ensure(src, iid)
    assert p.is_file()
    thumbs.drop(iid)
    assert not p.exists()
    thumbs.drop(iid)                 # 再叫一次不该炸（幂等）


def test_调完亮度缩略图立刻换新(client):
    from app import thumbs

    iid, src, dst = _thumb_src(client)
    first = client.get(f"/api/images/{iid}/thumb").content
    assert dst.is_file()

    r = client.put(f"/api/image/{iid}/enhance", json={"b": 0.5, "c": 1.0, "s": 1.0})
    assert r.status_code == 200
    assert not dst.exists(), "调完亮度旧缩略图必须删掉"

    assert client.get(f"/api/images/{iid}/thumb").content != first


def test_缩略图不再长缓存(client):
    iid, src, dst = _thumb_src(client)
    r = client.get(f"/api/images/{iid}/thumb")
    assert r.headers["cache-control"] == "no-cache"
