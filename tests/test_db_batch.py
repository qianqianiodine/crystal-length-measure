"""批量操作要用的两个 db 函数。

⚠️ 只碰临时目录里的库，不摸用户真实的 data/。
"""
import pytest

from app import config, db


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "app.db")
    c = db.get_conn(config.DB_PATH)
    db.init_db(c)
    yield c
    c.close()


def _img(conn, name, *, folders=(), lines=0):
    iid = db.create_image(conn, name, f"{name}.jpg", f"{name}.jpg", "upload")
    if folders:
        db.set_image_folders(conn, iid, list(folders))
    for _ in range(lines):
        db.add_measurement(conn, iid, 0, 0, 10, 0, 10.0, 5.0, 0.5)
    return iid


def test_ids_matching_lists_everything(conn):
    ids = [_img(conn, f"图{n}") for n in range(5)]
    assert sorted(db.ids_matching(conn)) == sorted(ids)


def test_ids_matching_same_filter_as_list_images(conn):
    """和图库格子墙必须是同一套筛选 —— 不一致的话「选中全部」会选错东西。"""
    f1 = db.create_folder(conn, "实验A", None)
    _img(conn, "甲", folders=[f1], lines=1)
    _img(conn, "乙")
    _img(conn, "丙", folders=[f1])

    for kwargs in ({"status": "measured"}, {"status": "unmeasured"},
                   {"folder": f1}, {"folder": 0}, {"q": "甲"}):
        from_list = [r["id"] for r in db.list_images(conn, limit=100, **kwargs)]
        assert sorted(db.ids_matching(conn, **kwargs)) == sorted(from_list), kwargs


def test_ids_matching_escapes_like_wildcards(conn):
    """用户真打出 % 时要当普通字符搜，不能变成通配符。"""
    hit = _img(conn, "100%纯")
    _img(conn, "1000纯")
    assert db.ids_matching(conn, q="%") == [hit]


def test_count_measured_counts_images_not_lines(conn):
    """一张图量 3 条线 = 1 张「量过的照片」—— 确认框里那句是「其中 N 张量过线」。"""
    a = _img(conn, "甲", lines=3)
    b = _img(conn, "乙", lines=1)
    c = _img(conn, "丙")
    assert db.count_measured(conn, [a, b, c]) == 2
    assert db.count_measured(conn, [c]) == 0
    assert db.count_measured(conn, []) == 0
