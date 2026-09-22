"""中文路径图像读写测试。

项目所有路径含中文，cv2.imread 在 Windows 下会静默失败，
因此这里覆盖中文路径、非图像文件、不存在的文件三种情况。
"""
import numpy as np
import pytest

from app import imageio


def test_roundtrip_chinese_path(tmp_path):
    """中文路径写入后能读回，像素值一致。"""
    p = tmp_path / "中文目录" / "样品_测试.png"
    p.parent.mkdir(parents=True)
    img = np.random.default_rng(42).integers(0, 256, (30, 40, 3), dtype=np.uint8)
    imageio.save_rgb(p, img)
    got = imageio.load_rgb(p)
    assert got.shape == (30, 40, 3)
    assert np.array_equal(got, img)


def test_load_rgb_returns_three_channels(tmp_path):
    """灰度源图也必须返回 3 通道。"""
    import cv2
    p = tmp_path / "灰度.png"
    gray = np.full((20, 20), 128, np.uint8)
    imageio.imwrite_unicode(p, gray)
    got = imageio.load_rgb(p)
    assert got.ndim == 3 and got.shape[2] == 3


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(imageio.ImageLoadError):
        imageio.load_rgb(tmp_path / "不存在.jpg")


def test_load_non_image_raises(tmp_path):
    """非图像文件不能静默返回 None。"""
    p = tmp_path / "假图.jpg"
    p.write_text("这不是图片", encoding="utf-8")
    with pytest.raises(imageio.ImageLoadError):
        imageio.load_rgb(p)


def test_imread_unicode_returns_none_for_bad_file(tmp_path):
    """底层函数保持宽松：坏文件返回 None 而不抛。"""
    p = tmp_path / "坏.bin"
    p.write_bytes(b"\x00\x01\x02")
    assert imageio.imread_unicode(p) is None


def test_load_real_sample_image():
    """用样品集真实图片验证（含中文路径）。"""
    from app import config
    p = config.PROJECT_ROOT / "样品集" / "样品1_全局.jpg"
    if not p.exists():
        pytest.skip("样品集不存在")
    img = imageio.load_rgb(p)
    assert img.shape[0] > 100 and img.shape[1] > 100
    assert img.ndim == 3 and img.shape[2] == 3


def test_save_rgb_creates_parent_dirs(tmp_path):
    """父目录不存在时应自动创建。"""
    p = tmp_path / "自动" / "创建" / "目录" / "图.png"
    imageio.save_rgb(p, np.zeros((10, 10, 3), np.uint8))
    assert p.exists()
