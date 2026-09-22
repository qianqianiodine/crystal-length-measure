"""暂存文件的存储层测试。

⚠️ 必须 monkeypatch config.STAGING_DIR —— 漏了就写进用户真实的 data/。

最要紧的一条是 test_discard_refuses_paths_outside_staging：
`Path("data") / "D:/.../样品集/图.jpg"` 会**返回右边那个绝对路径**
（pathlib 遇到绝对路径右操作数会整个丢弃左边）。不判断就删，删的是用户的原始素材。
"""
import cv2
import numpy as np
import pytest

from app import config, storage


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "data" / "images" / "originals")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "data" / "images" / "cache")
    monkeypatch.setattr(config, "THUMBS_DIR", tmp_path / "data" / "thumbs")
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "data" / "staging")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "app.db")
    config.ensure_dirs()
    return config


def _png(w=80, h=60, val=200) -> bytes:
    ok, buf = cv2.imencode(".png", np.full((h, w, 3), val, np.uint8))
    assert ok
    return buf.tobytes()


def test_ensure_dirs_creates_staging(dirs):
    assert dirs.STAGING_DIR.is_dir()
    assert (dirs.STAGING_DIR / "thumbs").is_dir()


def test_store_staged_writes_into_staging_and_returns_bare_name(dirs):
    name = storage.store_staged(_png())
    assert "/" not in name and "\\" not in name
    assert (dirs.STAGING_DIR / name).is_file()
    assert not list(dirs.IMAGES_DIR.glob("*"))       # 暂存**不能**碰正式库


def test_store_staged_extension_comes_from_content(dirs):
    assert storage.store_staged(_png()).endswith(".png")


def test_store_staged_leaves_nothing_behind_on_bad_input(dirs):
    with pytest.raises(ValueError):
        storage.store_staged(b"not an image")
    assert list(dirs.STAGING_DIR.glob("*.*")) == []


def test_resolve_staged(dirs):
    name = storage.store_staged(_png())
    assert storage.resolve_staged(name) == dirs.STAGING_DIR / name


def test_discard_staged_removes_the_file(dirs):
    name = storage.store_staged(_png())
    assert storage.discard_staged(name) is True
    assert not (dirs.STAGING_DIR / name).exists()


def test_discard_staged_is_ok_when_already_gone(dirs):
    assert storage.discard_staged("早就没了.png") is False


def test_discard_refuses_paths_outside_staging(dirs, tmp_path):
    """⭐ 绝对路径右操作数会丢弃左边 —— 不判断就会删掉用户的原始素材。"""
    outside = tmp_path / "样品集"
    outside.mkdir()
    precious = outside / "样品2_1.jpg"
    precious.write_bytes(b"x")

    assert storage.discard_staged(str(precious)) is False
    assert precious.is_file()


def test_store_original_still_works(dirs):
    """回归：抽出 _store 之后，正式入库那条路一个字都不能变。"""
    name = storage.store_original(_png())
    assert (dirs.IMAGES_DIR / name).is_file()
    assert list(dirs.STAGING_DIR.glob("*.*")) == []
