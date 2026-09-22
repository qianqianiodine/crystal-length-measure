"""标定计算测试。

标定基准 = **用户确认的那个圆**（Φ3.4mm）。两条路径：
孔直径（用户在界面上定的圆/椭圆）与手动拉线（兜底）。

⚠️ 像素长度一律在**校正坐标系**下算 —— 标定系数是在校正后的图上得到的，
测量也必须在同一坐标系，否则两者不自洽。
"""
import numpy as np
import pytest

from app import calibrate, config, db


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    """把「用户设的孔直径」隔离到 tmp_path。

    ⚠️ 不隔离的话，不传 real_um 的用例会去读**用户真实的** `data/settings.json`
    （见 app/settings.py）—— 用户在界面上把孔直径改成 2.5mm 之后，下面断言
    3400 / 34.0 的用例会集体变红，而且红得看不出为什么。
    """
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")


@pytest.fixture()
def conn(tmp_path):
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(c)
    yield c
    c.close()


# ---------------- 按孔直径标定（主路径） ----------------

def test_from_well_diameter_uses_default_3400_um():
    r = calibrate.from_well_diameter(diameter_px=106.25)
    assert r.real_length_um == pytest.approx(3400.0)
    assert r.um_per_px == pytest.approx(32.0)
    assert r.method == "well_diameter"


def test_from_well_diameter_accepts_custom_real_um():
    r = calibrate.from_well_diameter(diameter_px=100.0, real_um=2000.0)
    assert r.um_per_px == pytest.approx(20.0)


def test_changed_diameter_actually_changes_the_result():
    """★ 用户在界面上改了孔直径，换算必须当场跟着变。

    钉死一个 Python 陷阱：写成 `real_um: float = config.WELL_DIAMETER_UM`
    这种**默认参数**的话，它只在函数定义那一刻求值一次 —— 用户改完设置它
    纹丝不动，量出来的每个数都静静地偏着，而且一句报错都没有。
    所以这两个函数必须现读（走 app/settings.py）。
    """
    from app import settings

    assert calibrate.from_well_diameter(100.0).um_per_px == pytest.approx(34.0)

    settings.set_well_diameter_um(2500.0)

    r = calibrate.from_well_diameter(diameter_px=100.0)
    assert r.real_length_um == pytest.approx(2500.0)      # 不是 3400
    assert r.um_per_px == pytest.approx(25.0)

    e = calibrate.from_well_ellipse(major_px=100.0, minor_px=80.0)
    assert e.um_per_px == pytest.approx(25.0)             # 椭圆那条路一样

    # 显式传 real_um 仍然优先 —— 手动拉线那条老路径不受影响
    assert calibrate.from_well_diameter(100.0, real_um=1000.0).um_per_px \
        == pytest.approx(10.0)


def test_ellipse_calibration_uses_major_axis():
    """孔被拍成椭圆时，用**长轴**换算 —— 长轴才对应真实的圆直径。

    同一个孔，正圆时直径 100px；被压扁成 100x80 后，
    标定结果必须与正圆时一致（仍是 100px 对应 3.4mm）。
    """
    r = calibrate.from_well_ellipse(major_px=100.0, minor_px=80.0)
    assert r.pixel_length == pytest.approx(100.0)
    assert r.um_per_px == pytest.approx(34.0)


def test_ellipse_accepts_major_smaller_than_minor():
    """长短轴传反了也不该出错 —— 取大的那个当长轴。"""
    r = calibrate.from_well_ellipse(major_px=80.0, minor_px=100.0)
    assert r.pixel_length == pytest.approx(100.0)


# ---------------- 手动拉线（兜底路径） ----------------

def test_from_manual_line_basic():
    r = calibrate.from_manual_line(0, 0, 100, 0, real_um=3400.0)
    assert r.pixel_length == pytest.approx(100.0)
    assert r.um_per_px == pytest.approx(34.0)
    assert r.method == "manual"


def test_from_manual_line_with_correction():
    """有透视校正时，标定长度须在校正坐标系下算。"""
    from app import transform as tf
    src = np.float32([[0, 0], [100, 0], [100, 80], [0, 80]])
    dst = np.float32([[0, 0], [200, 0], [200, 80], [0, 80]])   # x 放大 2 倍
    h = tf.build_homography(src, dst)
    r = calibrate.from_manual_line(0, 0, 50, 0, real_um=1000.0, h=h)
    assert r.pixel_length == pytest.approx(100.0)   # 50px → 100px
    assert r.um_per_px == pytest.approx(10.0)


def test_corrected_pixel_length_identity_when_no_h():
    assert calibrate.corrected_pixel_length(0, 0, 3, 4, None) == pytest.approx(5.0)


# ---------------- 非法输入 ----------------

def test_zero_length_rejected():
    with pytest.raises(ValueError):
        calibrate.from_manual_line(10, 10, 10, 10, real_um=3400.0)
    with pytest.raises(ValueError):
        calibrate.from_well_diameter(diameter_px=0.0)
    with pytest.raises(ValueError):
        calibrate.from_well_ellipse(major_px=0.0, minor_px=0.0)


def test_negative_real_length_rejected():
    with pytest.raises(ValueError):
        calibrate.from_well_diameter(diameter_px=100.0, real_um=-1.0)


def test_make_result_rejects_unknown_method():
    with pytest.raises(ValueError):
        calibrate.make_result("瞎写的", 100.0, 3400.0)


def test_make_result_dispatches_by_method_name():
    r = calibrate.make_result("well_diameter", 100.0, 3400.0)
    assert r.method == "well_diameter"
    assert r.um_per_px == pytest.approx(34.0)


# ---------------- 落库与重算 ----------------

def test_apply_calibration_persists(conn):
    iid = db.create_image(conn, "A", "a.jpg", "p.jpg", "drag")
    calibrate.apply_calibration(conn, iid,
                                calibrate.from_well_diameter(100.0))
    row = db.get_calibration(conn, iid)
    assert row["method"] == "well_diameter"
    assert row["um_per_px"] == pytest.approx(34.0)


def test_recompute_measurements_updates_values(conn):
    """★ 核心行为：重标定后，已有测量值必须按新系数自动更新。"""
    iid = db.create_image(conn, "A", "a.jpg", "p.jpg", "drag")
    # 先用 100px→3400um 标定（34 um/px），画一条 10px 的线
    calibrate.apply_calibration(conn, iid,
                                calibrate.from_well_diameter(100.0))
    db.add_measurement(conn, iid, 0, 0, 10, 0, 10.0, 340.0, 34.0, "晶体A")
    # 改用 200px→3400um 标定（17 um/px）
    calibrate.apply_calibration(conn, iid,
                                calibrate.from_well_diameter(200.0))
    n = calibrate.recompute_measurements(conn, iid)
    assert n == 1
    m = db.list_measurements(conn, iid)[0]
    assert m["measured_um"] == pytest.approx(170.0)      # 10px × 17
    assert m["calib_snapshot"] == pytest.approx(17.0)    # 快照也更新
    assert m["note"] == "晶体A"                           # 备注不受影响


def test_recompute_without_calibration_raises(conn):
    iid = db.create_image(conn, "A", "a.jpg", "p.jpg", "drag")
    with pytest.raises(ValueError):
        calibrate.recompute_measurements(conn, iid)


def test_recompute_returns_zero_when_no_measurements(conn):
    iid = db.create_image(conn, "A", "a.jpg", "p.jpg", "drag")
    calibrate.apply_calibration(conn, iid,
                                calibrate.from_well_diameter(100.0))
    assert calibrate.recompute_measurements(conn, iid) == 0


# ---------------- 已废弃路径的守卫 ----------------

def test_well_pitch_calibration_is_gone():
    """9mm 孔间距标定已废弃（用户只传单孔照片，画面里没有阵列）。"""
    assert "well_pitch" not in calibrate.CALIB_METHODS
    assert not hasattr(calibrate, "from_well_pitch")
