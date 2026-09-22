"""孔检测测试。

场景是**单孔照片**（用户的实际用法）：画面里一个孔，没有阵列、没有参照物。
检测只做粗定位，精度要求不高 —— 起点圆由用户手动调整定案。
"""
import cv2
import numpy as np
import pytest

from app import config, detect


def _draw_well(img, cx, cy, r):
    """画一个孔：暗的孔内 + 亮的孔壁。"""
    cv2.circle(img, (int(cx), int(cy)), int(r), (95, 95, 95), -1)
    cv2.circle(img, (int(cx), int(cy)), int(r), (230, 230, 230), 4)
    return img


def _photo(cx=200, cy=200, r=60, size=(400, 400)):
    img = np.full((size[0], size[1], 3), 150, np.uint8)
    _draw_well(img, cx, cy, r)
    return cv2.GaussianBlur(img, (0, 0), 1.5)


def test_detect_single_well_center_and_radius():
    cands = detect.detect_wells(_photo(cx=200, cy=190, r=60))
    assert len(cands) >= 1
    c = cands[0]
    assert c.center[0] == pytest.approx(200, abs=8)
    assert c.center[1] == pytest.approx(190, abs=8)
    assert c.diameter_px == pytest.approx(120, rel=0.12)


def test_detect_well_filling_the_frame():
    """单孔照片里孔通常占画面很大一块，也要能检出来。"""
    cands = detect.detect_wells(_photo(cx=200, cy=200, r=120, size=(420, 420)))
    assert len(cands) >= 1
    assert cands[0].diameter_px == pytest.approx(240, rel=0.15)


def test_detect_returns_sorted_and_limited():
    img = _photo(cx=150, cy=150, r=60, size=(600, 600))
    _draw_well(img, 450, 450, 70)
    img = cv2.GaussianBlur(img, (0, 0), 1.5)
    cands = detect.detect_wells(img, max_candidates=2)
    assert len(cands) == 2
    scores = [c.score for c in cands]
    assert scores == sorted(scores, reverse=True)


def test_detect_no_wells_returns_empty():
    flat = np.full((300, 300, 3), 128, np.uint8)
    assert detect.detect_wells(flat) == []


def test_default_diameter_px_picks_top_candidate():
    cands = [detect.WellCandidate(center=(10, 10), radius_px=52.0, score=0.9),
             detect.WellCandidate(center=(99, 99), radius_px=30.0, score=0.1)]
    assert detect.default_diameter_px(cands) == pytest.approx(104.0)
    with pytest.raises(ValueError):
        detect.default_diameter_px([])


def test_crop_field_circle_removes_dark_border():
    img = np.zeros((400, 400, 3), np.uint8)
    cv2.circle(img, (200, 200), 150, (180, 180, 180), -1)
    out, info = detect.crop_field_circle(img)
    assert out.shape[0] < 400 and out.shape[1] < 400
    assert info is not None
    assert info[2] == pytest.approx(150, rel=0.12)


def test_crop_field_circle_noop_on_full_image():
    img = np.full((300, 300, 3), 180, np.uint8)
    out, info = detect.crop_field_circle(img)
    assert out.shape == img.shape


def test_real_single_well_photo_detects_a_well():
    """真实样品图冒烟：样品2_1.jpg 是典型的单孔照片。"""
    from app import imageio
    p = config.PROJECT_ROOT / "样品集" / "样品2_1.jpg"
    if not p.exists():
        pytest.skip("样品集不存在")
    img = imageio.load_rgb(p)
    cands = detect.detect_wells(img)
    assert len(cands) >= 1, "单孔照片上应能给出至少一个起点圆"
    short = min(img.shape[:2])
    assert 0.10 * short <= cands[0].radius_px <= 0.50 * short, cands[0]
