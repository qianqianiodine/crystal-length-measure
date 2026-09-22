"""骨架测试：目录常量、物理常量、应用可导入。"""
from pathlib import Path


def test_config_paths_are_absolute():
    from app import config
    assert isinstance(config.PROJECT_ROOT, Path)
    assert config.PROJECT_ROOT.is_absolute()
    assert config.DB_PATH.name == "app.db"


def test_physical_constants():
    from app import config
    assert config.WELL_DIAMETER_UM == 3400.0
    assert config.WELL_PITCH_UM == 9000.0


def test_ensure_dirs_creates_data_tree(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "data" / "images" / "originals")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "data" / "images" / "cache")
    monkeypatch.setattr(config, "THUMBS_DIR", tmp_path / "data" / "thumbs")
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "data" / "staging")
    config.ensure_dirs()
    assert config.IMAGES_DIR.is_dir()
    assert config.CACHE_DIR.is_dir()
    assert config.THUMBS_DIR.is_dir()


def test_app_importable():
    from app.main import app
    assert app.title
