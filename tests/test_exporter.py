"""导出功能测试。"""
import csv
import io

import numpy as np
import pytest

from app import calibrate, config, db, exporter, imageio, measure


def _rows(text):
    """解析 CSV 行。csv.reader 会把开头的 BOM 算进第一个单元格，先剥掉。"""
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    """把「用户设的孔直径」隔离到 tmp_path。

    ⚠️ 不隔离的话，下面 from_well_diameter(...) 会去读**用户真实的**
    `data/settings.json`（见 app/settings.py）—— 用户把孔直径改了之后，
    这里按 34 um/px 写的断言会集体变红。
    """
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")


@pytest.fixture()
def populated(tmp_path, monkeypatch):
    """一个已建库、有图、有标定、有测量的环境。

    路径按「相对于 IMAGES_DIR」存（正式导入流程的约定）。
    """
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "images")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")

    c = db.get_conn(tmp_path / "t.db")
    db.init_db(c)

    # 造一张真实存在的图（100x150）
    img = np.full((100, 150, 3), 140, np.uint8)
    imageio.save_rgb(tmp_path / "images" / "test.png", img)
    iid = db.create_image(c, "实验A", "orig.jpg", "test.png", "drag")

    calibrate.apply_calibration(c, iid, calibrate.from_well_diameter(100.0))  # 34 um/px
    measure.add_line(c, iid, 10, 10, 60, 10, note="晶体A")     # 50px → 1700um
    measure.add_line(c, iid, 20, 40, 70, 40, note="疑似盐晶")   # 50px → 1700um
    yield c, iid, tmp_path
    c.close()


# ---------- CSV ----------

def test_csv_header_matches_spec():
    assert exporter.CSV_HEADER == [
        "图像名称", "图像唯一标识", "测量编号", "起点坐标", "终点坐标",
        "像素长度", "实际长度(μm)", "标定系数", "备注", "测量时间",
    ]


def test_csv_single_image_has_two_rows(populated):
    conn, iid, _ = populated
    text = exporter.measurements_to_csv(conn, image_id=iid)
    rows = _rows(text)
    assert rows[0] == exporter.CSV_HEADER
    assert len(rows) == 3          # 表头 + 2 条测量
    assert rows[1][0] == "实验A"
    assert rows[1][2] == "1"       # 测量编号
    assert float(rows[1][6]) == pytest.approx(1700.0, rel=1e-6)


def test_csv_contains_coordinates(populated):
    conn, iid, _ = populated
    rows = _rows(exporter.measurements_to_csv(conn, iid))
    assert "10.0" in rows[1][3]
    assert "60.0" in rows[1][4]


def test_csv_export_all_images(populated):
    conn, iid, _ = populated
    rows = _rows(exporter.measurements_to_csv(conn))
    assert len(rows) == 3          # 只有一张图，仍是 2 条


def test_csv_has_bom_for_excel(populated):
    """带 BOM 才能在 Excel 里正确显示中文。"""
    conn, iid, _ = populated
    text = exporter.measurements_to_csv(conn, iid)
    assert text.startswith("﻿")


def test_export_csv_file_writes(populated):
    conn, iid, tmp_path = populated
    out = exporter.export_csv_file(conn, tmp_path / "导出.csv", image_id=iid)
    assert out.exists()
    content = out.read_text(encoding="utf-8-sig")
    assert "实验A" in content


def test_csv_empty_when_no_measurements(tmp_path):
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(c)
    rows = _rows(exporter.measurements_to_csv(c))
    assert len(rows) == 1          # 只有表头
    c.close()


def test_csv_missing_image_raises(tmp_path):
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(c)
    with pytest.raises(ValueError):
        exporter.measurements_to_csv(c, image_id=999)
    c.close()


# ---------- PNG ----------

def test_build_annotated_image_shape(populated):
    conn, iid, _ = populated
    img = exporter.build_annotated_image(conn, iid, supersample=1)
    assert img.shape == (100, 150, 3)


def test_build_annotated_image_supersample(populated):
    conn, iid, _ = populated
    img = exporter.build_annotated_image(conn, iid, supersample=2)
    assert img.shape == (200, 300, 3)


def test_build_annotated_with_focus_crop(populated):
    conn, iid, _ = populated
    img = exporter.build_annotated_image(conn, iid, supersample=1,
                                         crop={"mode": "focus", "seq": 1})
    # 那条线在 y=10 附近，裁剪后应是窄条，高度明显小于 100
    assert img.shape[0] < 100
    assert img.shape[1] <= 150


def test_build_annotated_does_not_touch_the_stored_photo(populated):
    """★ 导出是只读的：连导两次不能把标注烧进源文件。"""
    conn, iid, tmp_path = populated
    src = tmp_path / "images" / "test.png"
    before = src.read_bytes()
    exporter.build_annotated_image(conn, iid, supersample=1)
    exporter.build_annotated_image(conn, iid, supersample=1)
    assert src.read_bytes() == before


def test_build_annotated_works_with_absolute_path(tmp_path, monkeypatch):
    """★ 试用页把照片的**绝对路径**存进库（照片留在 样品集/，不拷贝进 data/）。

    真实数据就是这个形状，相对路径那条反倒是备用分支 —— 两个都得能跑。
    """
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    src = tmp_path / "样品集" / "孔.png"
    imageio.save_rgb(src, np.full((80, 120, 3), 140, np.uint8))

    c = db.get_conn(tmp_path / "t.db")
    db.init_db(c)
    iid = db.create_image(c, "绝对路径图", "孔.png", str(src), "sample")
    calibrate.apply_calibration(c, iid, calibrate.from_well_diameter(50.0))
    measure.add_line(c, iid, 10, 10, 60, 10)

    assert exporter.build_annotated_image(c, iid, supersample=1).shape == (80, 120, 3)
    c.close()


def test_export_annotated_png_writes(populated):
    conn, iid, tmp_path = populated
    out = exporter.export_annotated_png(conn, iid, tmp_path / "标注.png")
    assert out.exists()
    back = imageio.load_rgb(out)
    assert back.shape[2] == 3


def test_export_missing_image_raises(tmp_path):
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(c)
    with pytest.raises(ValueError):
        exporter.build_annotated_image(c, 999)
    c.close()


def test_export_without_calibration_raises(populated):
    conn, iid, _ = populated
    conn.execute("DELETE FROM calibrations WHERE image_id=?", (iid,))
    conn.commit()
    with pytest.raises(ValueError, match="标定"):
        exporter.build_annotated_image(conn, iid)


def test_export_honours_dragged_label_position(populated):
    """★ 用户在页面上拖开的长度数字，导出图上要出现在同一个地方。"""
    conn, iid, _ = populated
    plain = exporter.build_annotated_image(conn, iid, supersample=1,
                                           scale_bar=False)
    conn.execute("UPDATE measurements SET label_dy=40 WHERE image_id=?", (iid,))
    conn.commit()
    moved = exporter.build_annotated_image(conn, iid, supersample=1,
                                           scale_bar=False)
    assert np.any(plain != moved)


# ---------------- 裁剪的解析与切片 ----------------

def test_parse_crop_normal():
    from app import exporter

    c = exporter.parse_crop('{"x0": 120, "y0": 340, "x1": 920, "y1": 940}')
    assert c == {"mode": "rect", "rect": [120.0, 340.0, 920.0, 940.0]}


@pytest.mark.parametrize("raw", [
    None, "", "null", "[]", "{}", "{", "不是 JSON",
    '{"x0": 1, "y0": 2, "x1": 3}',                      # 缺字段
    '{"x0": 1, "y0": 2, "x1": "甲", "y1": 4}',          # 不是数
    '{"x0": 1, "y0": 2, "x1": null, "y1": 4}',
])
def test_parse_crop_坏数据一律_None(raw):
    """裁剪只是"怎么看"。读不懂就当没设 —— 绝不让它把打开照片这条路弄挂。"""
    from app import exporter

    assert exporter.parse_crop(raw) is None


def test_apply_crop_切得对():
    from app import exporter

    img = np.zeros((100, 200, 3), np.uint8)
    img[10:60, 30:90] = 255
    out = exporter.apply_crop(img, {"mode": "rect", "rect": [30, 10, 90, 60]})
    assert out.shape == (50, 60, 3)
    assert out.min() == 255


def test_apply_crop_越界自动夹住():
    from app import exporter

    img = np.zeros((100, 200, 3), np.uint8)
    out = exporter.apply_crop(img, {"mode": "rect", "rect": [-50, -50, 5000, 5000]})
    assert out.shape == (100, 200, 3)          # 夹回图内，不炸


def test_apply_crop_全在画面外退回原图():
    """整块都在图外面时，宁可给原图，也不给一张 0×0 的空图。"""
    from app import exporter

    img = np.zeros((100, 200, 3), np.uint8)
    out = exporter.apply_crop(img, {"mode": "rect", "rect": [500, 500, 600, 600]})
    assert out.shape == (100, 200, 3)


def test_apply_crop_None_原样返回():
    from app import exporter

    img = np.zeros((100, 200, 3), np.uint8)
    assert exporter.apply_crop(img, None) is img


def test_标注图带上用户的画质调整(populated):
    """库里 enhance 列调过亮度时，服务端出的标注图必须比不调时亮。

    ⚠️ 这条钉的是一个**既有 bug**：原来 exporter 去 transform 列里找 t["enh"]，
    而全项目没有任何地方往那里写过 —— 于是服务端标注图永远丢掉用户的画质调整。
    """
    conn, iid, _ = populated

    def 平均亮度():
        return float(exporter.build_annotated_image(conn, iid, supersample=1).mean())

    base = 平均亮度()
    db.update_image(conn, iid, enhance='{"b": 1.6, "c": 1.0, "s": 1.0}')
    bright = 平均亮度()
    assert bright > base + 3, f"调亮后应该更亮：{base} → {bright}"


# ---------------- 批量导出用的那张图 ----------------

def test_export_image_标定过的带标注(populated):
    """量过的图：有标尺也有品红的测量线。"""
    conn, iid, _ = populated
    img = exporter.export_image(conn, iid)

    assert img.shape[:2] == (100, 150)          # 原始分辨率，不缩放
    # 品红 = 测量线。纯灰底图上出现品红，说明标注真的画上去了。
    # ⚠️ 本项目的「品红」是 #FF2D55 = (255,45,85)（app/measure.py 的调色板第一色），
    # 不是纯洋红 (255,0,255) —— 判据要按前者写，否则永远找不到。
    r, g, b = img[..., 0].astype(int), img[..., 1].astype(int), img[..., 2].astype(int)
    assert ((r > 200) & (g < 120) & (b < 150)).sum() > 0, "没找到品红的测量线"


def test_export_image_没标定的也给图(populated):
    """一张没量过的照片，导出**不该失败** —— 整批导出不能被它卡住。

    库里允许有没标定的照片（用户拍了一堆，只量了其中几张）。
    """
    conn, _, tmp_path = populated
    bare = db.create_image(conn, "没量过的", "b.jpg", "test.png", "upload")

    img = exporter.export_image(conn, bare)     # 不抛异常
    assert img.shape[:2] == (100, 150)
    r, g, b = img[..., 0].astype(int), img[..., 1].astype(int), img[..., 2].astype(int)
    assert ((r > 200) & (g < 120) & (b < 150)).sum() == 0, "没标定的不该有测量线"


def test_export_image_带上裁剪(populated):
    conn, iid, _ = populated
    db.update_image(conn, iid, crop='{"x0": 10, "y0": 10, "x1": 60, "y1": 50}')
    assert exporter.export_image(conn, iid).shape[:2] == (40, 50)


def test_export_image_图不存在(populated):
    conn, _, _ = populated
    with pytest.raises(ValueError):
        exporter.export_image(conn, 99999)


def test_export_image_图太大时不许静默降级(populated, monkeypatch):
    """`annotate` 的「导出的图太大了」也是 ValueError。以前被 except 一起吞掉，
    用户会拿到一张**没有标尺和测量线的干净原图、而且没有任何提示** ——
    对科研图这是最坏的一种错（看不出来，还以为量过）。
    """
    from app import annotate

    conn, iid, _ = populated
    monkeypatch.setattr(annotate, "MAX_OUTPUT_PIXELS", 10)   # 逼出那条
    with pytest.raises(ValueError, match="太大"):
        exporter.export_image(conn, iid)


# ---------------- 路径安全（这一组最要紧） ----------------

@pytest.mark.parametrize("坏", ["..\\..\\Windows", "../../Windows", "..", "a/../b"])
def test_安全段_不含上跳(坏):
    s = exporter._safe_segment(坏)
    assert ".." not in s
    assert "/" not in s and "\\" not in s


@pytest.mark.parametrize("坏", ["D:\\某处", "D:", "C:/Windows"])
def test_安全段_不含盘符冒号(坏):
    assert ":" not in exporter._safe_segment(坏)


# ⚠️ "CON " 这条不是凑数：去掉「先 rstrip 再查保留名」这一步时，
# 只有它会红 —— 规则 3 的返回值部分被规则 5 的 rstrip 兜住了，
# 但「结尾空格让保留名查不出来」这个洞只有这条能抓到（变异验证过）。
@pytest.mark.parametrize("坏", ["CON", "con", "nul", "COM1", "lpt9", "CON.png", "CON "])
def test_安全段_保留设备名被加前缀(坏):
    s = exporter._safe_segment(坏)
    assert s.startswith("_"), f"{坏} → {s}"
    assert s.split(".")[0].upper() != 坏.split(".")[0].upper()


def test_安全段_非法字符全换掉():
    s = exporter._safe_segment('a<b>c:d"e|f?g*h')
    assert not set(s) & set('<>:"/\\|?*'), s
    assert s  # 不能整段被吃掉


@pytest.mark.parametrize("坏", ["样品1 ", "样品1.", "样品1 . . ", "   "])
def test_安全段_结尾空格和点被去掉(坏):
    s = exporter._safe_segment(坏)
    assert not s.endswith(" ") and not s.endswith("."), repr(s)
    assert s        # 全空 → 未命名


def test_安全段_超长被截到60():
    s = exporter._safe_segment("样" * 200)
    assert len(s) <= 60


def test_安全段_空的和全非法都给未命名():
    assert exporter._safe_segment("") == "未命名"
    assert exporter._safe_segment("   ") == "未命名"


def test_安全段_正常名字原样留着():
    assert exporter._safe_segment("样品1") == "样品1"
    assert exporter._safe_segment("A1-1") == "A1-1"


# ---------------- 导出目录与文件名的拼装 ----------------

@pytest.fixture()
def 树(tmp_path, monkeypatch):
    """一个小文件夹树：样品A / 6月批次（套在里面）、样品B。"""
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "images")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(c)
    imageio.save_rgb(tmp_path / "images" / "t.png", np.full((40, 40, 3), 128, np.uint8))

    a = db.create_folder(c, "样品A", None)
    b = db.create_folder(c, "6月批次", a)
    other = db.create_folder(c, "样品B", None)
    yield c, {"a": a, "b": b, "other": other}
    c.close()


def _plan(conn, ids, members):
    images = [db.get_image(conn, i) for i in ids]
    folders = conn.execute("SELECT id, name, parent_id FROM folders").fetchall()
    return exporter.export_plan(images, folders, members)


def test_导出计划_文件夹名做前缀(树):
    conn, f = 树
    iid = db.create_image(conn, "照片", "IMG_2201.jpg", "t.png", "upload")
    db.update_image(conn, iid, tags='["A1-1"]')
    计划 = _plan(conn, [iid], {iid: [f["a"]]})
    assert 计划 == [{"image_id": iid, "parts": ["样品A"],
                     "name": "样品A_A1-1.png", "renamed": False}]


def test_导出计划_没标签就用原文件名(树):
    conn, f = 树
    iid = db.create_image(conn, "照片", "IMG_2201.jpg", "t.png", "upload")
    计划 = _plan(conn, [iid], {iid: [f["a"]]})
    assert 计划[0]["name"] == "样品A_IMG_2201.png"


def test_导出计划_未分类的单独一层且不加前缀(树):
    conn, f = 树
    iid = db.create_image(conn, "照片", "IMG_2203.jpg", "t.png", "upload")
    db.update_image(conn, iid, tags='["A1-1"]')
    计划 = _plan(conn, [iid], {iid: []})
    assert 计划 == [{"image_id": iid, "parts": ["未分类"],
                     "name": "A1-1.png", "renamed": False}]


def test_导出计划_套层照搬(树):
    conn, f = 树
    iid = db.create_image(conn, "照片", "IMG_1.jpg", "t.png", "upload")
    计划 = _plan(conn, [iid], {iid: [f["b"]]})
    assert 计划[0]["parts"] == ["样品A", "6月批次"]
    assert 计划[0]["name"] == "6月批次_IMG_1.png"


def test_导出计划_一张图两个文件夹就两份(树):
    """多对多是"也放进来"，不是"再存一份"（db.py:500）——
    导出只是把多个归属如实摊开，不替用户挑一个。"""
    conn, f = 树
    iid = db.create_image(conn, "照片", "IMG_1.jpg", "t.png", "upload")
    计划 = _plan(conn, [iid], {iid: [f["a"], f["other"]]})
    assert len(计划) == 2
    assert {p["parts"][0] for p in 计划} == {"样品A", "样品B"}


def test_导出计划_重名自动加序号(树):
    """跨照片重复填 A1-1 是允许的（设计 §六 第 8 条），
    但**同一次导出绝不静默覆盖** —— 那会悄悄丢掉一张已经量好的图。"""
    conn, f = 树
    a = db.create_image(conn, "甲", "IMG_1.jpg", "t.png", "upload")
    b = db.create_image(conn, "乙", "IMG_2.jpg", "t.png", "upload")
    db.update_image(conn, a, tags='["A1-1"]')
    db.update_image(conn, b, tags='["A1-1"]')
    计划 = _plan(conn, [a, b], {a: [f["a"]], b: [f["a"]]})
    assert [p["name"] for p in 计划] == ["样品A_A1-1.png", "样品A_A1-1 (2).png"]
    assert [p["renamed"] for p in 计划] == [False, True]


def test_导出计划_只有大小写不同的文件夹也算撞名(树):
    """Windows 上 NTFS 不区分大小写：`样品A` 和 `样品a` 是**同一个目录**，
    两个目录下的 `样品A_A1-1.png` / `样品a_A1-1.png` 也是**同一个文件**。
    去重键不归一化的话，后一张会把前一张**静默覆盖** —— 正是上面那条注释要防的事。

    （标签那条路不用管：接口用严格正则卡死了大小写，小写 `a1-1` 直接拒掉。）"""
    conn, f = 树
    小写 = db.create_folder(conn, "样品a", None)     # 和 f["a"]（样品A）只差大小写
    x = db.create_image(conn, "甲", "IMG_1.jpg", "t.png", "upload")
    y = db.create_image(conn, "乙", "IMG_2.jpg", "t.png", "upload")
    db.update_image(conn, x, tags='["A1-1"]')
    db.update_image(conn, y, tags='["A1-1"]')
    计划 = _plan(conn, [x, y], {x: [f["a"]], y: [小写]})

    # 性质：plan 里任意两条都不能落到大小写不敏感下的同一个路径
    键 = ["/".join(p["parts"] + [p["name"]]).casefold() for p in 计划]
    assert len(set(键)) == len(计划)

    # 去重是加了序号，写出去的文件名仍保留用户原来的大小写
    assert [p["parts"][0] for p in 计划] == ["样品A", "样品a"]
    assert 计划[0]["name"] == "样品A_A1-1.png"
    assert 计划[1]["name"] == "样品a_A1-1 (2).png"
    assert [p["renamed"] for p in 计划] == [False, True]


def test_导出计划_恶意文件夹名被洗干净(树):
    conn, f = 树
    bad = db.create_folder(conn, "..\\..\\Windows", None)
    iid = db.create_image(conn, "照片", "IMG_1.jpg", "t.png", "upload")
    计划 = _plan(conn, [iid], {iid: [bad]})
    assert 计划[0]["parts"] == ["Windows"]
    assert ".." not in "/".join(计划[0]["parts"]) and 计划[0]["name"]


def test_parse_tags_坏数据一律空表():
    assert exporter.parse_tags(None) == []
    assert exporter.parse_tags("{ 不是 JSON") == []
    assert exporter.parse_tags('"甲"') == []
    assert exporter.parse_tags('["A1-1", 5]') == ["A1-1"]
    assert exporter.parse_tags('["A1-1"]') == ["A1-1"]
