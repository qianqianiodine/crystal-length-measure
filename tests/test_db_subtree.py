"""图库按文件夹筛选：点一个父文件夹要看得到子文件夹里的照片。

配套的是 folder_counts() 也要改成子树计数 —— 不然会出现
「树上写 0 张、点进去有 20 张」这种自相矛盾。
"""
import pytest

from app import db


@pytest.fixture()
def tree(tmp_path):
    """样品A / {部分1, 部分2}；三张照片：
       甲 只放进 部分1
       乙 只放进 部分2
       丙 只放进 样品A（父里，没进任何子）
       + 一张没归档的 丁
    """
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(c)
    a = db.create_folder(c, "样品A", None)
    p1 = db.create_folder(c, "部分1", a)
    p2 = db.create_folder(c, "部分2", a)
    jia = db.create_image(c, "甲", "a.jpg", "p1.png", "drag")
    yi = db.create_image(c, "乙", "b.jpg", "p2.png", "drag")
    bing = db.create_image(c, "丙", "c.jpg", "p3.png", "drag")
    ding = db.create_image(c, "丁", "d.jpg", "p4.png", "drag")
    db.set_image_folders(c, jia, [p1])
    db.set_image_folders(c, yi, [p2])
    db.set_image_folders(c, bing, [a])
    yield {"conn": c, "a": a, "p1": p1, "p2": p2,
           "甲": jia, "乙": yi, "丙": bing, "丁": ding}
    c.close()


def _names(rows):
    return sorted(r["name"] for r in rows)


def test_点父文件夹能看到整棵子树的照片(tree):
    c = tree["conn"]
    assert _names(db.list_images(c, folder=tree["a"])) == ["丙", "乙", "甲"]


def test_点子文件夹只看到它自己的(tree):
    c = tree["conn"]
    assert _names(db.list_images(c, folder=tree["p1"])) == ["甲"]


def test_总数和列表是同一套条件(tree):
    c = tree["conn"]
    for fid in (tree["a"], tree["p1"], tree["p2"]):
        assert db.count_images(c, folder=fid) == len(db.list_images(c, folder=fid))


def test_未分类只认一张文件夹都没进的(tree):
    c = tree["conn"]
    assert _names(db.list_images(c, folder=0)) == ["丁"]


def test_不传folder就是全部(tree):
    c = tree["conn"]
    assert _names(db.list_images(c)) == ["丁", "丙", "乙", "甲"]


def test_全部选中拿到的也是整棵子树(tree):
    c = tree["conn"]
    assert sorted(db.ids_matching(c, folder=tree["a"])) == sorted([tree["甲"], tree["乙"], tree["丙"]])


def test_子树计数_父子都在只算一次(tree):
    """丙 在 样品A 里；甲乙在子文件夹里。父的数字必须是 3，不是 3+ 重复。"""
    c = tree["conn"]
    counts = db.folder_counts(c)
    assert counts[tree["a"]] == 3
    assert counts[tree["p1"]] == 1
    assert counts[tree["p2"]] == 1


def test_一张照片同时放进父子也只算一次(tmp_path):
    """这张图同时在父和子里 —— 列表里它只能出现一次。

    ⚠️ 这条是**回归测试**，别删：`_filter` 里的子树条件写成
    `EXISTS (... f.folder_id IN (?,?))` 时，SQLite 3.51.0 会按匹配行数吐重复
    （写这条时真踩到了：`_names(...)` 返回 ['甲','甲']）。改成
    `images.id IN (SELECT ...)` 才对。细节见 app/db.py 那段注释。
    """
    c = db.get_conn(tmp_path / "t2.db")
    db.init_db(c)
    a = db.create_folder(c, "样品A", None)
    p1 = db.create_folder(c, "部分1", a)
    iid = db.create_image(c, "甲", "a.jpg", "p.png", "drag")
    db.set_image_folders(c, iid, [a, p1])
    assert db.folder_counts(c)[a] == 1          # COUNT(DISTINCT image_id)
    assert _names(db.list_images(c, folder=a)) == ["甲"]
    c.close()
