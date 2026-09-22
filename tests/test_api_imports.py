"""待确认导入列表 API 测试。

⚠️ 必须 monkeypatch config 里的目录，尤其是 STAGING_DIR ——
漏一个就会往用户真实的 data/ 里写东西。
"""
import threading
import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

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


def _upload(client, name="照片.png", data=None, mode=None):
    q = f"?mode={mode}" if mode else ""
    return client.post("/api/images/upload" + q,
                       files=[("files", (name, data if data is not None else _png(), "image/png"))])


def _upload_many(client, names, mode=None):
    q = f"?mode={mode}" if mode else ""
    return client.post("/api/images/upload" + q,
                       files=[("files", (n, _png(val=10 + i), "image/png"))
                              for i, n in enumerate(names)])


def _pending_rows():
    """直接读库 —— 列表接口是 Task 4 才有的。"""
    conn = db.get_conn(config.DB_PATH)
    try:
        return db.list_pending(conn)
    finally:
        conn.close()


def _library_names(client) -> list[str]:
    return [i["name"] for i in client.get("/api/images").json()["items"]]


def _measure_one(client, name: str) -> int:
    """上传一张并真的量一条线 —— 「已测量」是按有没有测量记录算的。"""
    iid = _upload(client, name).json()["ok"][0]["id"]
    client.post("/api/open", json={"image_id": iid})
    r = client.post(f"/api/image/{iid}/lines",
                    json={"x1": 10, "y1": 10, "x2": 50, "y2": 10, "note": ""})
    assert r.status_code == 200, r.text
    return iid


# ---------------- 上传的 mode ----------------

def test_auto_single_goes_straight_in(client):
    j = _upload(client, "一张.png").json()
    assert [i["name"] for i in j["ok"]] == ["一张"]
    assert j["staged"] == []
    assert _pending_rows() == []


def test_auto_multiple_goes_to_pending(client):
    j = _upload_many(client, ["甲.png", "乙.png", "丙.png"]).json()
    assert j["ok"] == []
    assert [s["name"] for s in j["staged"]] == ["甲", "乙", "丙"]
    assert _library_names(client) == []                 # 图库里一张都没多
    assert [r["name"] for r in _pending_rows()] == ["甲", "乙", "丙"]


def test_pending_files_land_in_staging_not_the_library(client):
    _upload_many(client, ["甲.png", "乙.png"])
    assert len(list(config.STAGING_DIR.glob("*.png"))) == 2
    assert list(config.IMAGES_DIR.glob("*")) == []


def test_direct_multiple_ignores_the_count(client):
    j = _upload_many(client, ["甲.png", "乙.png"], mode="direct").json()
    assert [i["name"] for i in j["ok"]] == ["甲", "乙"]
    assert j["staged"] == []
    assert _pending_rows() == []


def test_pending_single(client):
    j = _upload(client, "一张.png", mode="pending").json()
    assert j["ok"] == []
    assert len(j["staged"]) == 1
    assert _library_names(client) == []


def test_bad_file_gets_a_red_row_in_pending(client):
    """需求 §3.1.2：失败的文件要在列表里用红字标出文件名和原因。"""
    r = client.post("/api/images/upload?mode=pending",
                    files=[("files", ("坏的.png", b"not an image", "image/png"))])
    j = r.json()
    assert j["failed"] == []                            # 不是丢进 failed，是占一行
    assert j["staged"][0]["status"] == "failed"
    assert "识别" in j["staged"][0]["reason"]
    assert _pending_rows()[0]["staged_path"] is None


def test_bad_file_in_direct_mode_still_uses_the_failed_array(client):
    """回归：直接入库那条路的契约没变（测量页的下拉框和状态栏还靠它）。"""
    j = _upload(client, "坏的.png", b"garbage", mode="direct").json()
    assert j["ok"] == [] and len(j["failed"]) == 1
    assert j["staged"] == []


def test_upload_rejects_a_mode_it_does_not_know(client):
    """拼错的 mode 要当场报错，不能被当成 direct 悄悄收下 ——
    用户以为进了待确认列表，其实照片已经直接入库了。"""
    assert _upload(client, "甲.png", mode="pendingX").status_code == 422
    assert _library_names(client) == []


# ---------------- 列表接口 ----------------

def _pending(client) -> dict:
    r = client.get("/api/imports")
    assert r.status_code == 200, r.text
    return r.json()


def test_list_starts_empty(client):
    assert _pending(client) == {"items": [], "prefix": "", "total_size": 0,
                                "ok_count": 0, "failed_count": 0}


def test_list_size_counts_only_staged_files(client):
    _upload_many(client, ["甲.png", "乙.png"])
    d = _pending(client)
    assert d["ok_count"] == 2 and d["failed_count"] == 0
    assert d["total_size"] == sum(i["size"] for i in d["items"]) > 0


def test_failed_row_does_not_count_toward_size(client):
    client.post("/api/images/upload?mode=pending",
                files=[("files", ("好的.png", _png(), "image/png")),
                       ("files", ("坏的.png", b"garbage", "image/png"))])
    d = _pending(client)
    assert d["ok_count"] == 1 and d["failed_count"] == 1
    assert d["total_size"] == d["items"][0]["size"]


def test_thumb(client):
    _upload(client, "甲.png", _png(640, 480), mode="pending")
    pid = _pending(client)["items"][0]["id"]

    r = client.get(f"/api/imports/{pid}/thumb")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert thumbs.path_for_staged(pid).is_file()
    assert list(config.THUMBS_DIR.glob("*")) == []      # 不许混进正式图的缩略图目录


def test_thumb_404_for_failed_row(client):
    client.post("/api/images/upload?mode=pending",
                files=[("files", ("坏的.png", b"garbage", "image/png"))])
    pid = _pending(client)["items"][0]["id"]
    assert client.get(f"/api/imports/{pid}/thumb").status_code == 404


def test_thumb_404_for_missing_row(client):
    assert client.get("/api/imports/9999/thumb").status_code == 404


def test_rename_a_pending_row(client):
    _upload(client, "旧名.png", mode="pending")
    pid = _pending(client)["items"][0]["id"]
    assert client.patch(f"/api/imports/{pid}", json={"name": "EXP0615_A1"}).json()["name"] == "EXP0615_A1"
    assert _pending(client)["items"][0]["name"] == "EXP0615_A1"


def test_rename_to_empty_is_allowed(client):
    """清空重打的中间态就是空的，那时候弹 400 只会让人困惑。
    空 = 到确认时回退到原始文件名。"""
    _upload(client, "甲.png", mode="pending")
    pid = _pending(client)["items"][0]["id"]
    assert client.patch(f"/api/imports/{pid}", json={"name": ""}).status_code == 200
    assert _pending(client)["items"][0]["name"] == ""


def test_rename_rejects_bad_chars(client):
    _upload(client, "甲.png", mode="pending")
    pid = _pending(client)["items"][0]["id"]
    assert client.patch(f"/api/imports/{pid}", json={"name": "a/b"}).status_code == 400


def test_rename_missing_row_404(client):
    assert client.patch("/api/imports/9999", json={"name": "x"}).status_code == 404


def test_conflict_carries_the_line_count_and_a_suggestion(client):
    iid = _measure_one(client, "IMG_0001.png")
    _upload(client, "IMG_0001.png", mode="pending")
    assert _pending(client)["items"][0]["conflict"] == {
        "image_id": iid, "name": "IMG_0001", "line_count": 1,
        "suggest": "IMG_0001 (2)"}


def test_no_conflict_when_the_name_is_free(client):
    _upload(client, "甲.png", mode="pending")
    assert _pending(client)["items"][0]["conflict"] is None


def test_empty_name_never_reports_a_conflict(client):
    _measure_one(client, "甲.png")
    _upload(client, "甲.png", mode="pending")
    pid = _pending(client)["items"][0]["id"]
    client.patch(f"/api/imports/{pid}", json={"name": ""})
    assert _pending(client)["items"][0]["conflict"] is None


def test_failed_row_never_reports_a_conflict(client):
    _measure_one(client, "IMG_0001.png")
    client.post("/api/images/upload?mode=pending",
                files=[("files", ("IMG_0001.png", b"garbage", "image/png"))])
    assert _pending(client)["items"][0]["conflict"] is None


def test_prefix_is_applied_to_every_row_when_checking_conflicts(client):
    _upload(client, "A1.png", mode="pending")
    assert _pending(client)["items"][0]["conflict"] is None

    _upload(client, "IMG_A1.png")                     # 库里先放一张 IMG_A1
    d = client.put("/api/imports/prefix", json={"prefix": "IMG_"}).json()
    assert d["prefix"] == "IMG_"
    assert d["items"][0]["name"] == "A1"              # 行自己一个字没变
    assert d["items"][0]["conflict"]["name"] == "IMG_A1"


def test_prefix_clears_with_an_empty_string(client):
    client.put("/api/imports/prefix", json={"prefix": "IMG_"})
    assert client.put("/api/imports/prefix", json={"prefix": ""}).json()["prefix"] == ""


def test_prefix_rejects_bad_chars(client):
    assert client.put("/api/imports/prefix", json={"prefix": "a/b"}).status_code == 400


def test_prefix_rejects_too_long(client):
    assert client.put("/api/imports/prefix", json={"prefix": "x" * 61}).status_code == 400


def test_remove_one_row_deletes_only_its_file(client):
    _upload_many(client, ["甲.png", "乙.png"])
    pid = _pending(client)["items"][0]["id"]
    assert len(list(config.STAGING_DIR.glob("*.png"))) == 2

    j = client.delete(f"/api/imports/{pid}").json()
    assert [i["name"] for i in j["items"]] == ["乙"]
    assert len(list(config.STAGING_DIR.glob("*.png"))) == 1


def test_remove_missing_row_404(client):
    assert client.delete("/api/imports/9999").status_code == 404


def test_cancel_clears_everything(client):
    _upload_many(client, ["甲.png", "乙.png"])
    for it in _pending(client)["items"]:               # 先把小图做出来
        client.get(f"/api/imports/{it['id']}/thumb")
    client.put("/api/imports/prefix", json={"prefix": "X_"})

    assert client.delete("/api/imports").json() == {"cleared": 2}
    assert _pending(client) == {"items": [], "prefix": "", "total_size": 0,
                                "ok_count": 0, "failed_count": 0}
    assert list(config.STAGING_DIR.glob("*.png")) == []
    assert list((config.STAGING_DIR / "thumbs").glob("*.jpg")) == []
    assert _library_names(client) == []


def test_phone_mirror_lists_the_same_thing(client):
    """⭐ 手机必须走 /m/<令牌> —— 守卫只放行那个前缀，直接打 /api/imports 会 403。"""
    token = main.app.state.mobile_token
    _upload_many(client, ["甲.png", "乙.png"])
    r = client.get(f"/m/{token}/imports")
    assert r.status_code == 200
    assert [i["name"] for i in r.json()["items"]] == ["甲", "乙"]


# ---------------- 确认导入 ----------------

def test_confirm_imports_with_the_given_names(client):
    _upload_many(client, ["IMG_0001.png", "IMG_0002.png"])
    client.put("/api/imports/prefix", json={"prefix": "EXP0615_"})
    pid = _pending(client)["items"][0]["id"]
    client.patch(f"/api/imports/{pid}", json={"name": "A1"})

    j = client.post("/api/imports/confirm", json={}).json()
    assert j == {"imported": 2, "skipped": 0, "unnamed": 0, "overwritten": []}
    assert sorted(_library_names(client)) == ["EXP0615_A1", "EXP0615_IMG_0002"]
    assert _pending(client) == {"items": [], "prefix": "", "total_size": 0,
                                "ok_count": 0, "failed_count": 0}
    assert list(config.STAGING_DIR.glob("*.png")) == []


def test_confirmed_photo_really_opens(client):
    """入库的不只是一条记录 —— 文件和缩略图都要真的能用。"""
    _upload(client, "甲.png", mode="pending")
    assert client.post("/api/imports/confirm", json={}).json()["imported"] == 1

    got = client.get("/api/images").json()["items"][0]
    assert got["name"] == "甲"
    assert got["import_source"] == "upload"
    assert client.get(f"/api/image/{got['id']}").status_code == 200
    assert client.get(f"/api/images/{got['id']}/thumb").status_code == 200
    assert client.get(f"/api/image/{got['id']}/file").status_code == 200


def test_unnamed_row_falls_back_to_the_original_filename(client):
    """需求 §3.1.3：没命名的用原始文件名，并告诉用户有几张。"""
    _upload(client, "IMG_0007.png", mode="pending")
    pid = _pending(client)["items"][0]["id"]
    client.patch(f"/api/imports/{pid}", json={"name": ""})

    j = client.post("/api/imports/confirm", json={}).json()
    assert j["unnamed"] == 1
    assert _library_names(client) == ["IMG_0007"]


def test_duplicate_names_get_a_suffix_by_default(client):
    _upload(client, "甲.png")                          # 先入库一张「甲」
    _upload(client, "甲.png", mode="pending")
    client.post("/api/imports/confirm", json={})
    assert sorted(_library_names(client)) == ["甲", "甲 (2)"]


def test_confirmed_rows_go_to_the_front_of_the_gallery(client):
    """需求 §3.1.3：导入完成后新图排在画廊最前面。

    这三张的 import_time 是同一秒，所以实际由 `id DESC` 决出名次 ——
    只断言"新导入的占前两位、老照片掉到第三"。
    """
    _upload(client, "老照片.png")
    _upload_many(client, ["甲.png", "乙.png"])
    client.post("/api/imports/confirm", json={})

    names = _library_names(client)
    assert set(names[:2]) == {"甲", "乙"}
    assert names[2] == "老照片"


def test_failed_rows_are_skipped_and_cleared(client):
    client.post("/api/images/upload?mode=pending",
                files=[("files", ("好的.png", _png(), "image/png")),
                       ("files", ("坏的.png", b"garbage", "image/png"))])
    j = client.post("/api/imports/confirm", json={}).json()
    assert j["imported"] == 1 and j["skipped"] == 1
    assert _pending(client)["items"] == []
    assert _library_names(client) == ["好的"]


def test_overwrite_replaces_the_photo_and_its_measurements(client):
    """⭐ 覆盖是唯一会永久毁数据的路径：旧照片的标定和测量记录一起没。

    原图会挪进 data/trash/，但测量记录不进 —— 不可恢复。
    """
    iid = _measure_one(client, "IMG_0001.png")
    assert client.get(f"/api/image/{iid}").json()["lines"]

    _upload(client, "IMG_0001.png", _png(val=90), mode="pending")
    pid = _pending(client)["items"][0]["id"]
    j = client.post("/api/imports/confirm", json={"overwrite": [pid]}).json()

    assert j["overwritten"] == ["IMG_0001"]
    items = client.get("/api/images").json()["items"]
    assert [i["name"] for i in items] == ["IMG_0001"]
    new_id = items[0]["id"]
    assert new_id != iid                                     # 换了一张新的
    assert client.get(f"/api/image/{new_id}").json()["lines"] == []   # 新的这张干干净净
    assert len(list((config.DATA_DIR / "trash").glob("*"))) == 1      # 原图进了回收站

    # 直查库：老 id 底下的测量记录是**真删了**，不是留下一堆孤儿行
    conn = db.get_conn(config.DB_PATH)
    try:
        left = conn.execute("SELECT COUNT(*) c FROM measurements WHERE image_id=?",
                            (iid,)).fetchone()["c"]
    finally:
        conn.close()
    assert left == 0


def test_a_failed_store_never_touches_the_old_photo(client, monkeypatch):
    """⭐ 覆盖的**顺序**：先落盘、再删旧的。

    新文件存不下来（磁盘满 / 字节坏了 / 图坏了）时，旧照片和它量过的线必须
    完好无损 —— 反过来的话旧的已经删了、新的又没落盘，两边都没。
    """
    iid = _measure_one(client, "IMG_0001.png")
    _upload(client, "IMG_0001.png", _png(val=90), mode="pending")
    pid = _pending(client)["items"][0]["id"]

    def boom(_data):
        raise ValueError("磁盘满了")

    monkeypatch.setattr(storage, "store_original", boom)
    j = client.post("/api/imports/confirm", json={"overwrite": [pid]}).json()

    assert j["imported"] == 0 and j["overwritten"] == []
    r = client.get(f"/api/image/{iid}")                        # 老照片还在吗
    assert r.status_code == 200, "旧照片被删了 —— 顺序反了"
    assert r.json()["lines"]                                   # 它量过的线也在
    assert _library_names(client) == ["IMG_0001"]
    assert list((config.DATA_DIR / "trash").glob("*")) == []   # 旧图没被挪走
    row = _pending(client)["items"][0]                         # 还留着，带红字
    assert row["status"] == "failed" and "磁盘满了" in row["reason"]


def test_without_the_overwrite_flag_the_old_photo_survives(client):
    """没有显式点「覆盖」时，同名的老照片一根汗毛都不许动。"""
    iid = _measure_one(client, "IMG_0001.png")
    _upload(client, "IMG_0001.png", _png(val=90), mode="pending")
    client.post("/api/imports/confirm", json={})

    assert sorted(_library_names(client)) == ["IMG_0001", "IMG_0001 (2)"]
    assert client.get(f"/api/image/{iid}").json()["lines"]     # 老的那张还量着
    assert list((config.DATA_DIR / "trash").glob("*")) == []


def test_overwrite_of_a_name_that_is_gone_still_imports(client):
    """勾了覆盖，但那张已经被别处删了 —— 照常入库，不能报错。"""
    _upload(client, "甲.png", mode="pending")
    pid = _pending(client)["items"][0]["id"]
    assert client.post("/api/imports/confirm",
                       json={"overwrite": [pid]}).json()["imported"] == 1
    assert _library_names(client) == ["甲"]


def test_confirm_reports_a_row_whose_staged_file_vanished(client):
    _upload(client, "甲.png", mode="pending")
    for p in config.STAGING_DIR.glob("*.png"):
        p.unlink()

    assert client.post("/api/imports/confirm", json={}).json()["imported"] == 0
    row = _pending(client)["items"][0]
    assert row["status"] == "failed"
    assert "找不到" in row["reason"]


def test_confirm_is_not_all_or_nothing(client):
    """确认导入不是原子的：能进的进，进不去的留在列表里带红字，接着点就行。"""
    _upload_many(client, ["甲.png", "乙.png"])
    for p in sorted(config.STAGING_DIR.glob("*.png"))[1:]:
        p.unlink()

    assert client.post("/api/imports/confirm", json={}).json()["imported"] == 1
    left = _pending(client)["items"]
    assert len(left) == 1 and left[0]["status"] == "failed"


def test_confirm_rejects_a_too_long_combined_name_row_by_row(client):
    """前缀 60 字 + 名字 50 字 = 110 > 100。界面里有 maxlength 挡着，服务端仍要兜底。"""
    _upload(client, "甲.png", mode="pending")
    client.put("/api/imports/prefix", json={"prefix": "P" * 60})
    pid = _pending(client)["items"][0]["id"]
    client.patch(f"/api/imports/{pid}", json={"name": "N" * 50})

    assert client.post("/api/imports/confirm", json={}).json()["imported"] == 0
    assert "名字" in _pending(client)["items"][0]["reason"]
    assert _library_names(client) == []


def test_confirm_clears_the_prefix(client):
    _upload_many(client, ["甲.png", "乙.png"])
    client.put("/api/imports/prefix", json={"prefix": "X_"})
    client.post("/api/imports/confirm", json={})
    assert _pending(client)["prefix"] == ""


def test_confirm_on_an_empty_list_is_harmless(client):
    assert client.post("/api/imports/confirm", json={}).json() == {
        "imported": 0, "skipped": 0, "unnamed": 0, "overwritten": []}


def test_phone_can_run_the_whole_flow(client):
    """⭐ 手机必须能独立走完全程 —— 守卫只放行 /m/<令牌> 前缀。"""
    token = main.app.state.mobile_token
    j = client.post(f"/m/{token}/upload?mode=pending",
                    files=[("files", ("手机拍的.png", _png(), "image/png"))]).json()
    assert j["staged"][0]["import_source"] == "mobile"

    d = client.get(f"/m/{token}/imports").json()
    pid = d["items"][0]["id"]
    assert client.patch(f"/m/{token}/imports/{pid}",
                        json={"name": "A1"}).status_code == 200
    assert client.put(f"/m/{token}/imports/prefix",
                      json={"prefix": "EXP_"}).status_code == 200
    assert client.post(f"/m/{token}/imports/confirm", json={}).status_code == 200

    assert _library_names(client) == ["EXP_A1"]


def test_two_same_names_in_one_confirm_do_not_eat_each_other(client):
    """⭐ 同一次 confirm 里两行同名、两行都点覆盖。

    覆盖的「旧照片」是**循环里现查的**，所以第 2 行查到的是第 1 行刚建的那条：
    删它 = 把用户自己刚传的照片挪进 trash、连测量一起删掉（没恢复入口），
    而确认前那句警告从没提过它 —— 用户传了 2 张，库里只剩 1 张。

    手机相机文件名重复（IMG_0001.jpg 是常态），两行都报「和库里那张冲突」，
    两行都点覆盖是自然动作。
    """
    _upload_many(client, ["IMG_0001.png", "IMG_0001.png"])
    rows = _pending(client)["items"]
    assert [r["name"] for r in rows] == ["IMG_0001", "IMG_0001"]

    j = client.post("/api/imports/confirm",
                    json={"overwrite": [rows[0]["id"], rows[1]["id"]]}).json()

    assert j["imported"] == 2
    assert j["overwritten"] == []                # 库里本来就没这张，没什么可覆盖的
    assert sorted(_library_names(client)) == ["IMG_0001", "IMG_0001 (2)"]
    assert list((config.DATA_DIR / "trash").glob("*")) == []    # 谁都没被丢进去


def test_unnamed_is_only_counted_for_rows_that_really_landed(client):
    """⭐ 「未命名」只在**这一行真的进了库**之后才 +1。

    回退到原始文件名后总长仍然超限时，这一行留红字不入库；此时再报 unnamed: 1，
    界面就成了「有 1 张用了原始文件名」但一张都没进 —— 对非程序员用户，
    这句提示必须诚实。
    """
    _upload(client, "N" * 100 + ".png", mode="pending")
    pid = _pending(client)["items"][0]["id"]
    client.patch(f"/api/imports/{pid}", json={"name": ""})     # 清空 = 回退到原始文件名
    client.put("/api/imports/prefix", json={"prefix": "P" * 60})

    j = client.post("/api/imports/confirm", json={}).json()

    assert j["imported"] == 0 and j["unnamed"] == 0
    row = _pending(client)["items"][0]           # 留着红字，写清它为什么没进去
    assert row["status"] == "failed" and "名字" in row["reason"]


def test_a_create_image_failure_does_not_take_down_the_whole_confirm(client, monkeypatch):
    """⭐ 覆盖删旧的和 create_image 之间原本不在任何 try 里。

    sqlite 一抖（锁住 / 写不进去）异常就会冲出整个 confirm：客户端拿到 500、
    剩下的行不再处理、前缀也不清 —— 用户在界面上看到的是"点了没反应"。
    """
    _measure_one(client, "EXP_IMG_0001.png")             # 库里有张量过线的老照片
    _upload_many(client, ["IMG_0001.png", "乙.png"])     # 第一行走覆盖、第二行普通
    rows = _pending(client)["items"]
    assert [r["name"] for r in rows] == ["IMG_0001", "乙"]
    client.put("/api/imports/prefix", json={"prefix": "EXP_"})

    real = db.create_image
    calls = {"n": 0}

    def flaky(*a, **kw):                                 # 只在第一行上炸
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("数据库被锁住了")
        return real(*a, **kw)

    monkeypatch.setattr(db, "create_image", flaky)
    r = client.post("/api/imports/confirm", json={"overwrite": [rows[0]["id"]]})

    assert r.status_code == 200, "异常冲出了 confirm —— 客户端只会看到 500"
    j = r.json()
    assert j["imported"] == 1                            # 后面那行照常处理
    assert j["overwritten"] == ["EXP_IMG_0001"]          # 旧的**真被删了**，就照实说
    assert sorted(_library_names(client)) == ["EXP_乙"]  # 失败的这张没进库
    left = _pending(client)["items"]                     # 失败那行留着带红字
    assert len(left) == 1 and left[0]["status"] == "failed"
    assert "数据库被锁住了" in left[0]["reason"]
    assert _pending(client)["prefix"] == ""              # 前缀照清


def test_a_staged_row_that_failed_on_its_name_survives_a_second_click(client):
    """⭐ 暂存下来了、只是这一次没进库的行，改完名字再点一次就能进库。

    前缀 60 字 + 名字 50 字 = 110 > 100 字上限，第一次确认进不去、留红字 ——
    界面上给用户的路就是"改短名字接着点"。这样的行**有暂存文件**，它不是
    "没传上来"：按没传上来处理的话，第二次点「确认导入」会把它连同暂存文件
    一起清掉，用户改好名字反而发现照片得重新上传。
    """
    prefix, too_long, fixed = "P" * 60, "N" * 50, "A1"
    _upload(client, "甲.png", mode="pending")
    client.put("/api/imports/prefix", json={"prefix": prefix})
    pid = _pending(client)["items"][0]["id"]
    client.patch(f"/api/imports/{pid}", json={"name": too_long})

    assert client.post("/api/imports/confirm", json={}).json() == {
        "imported": 0, "skipped": 0, "unnamed": 0, "overwritten": []}
    row = _pending(client)["items"][0]                    # 还在，还带着红字
    assert row["id"] == pid and row["status"] == "failed"
    assert "名字" in row["reason"]
    assert list(config.STAGING_DIR.glob("*.png")), "暂存文件被清了 —— 照片只能重新上传"

    # 确认会把前缀清掉，所以第二次点之前重新设上：让"名字改短了"是唯一的变化
    client.put("/api/imports/prefix", json={"prefix": prefix})
    client.patch(f"/api/imports/{pid}", json={"name": fixed})

    assert client.post("/api/imports/confirm", json={}).json() == {
        "imported": 1, "skipped": 0, "unnamed": 0, "overwritten": []}
    assert _library_names(client) == [prefix + fixed]
    assert _pending(client)["items"] == []
    assert list(config.STAGING_DIR.glob("*.png")) == []    # 这一次才真清掉


# ---------------- 局域网守卫 / 手机上不许覆盖 ----------------

def test_the_lan_guard_really_blocks_a_remote_caller(client, monkeypatch):
    """⭐ 换一个**远程来源**的客户端再验一遍守卫。

    默认的 TestClient 报的来源是 "testclient"，main.py 的 _is_local() 直接判它是
    本机，令牌校验那条路根本不跑 —— 只拿它测试的话，谁要是把放行前缀改窄
    （比如只放 /upload），手机功能会静默死掉而全套测试依旧全绿。
    """
    token = main.app.state.mobile_token
    with TestClient(main.app, client=("1.2.3.4", 5555)) as remote:
        monkeypatch.setattr(main.app.state, "mobile_on", True)
        monkeypatch.setattr(main.app.state, "mobile_off_at", time.time() + 60)

        assert remote.get("/api/imports").status_code == 403       # 别的路径一律挡
        assert remote.get(f"/m/{token}/imports").status_code == 200  # 只放行这个前缀


def test_the_phone_cannot_overwrite(client):
    """⭐ 覆盖会连旧照片量过的线一起删，手机上一句话讲不清这件事。

    服务端真拦住它 —— 不能只靠手机页不画那个按钮，构造一个请求就绕过去了。
    """
    iid = _measure_one(client, "IMG_0001.png")
    _upload(client, "IMG_0001.png", _png(val=90), mode="pending")
    pid = _pending(client)["items"][0]["id"]
    token = main.app.state.mobile_token

    r = client.post(f"/m/{token}/imports/confirm", json={"overwrite": [pid]})
    assert r.status_code == 400
    assert "手机" in r.json()["detail"]
    # 老照片和它量过的线一根汗毛都没动，那一行也还留着等用户到电脑上处理
    assert client.get(f"/api/image/{iid}").json()["lines"]
    assert _library_names(client) == ["IMG_0001"]
    assert len(_pending(client)["items"]) == 1


# ---------------- 手机上传 ----------------

def test_mobile_upload_pending_mode_stages(client):
    token = main.app.state.mobile_token
    r = client.post(f"/m/{token}/upload?mode=pending",
                    files=[("files", ("手机拍的.png", _png(), "image/png"))])
    j = r.json()
    assert j["ok"] == []
    assert j["staged"][0]["import_source"] == "mobile"
    assert _library_names(client) == []


def test_mobile_upload_defaults_to_direct(client):
    """手机传一张 = 一个请求一张，默认照样直接入库（老行为不变）。"""
    token = main.app.state.mobile_token
    r = client.post(f"/m/{token}/upload",
                    files=[("files", ("手机拍的.png", _png(), "image/png"))])
    assert len(r.json()["ok"]) == 1
    assert r.json()["ok"][0]["import_source"] == "mobile"


def test_mobile_upload_keeps_the_size_limit(client):
    token = main.app.state.mobile_token
    r = client.post(f"/m/{token}/upload",
                    files=[("files", (f"第{i}.png", _png(), "image/png"))
                           for i in range(51)])
    assert r.status_code == 400


def test_phone_page_has_the_pending_ui(client):
    """手机页面上要有待确认列表那块 —— 用户要能在手机上改名和确认。"""
    token = main.app.state.mobile_token
    html = client.get(f"/m/{token}").text
    assert 'id="pend"' in html
    assert "/imports" in html


# ---------------- 改名要把红字清掉 / 确认导入不能同时跑两遍 ----------------

def test_renaming_a_failed_row_clears_its_red_text(client):
    """名字改对了，上一次的红字就该消失。

    红字（"名字不能超过 100 个字"）挂在那里会让人以为还是坏的，
    而实际上这一行改完就能进库了 —— 屏幕上那句话跟现状是反的。
    """
    prefix, too_long = "P" * 60, "N" * 50
    _upload(client, "甲.png", mode="pending")
    client.put("/api/imports/prefix", json={"prefix": prefix})
    pid = _pending(client)["items"][0]["id"]
    client.patch(f"/api/imports/{pid}", json={"name": too_long})
    client.post("/api/imports/confirm", json={})          # 进不去，留红字
    assert _pending(client)["items"][0]["status"] == "failed"

    r = client.patch(f"/api/imports/{pid}", json={"name": "A1"})
    assert r.json()["status"] == "ok" and r.json()["reason"] == ""


def test_renaming_a_row_without_a_file_keeps_it_failed(client):
    """反过来的那一半：没有暂存文件的行（"这不是图片"）改名字也不该变成能入库的。

    它没有暂存文件，名字改得再好也进不了库；清掉红字会让它看着像好了，
    点确认才发现是被跳过的。
    """
    client.post("/api/images/upload?mode=pending",
                files=[("files", ("坏的.png", b"not an image", "image/png"))])
    pid = _pending(client)["items"][0]["id"]
    j = client.patch(f"/api/imports/{pid}", json={"name": "新名字"}).json()
    assert j["status"] == "failed" and j["reason"]


def test_a_failed_create_image_leaves_no_orphan_in_the_library(client, monkeypatch):
    """写库失败时，上一步刚落盘的原图不能变成没人引用的孤儿文件。

    它不在任何 images 记录里，界面上看不见、也删不掉，却永远占着磁盘；
    用户重试一次就再写一份。
    """
    _upload(client, "甲.png", mode="pending")
    real = db.create_image

    def boom(*a, **k):
        raise RuntimeError("写库失败")

    monkeypatch.setattr(db, "create_image", boom)
    assert client.post("/api/imports/confirm", json={}).json()["imported"] == 0
    assert list(config.IMAGES_DIR.glob("*")) == [], "孤儿原图留在了原图目录里"
    assert _pending(client)["items"][0]["status"] == "failed"

    monkeypatch.setattr(db, "create_image", real)      # 不 undo：那会把临时目录也撤掉
    assert client.post("/api/imports/confirm", json={}).json()["imported"] == 1
    assert _library_names(client) == ["甲"]


def test_a_failing_orphan_cleanup_does_not_take_down_the_whole_confirm(client, monkeypatch):
    """⭐ 清孤儿文件这一下自己炸了，也不能把整批导入带下水。

    trash_file 内部是 shutil.move —— Windows 上对一个刚写完的文件它就可能抛
    （杀软的实时扫描、索引器还按着句柄）。这个调用是**善后**：清不掉只是多占点磁盘，
    让它冒出去却正好触发它本来要防的那件事 —— 客户端 500、剩下的行不再处理、前缀不清。
    """
    _upload_many(client, ["甲.png", "乙.png"], mode="pending")
    client.put("/api/imports/prefix", json={"prefix": "EXP_"})

    real = db.create_image
    calls = {"n": 0}

    def flaky(*a, **kw):                                 # 第一行写库失败 → 走善后那条路
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("写库失败")
        return real(*a, **kw)

    def boom(*a, **kw):                                  # 善后自己也炸
        raise OSError("文件被占用，挪不动")

    monkeypatch.setattr(db, "create_image", flaky)
    monkeypatch.setattr(storage, "trash_file", boom)

    r = client.post("/api/imports/confirm", json={})
    assert r.status_code == 200, "善后那一炸冲出了 confirm —— 客户端只会看到 500"
    assert r.json()["imported"] == 1                     # 后面那行照常处理
    assert _library_names(client) == ["EXP_乙"]
    left = _pending(client)["items"]                     # 失败那行留着带红字
    assert len(left) == 1 and left[0]["status"] == "failed"
    assert "写库失败" in left[0]["reason"]
    assert _pending(client)["prefix"] == ""              # 前缀照清


def test_an_ok_row_without_a_staged_file_is_skipped_not_500(client):
    """今天到不了的一行：status='ok' 却没有暂存文件。

    真出现了也不能抛出去 —— 那会让整批 500、后面的行全不处理、前缀也不清。
    """
    _upload_many(client, ["甲.png", "乙.png"], mode="pending")
    conn = db.get_conn(config.DB_PATH)
    try:
        db.add_pending(conn, "空的", "空的.png", 0, None, "ok", "", "upload")
    finally:
        conn.close()

    r = client.post("/api/imports/confirm", json={})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 2
    assert sorted(_library_names(client)) == sorted(["甲", "乙"])


def test_two_confirm_clicks_at_once_import_only_once(client, monkeypatch):
    """⭐ 「确认导入」点两下（或者手机和电脑同时点）不能把同一批照片入库两遍。

    confirm_pending 是同步函数，Starlette 会把它丢进线程池 —— 两个请求是**真的并行**，
    各自开自己的数据库连接、读到同一份待确认列表。没有互斥的话同一批照片进库两遍；
    覆盖那条路上更糟：T2 会把 T1 刚建好的那张当成"旧照片"删进 data/trash/。
    """
    _upload_many(client, ["甲.png", "乙.png"], mode="pending")

    real = storage.store_original

    def slow(data):
        time.sleep(0.15)          # 把并发窗口拉开：不拉开这个竞态是碰运气的
        return real(data)

    monkeypatch.setattr(storage, "store_original", slow)

    results, errors = [], []

    def click():
        try:
            results.append(client.post("/api/imports/confirm", json={}).json())
        except Exception as e:                     # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=click) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert sorted(_library_names(client)) == sorted(["甲", "乙"])   # 两张，不是四张
    assert sum(r["imported"] for r in results) == 2
    assert _pending(client)["items"] == []
