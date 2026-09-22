"""坐标变换链测试 —— 项目正确性的根基。

重点验证：
1. 正反变换严格互逆（这是测量数值正确的前提）
2. 透视校正后长度按校正坐标计算
3. 退化为恒等变换时行为正确
"""
import numpy as np
import pytest

from app import transform as tf


def test_line_length_3_4_5():
    assert tf.line_length(0, 0, 3, 4) == pytest.approx(5.0)


def test_px_to_um():
    assert tf.px_to_um(100.0, 34.0) == pytest.approx(3400.0)


def test_build_homography_identity():
    """四点映射到自身应得到单位矩阵。"""
    pts = np.float32([[0, 0], [100, 0], [100, 80], [0, 80]])
    H = tf.build_homography(pts, pts)
    assert H.shape == (3, 3)
    assert np.allclose(H / H[2, 2], np.eye(3), atol=1e-6)


def test_build_homography_scale():
    """四点映射到放大 2 倍的目标，应得到缩放 2 倍的变换。"""
    src = np.float32([[0, 0], [100, 0], [100, 80], [0, 80]])
    dst = np.float32([[0, 0], [200, 0], [200, 160], [0, 160]])
    H = tf.build_homography(src, dst)
    x, y = tf.apply_h(H, 50, 40)
    assert (x, y) == pytest.approx((100.0, 80.0))


def test_build_homography_rejects_wrong_point_count():
    with pytest.raises(ValueError):
        tf.build_homography(np.float32([[0, 0], [1, 1]]),
                            np.float32([[0, 0], [1, 1]]))


def test_build_homography_rejects_degenerate():
    """四点共线无法求解，应抛错而不是返回垃圾矩阵。"""
    src = np.float32([[0, 0], [10, 0], [20, 0], [30, 0]])
    dst = np.float32([[0, 0], [10, 10], [20, 20], [30, 30]])
    with pytest.raises(ValueError):
        tf.build_homography(src, dst)


def test_invert_h_roundtrip():
    src = np.float32([[0, 0], [100, 0], [100, 80], [0, 80]])
    dst = np.float32([[10, 5], [190, 12], [180, 170], [15, 160]])
    H = tf.build_homography(src, dst)
    Hi = tf.invert_h(H)
    for x, y in [(0, 0), (50, 40), (100, 80), (23.5, 61.25)]:
        mx, my = tf.apply_h(H, x, y)
        bx, by = tf.apply_h_inv(Hi, mx, my)
        assert (bx, by) == pytest.approx((x, y), abs=1e-6)


def test_display_transform_identity():
    """scale=1, 无平移、无校正 → 恒等。"""
    dt = tf.DisplayTransform(scale=1.0, tx=0.0, ty=0.0, H=None)
    assert dt.original_to_display(10, 20) == pytest.approx((10.0, 20.0))
    assert dt.display_to_original(10, 20) == pytest.approx((10.0, 20.0))


def test_display_transform_scale_and_translate():
    dt = tf.DisplayTransform(scale=2.0, tx=100.0, ty=50.0, H=None)
    assert dt.original_to_display(10, 20) == pytest.approx((120.0, 90.0))
    assert dt.display_to_original(120, 90) == pytest.approx((10.0, 20.0))


def test_display_transform_with_homography_roundtrip():
    """带透视校正时，正反变换仍须互逆。"""
    src = np.float32([[0, 0], [100, 0], [100, 80], [0, 80]])
    dst = np.float32([[5, 3], [110, 8], [105, 95], [12, 88]])
    H = tf.build_homography(src, dst)
    dt = tf.DisplayTransform(scale=1.7, tx=33.0, ty=-12.0, H=H)
    for x, y in [(0, 0), (50, 40), (99, 79), (12.3, 45.6)]:
        dx, dy = dt.original_to_display(x, y)
        bx, by = dt.display_to_original(dx, dy)
        assert (bx, by) == pytest.approx((x, y), abs=1e-6)


def test_measurement_um_without_correction():
    """无校正：直接按原图坐标算长度。"""
    px, um = tf.measurement_um(0, 0, 3, 4, um_per_px=10.0, H=None)
    assert px == pytest.approx(5.0)
    assert um == pytest.approx(50.0)


def test_measurement_um_with_correction():
    """有校正：长度须按校正后的坐标算。"""
    # 水平方向拉长 2 倍
    src = np.float32([[0, 0], [100, 0], [100, 80], [0, 80]])
    dst = np.float32([[0, 0], [200, 0], [200, 80], [0, 80]])
    H = tf.build_homography(src, dst)
    px, um = tf.measurement_um(0, 0, 50, 0, um_per_px=1.0, H=H)
    assert px == pytest.approx(100.0)   # 50px 在校正空间变成 100px
    assert um == pytest.approx(100.0)


def test_ellipse_axes_roundup():
    """孔被拍成椭圆、做校正后，长短轴应趋近相等。"""
    # 构造一个把 x 压缩 0.5 的变换，逆变换后椭圆应变圆
    src = np.float32([[0, 0], [100, 0], [100, 80], [0, 80]])
    dst = np.float32([[0, 0], [50, 0], [50, 80], [0, 80]])
    H_correction = tf.build_homography(dst, src)   # 把压缩的还原回去
    a, b = tf.ellipse_axes_from_H(H_correction, axes_px=(40.0, 80.0), angle_deg=0.0)
    assert a == pytest.approx(80.0, rel=0.02)
    assert b == pytest.approx(80.0, rel=0.02)
