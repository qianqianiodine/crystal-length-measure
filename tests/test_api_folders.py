"""文件夹 API 测试。

⚠️ 必须 monkeypatch config 里的目录，否则会往用户真实的 data/ 里写东西。

最要紧的一组是「删除的条件连坐」：一张照片同时在 A、B 两个文件夹里，
删 A 时它**不能**被删 —— 它只是从 A 里出来。这是多对多文件夹唯一安全的做法。
"""
import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

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


def _png(val=200) -> bytes:
    ok, buf = cv2.imencode(".png", np.full((40, 50, 3), val, np.uint8))
    assert ok
    return buf.tobytes()


def _photo(client, name="照片.png") -> int:
    r = client.post("/api/images/upload",
                    files=[("files", (name, _png(), "image/png"))])
    return r.json()["ok"][0]["id"]


def _mk(client, name, parent=None) -> int:
    r = client.post("/api/folders", json={"name": name, "parent_id": parent})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _tree(client):
    return client.get("/api/folders").json()


def _names(client) -> list[tuple[str, int]]:
    return [(f["name"], f["depth"]) for f in _tree(client)["items"]]


def _put_folders(client, image_id, ids):
    return client.put(f"/api/images/{image_id}/folders", json={"folder_ids": ids})


# ---------------- 树 ----------------

def test_tree_starts_empty(client):
    d = _tree(client)
    assert d == {"items": [], "total": 0, "uncategorized": 0}


def test_tree_is_depth_first_with_depth(client):
    """界面靠「后代紧跟在自己后面、depth 更大」算子树，这个顺序是接口契约。"""
    a = _mk(client, "6月批次")
    a1 = _mk(client, "样品A", a)
    a2 = _mk(client, "样品B", a)
    b = _mk(client, "7月批次")
    assert _names(client) == [("6月批次", 0), ("样品A", 1), ("样品B", 1), ("7月批次", 0)]
    assert [f["id"] for f in _tree(client)["items"]] == [a, a1, a2, b]


def test_tree_counts_include_subfolders(client):
    """父文件夹的数字 = 整棵子树的张数（点进去看到的格子数就是这个数）。

    2026-09-23 改的：原先是"只算直接放进去的"，用户要求点大文件夹能看到
    子文件夹里的照片，两个数字必须跟着一起改 —— 不然树上写 1 张、点进去 2 张。
    """
    a = _mk(client, "6月批次")
    sub = _mk(client, "样品A", a)
    p1, p2 = _photo(client, "a.png"), _photo(client, "b.png")
    _put_folders(client, p1, [sub])
    _put_folders(client, p2, [a])

    by = {f["id"]: f["count"] for f in _tree(client)["items"]}
    assert by[a] == 2 and by[sub] == 1


def test_tree_totals(client):
    a = _mk(client, "6月批次")
    p1, p2, p3 = _photo(client, "a.png"), _photo(client, "b.png"), _photo(client, "c.png")
    _put_folders(client, p1, [a])
    _put_folders(client, p2, [a])
    d = _tree(client)
    assert d["total"] == 3 and d["uncategorized"] == 1     # p3 谁的都不是
    assert {f["id"]: f["count"] for f in d["items"]}[a] == 2


def test_nesting_has_no_depth_limit(client):
    pid = None
    for i in range(15):
        pid = _mk(client, f"第{i}层", pid)
    items = _tree(client)["items"]
    assert len(items) == 15
    assert items[-1]["depth"] == 14


# ---------------- 新建 ----------------

def test_create_at_top_level(client):
    fid = _mk(client, "6月批次")
    assert _tree(client)["items"][0] == {
        "id": fid, "name": "6月批次", "parent_id": None, "depth": 0, "count": 0}


def test_create_rejects_missing_parent(client):
    r = client.post("/api/folders", json={"name": "孤儿", "parent_id": 999})
    assert r.status_code == 404
    assert "找不到" in r.json()["detail"]


def test_create_rejects_blank_name(client):
    r = client.post("/api/folders", json={"name": "   "})
    assert r.status_code == 400
    assert "名字" in r.json()["detail"]


def test_sibling_names_do_not_collide(client):
    """同一个父级下重名会加 " (2)"；不同父级下同名互不影响。"""
    a = _mk(client, "批次")
    _mk(client, "批次")
    assert [f["name"] for f in _tree(client)["items"]] == ["批次", "批次 (2)"]

    b = _mk(client, "另一批")
    _mk(client, "批次", b)                       # 换了个父级，不用加后缀
    assert {f["id"]: f["name"] for f in _tree(client)["items"]}[a] == "批次"
    assert len([f for f in _tree(client)["items"] if f["name"] == "批次"]) == 2


# ---------------- 改名 ----------------

def test_rename(client):
    fid = _mk(client, "旧名字")
    r = client.patch(f"/api/folders/{fid}", json={"name": "新名字"})
    assert r.json()["name"] == "新名字"
    assert _names(client) == [("新名字", 0)]


def test_rename_to_own_name_is_not_a_collision(client):
    """改成自己现在的名字不该变成 "X (2)"。"""
    fid = _mk(client, "6月批次")
    r = client.patch(f"/api/folders/{fid}", json={"name": "6月批次"})
    assert r.json()["name"] == "6月批次"


def test_rename_into_a_taken_name_gets_suffix(client):
    _mk(client, "6月批次")
    fid = _mk(client, "7月批次")
    assert client.patch(f"/api/folders/{fid}", json={"name": "6月批次"}).json()["name"] == "6月批次 (2)"


def test_rename_missing_folder_404(client):
    assert client.patch("/api/folders/999", json={"name": "x"}).status_code == 404


# ---------------- 移动 ----------------

def test_move_into_another_folder(client):
    a, b = _mk(client, "A"), _mk(client, "B")
    assert client.patch(f"/api/folders/{a}", json={"parent_id": b}).json()["parent_id"] == b
    assert _names(client) == [("B", 0), ("A", 1)]


def test_move_back_to_top_level(client):
    a = _mk(client, "A")
    b = _mk(client, "B", a)
    r = client.patch(f"/api/folders/{b}", json={"parent_id": None})
    assert r.json()["parent_id"] is None
    assert _names(client) == [("A", 0), ("B", 0)]


def test_move_rejects_into_itself(client):
    a = _mk(client, "A")
    r = client.patch(f"/api/folders/{a}", json={"parent_id": a})
    assert r.status_code == 400
    assert "自己" in r.json()["detail"]
    assert _names(client) == [("A", 0)]          # 树没被动过


def test_move_rejects_into_own_descendant(client):
    """A > B > C，把 A 挪到 C 底下会成环 —— 必须拦住，否则整棵树从根上消失。"""
    a = _mk(client, "A")
    b = _mk(client, "B", a)
    c = _mk(client, "C", b)
    r = client.patch(f"/api/folders/{a}", json={"parent_id": c})
    assert r.status_code == 400
    assert "子文件夹" in r.json()["detail"]
    assert _names(client) == [("A", 0), ("B", 1), ("C", 2)]


def test_move_rejects_missing_target(client):
    a = _mk(client, "A")
    assert client.patch(f"/api/folders/{a}", json={"parent_id": 999}).status_code == 404


def test_rename_checks_against_the_new_parent(client):
    """同时改名 + 移动时，重名要按**新**父级查。"""
    a = _mk(client, "A")
    b = _mk(client, "B")
    _mk(client, "同名", b)
    fid = _mk(client, "别的", a)
    r = client.patch(f"/api/folders/{fid}", json={"name": "同名", "parent_id": b})
    assert r.json()["name"] == "同名 (2)"


# ---------------- 复制 ----------------

def test_copy_duplicates_structure_and_membership(client):
    """复制的是文件夹结构 + 归档关系；照片文件一张都不复制。"""
    a = _mk(client, "6月批次")
    sub = _mk(client, "样品A", a)
    p = _photo(client, "a.png")
    _put_folders(client, p, [sub])

    new_id = client.post(f"/api/folders/{a}/copy", json={}).json()["id"]
    items = _tree(client)["items"]
    assert [(f["name"], f["depth"]) for f in items] == [
        ("6月批次", 0), ("样品A", 1), ("6月批次 (2)", 0), ("样品A", 1)]

    # 副本根节点紧跟其后就是它的子文件夹（靠的就是接口那个深度优先契约）
    at = next(i for i, f in enumerate(items) if f["id"] == new_id)
    copy_sub = items[at + 1]
    assert copy_sub["name"] == "样品A" and copy_sub["depth"] == 1 and copy_sub["id"] != sub

    by = {f["id"]: f["count"] for f in items}
    assert by[sub] == 1 and by[copy_sub["id"]] == 1     # 两个「样品A」里都有它
    # 数字是子树计数：照片虽然在子文件夹里，根节点看下去也装得到（2026-09-23 改）
    assert by[new_id] == 1
    assert _tree(client)["total"] == 1                 # 还是那一张，没变成两份


def test_copy_membership_is_shared_not_duplicated(client):
    """副本里的照片 = 同一张照片「也放在这儿」，不是新的一份记录。"""
    a = _mk(client, "A")
    p = _photo(client, "a.png")
    _put_folders(client, p, [a])
    new_id = client.post(f"/api/folders/{a}/copy", json={}).json()["id"]

    got = client.get("/api/images").json()["items"]
    assert len(got) == 1
    assert sorted(got[0]["folders"]) == sorted([a, new_id])


def test_copy_into_a_folder(client):
    """副本挪进 B 里面，B 底下没有重名，所以不用加后缀 —— 跟资源管理器一个道理。"""
    a = _mk(client, "A")
    b = _mk(client, "B")
    client.post(f"/api/folders/{a}/copy", json={"parent_id": b})
    assert _names(client) == [("A", 0), ("B", 0), ("A", 1)]


def test_copy_missing_folder_404(client):
    assert client.post("/api/folders/999/copy", json={}).status_code == 404


# ---------------- 删除 ----------------

def test_delete_removes_the_whole_subtree(client):
    a = _mk(client, "A")
    _mk(client, "B", a)
    _mk(client, "C", a)
    r = client.delete(f"/api/folders/{a}")
    assert r.json()["folders_deleted"] == 3
    assert _tree(client)["items"] == []


def test_delete_without_photos_keeps_them(client):
    a = _mk(client, "A")
    p = _photo(client, "a.png")
    _put_folders(client, p, [a])

    r = client.delete(f"/api/folders/{a}?with_photos=false").json()
    assert r == {"folders_deleted": 1, "images_deleted": 0, "images_kept": 1}
    d = _tree(client)
    assert d["total"] == 1 and d["uncategorized"] == 1       # 照片还在，变成未分类


def test_delete_with_photos_removes_only_them(client):
    a = _mk(client, "A")
    p = _photo(client, "a.png")
    _put_folders(client, p, [a])

    r = client.delete(f"/api/folders/{a}?with_photos=true").json()
    assert r["images_deleted"] == 1
    assert client.get("/api/images").json()["total"] == 0


def test_delete_keeps_photos_that_live_in_other_folders(client):
    """⭐ 这条是多对多文件夹最容易出事的地方。

    照片 X 同时在「6月批次」和「样品A」里。删「6月批次」时 X 不能被删 ——
    它还属于「样品A」。否则用户会莫名其妙丢掉一张（可能已经量过的）照片。
    """
    a = _mk(client, "6月批次")
    b = _mk(client, "样品A")
    shared = _photo(client, "两边都在.png")
    only_a = _photo(client, "只在这儿.png")
    _put_folders(client, shared, [a, b])
    _put_folders(client, only_a, [a])

    prev = client.get(f"/api/folders/{a}/delete-preview").json()
    assert prev == {"folders": 1, "images_in_subtree": 2,
                    "images_only_here": 1, "images_elsewhere": 1}

    r = client.delete(f"/api/folders/{a}?with_photos=true").json()
    assert r["images_deleted"] == 1 and r["images_kept"] == 1

    left = client.get("/api/images").json()["items"]
    assert [i["name"] for i in left] == ["两边都在"]           # 共享的那张活着
    assert left[0]["folders"] == [b]                          # 只剩「样品A」


def test_delete_preview_does_not_change_anything(client):
    a = _mk(client, "A")
    p = _photo(client, "a.png")
    _put_folders(client, p, [a])
    client.get(f"/api/folders/{a}/delete-preview")
    assert client.get("/api/images").json()["total"] == 1
    assert len(_tree(client)["items"]) == 1


def test_delete_preview_counts_subfolders(client):
    a = _mk(client, "A")
    b = _mk(client, "B", a)
    p = _photo(client, "a.png")
    _put_folders(client, p, [b])
    assert client.get(f"/api/folders/{a}/delete-preview").json() == {
        "folders": 2, "images_in_subtree": 1,
        "images_only_here": 1, "images_elsewhere": 0}


def test_delete_missing_folder_404(client):
    assert client.delete("/api/folders/999").status_code == 404


# ---------------- 归档 ----------------

def test_set_image_folders_is_covering(client):
    a, b = _mk(client, "A"), _mk(client, "B")
    p = _photo(client, "a.png")
    assert _put_folders(client, p, [a, b]).json()["folders"] == [a, b]
    assert _put_folders(client, p, [b]).json()["folders"] == [b]     # 覆盖，不是累加
    assert _put_folders(client, p, []).json()["folders"] == []       # 空 = 回未分类


def test_set_image_folders_dedupes(client):
    a = _mk(client, "A")
    p = _photo(client, "a.png")
    assert _put_folders(client, p, [a, a]).json()["folders"] == [a]


def test_set_image_folders_rejects_unknown_folder(client):
    p = _photo(client, "a.png")
    assert _put_folders(client, p, [999]).status_code == 404


def test_set_image_folders_rejects_unknown_image(client):
    a = _mk(client, "A")
    assert _put_folders(client, 999, [a]).status_code == 404


def test_gallery_summary_carries_folder_ids(client):
    a = _mk(client, "A")
    p = _photo(client, "a.png")
    _put_folders(client, p, [a])
    assert client.get("/api/images").json()["items"][0]["folders"] == [a]


# ---------------- 按文件夹筛图 ----------------

def test_list_filters_by_folder(client):
    a, b = _mk(client, "A"), _mk(client, "B")
    pa, pb, pn = _photo(client, "只A.png"), _photo(client, "只B.png"), _photo(client, "没归档.png")
    _put_folders(client, pa, [a])
    _put_folders(client, pb, [b])
    _put_folders(client, pn, [])

    def names(qs):
        return [i["name"] for i in client.get(f"/api/images?{qs}").json()["items"]]

    assert names(f"folder={a}") == ["只A"]
    assert names(f"folder={b}") == ["只B"]
    assert names("folder=0") == ["没归档"]            # 0 = 未分类
    assert set(names("")) == {"只A", "只B", "没归档"}


def test_list_filter_folder_total_is_not_page_size(client):
    a = _mk(client, "A")
    for i in range(3):
        _put_folders(client, _photo(client, f"p{i}.png"), [a])
    d = client.get(f"/api/images?folder={a}&limit=1").json()
    assert d["total"] == 3 and len(d["items"]) == 1


def test_list_filter_folder_includes_subfolders(client):
    """点父文件夹要看到子文件夹里的照片（2026-09-23 用户要求改的）。

    和树上那个数字是同一套条件 —— 两边不一致的话就是"树上写 N 张、
    点进去看到别的张数"。db 层的等价断言在 tests/test_db_subtree.py。
    """
    a = _mk(client, "A")
    sub = _mk(client, "B", a)
    p = _photo(client, "深处的.png")
    _put_folders(client, p, [sub])
    d = client.get(f"/api/images?folder={a}").json()
    assert d["total"] == 1
    assert [i["name"] for i in d["items"]] == ["深处的"]   # 名字是文件名去掉后缀


def test_list_folder_rejects_negative(client):
    assert client.get("/api/images?folder=-1").status_code == 422


# ---------------- 仓储层边界 ----------------

def test_shared_photo_survives_deleting_one_of_its_folders_at_db_level(client, monkeypatch):
    """绕过 API 直接验仓储：删文件夹**不碰**照片，只掉关系。"""
    a, b = _mk(client, "A"), _mk(client, "B")
    p = _photo(client, "a.png")
    _put_folders(client, p, [a, b])

    conn = db.get_conn()
    try:
        db.delete_folder(conn, a)
        assert db.get_image(conn, p) is not None
        assert db.image_folders(conn, p) == [b]
    finally:
        conn.close()
