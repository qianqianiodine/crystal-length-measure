"""用户可改的设置（`app/settings.py` 与 `/api/settings`）测试。

别人手上的坐滴板孔不一定是 Φ3.4mm，所以孔直径做成了可改的。

⭐ 最要紧的一条：**改完直径，接着定圆的照片必须按新值换算。**
   钉的是一个 Python 陷阱 —— 详见 tests/test_calibrate.py 里那条回归用例。

⚠️ 必须 monkeypatch config 里的目录，否则会往用户真实的 data/ 里读写东西。
"""
import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import config, main, settings


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """把数据目录全挪进 tmp_path（形状抄自 tests/test_api_tags.py:17-27）。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "data" / "images" / "originals")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "data" / "images" / "cache")
    monkeypatch.setattr(config, "THUMBS_DIR", tmp_path / "data" / "thumbs")
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "data" / "staging")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "app.db")


@pytest.fixture()
def client(_isolate):
    # 显式依赖 _isolate：保证目录先被换掉，再 ensure_dirs()
    config.ensure_dirs()
    with TestClient(main.app) as c:
        yield c


# ---------------- app/settings.py ----------------

def test_default_is_the_factory_value():
    assert settings.well_diameter_um() == pytest.approx(3400.0)
    assert settings.well_diameter_um() == pytest.approx(config.WELL_DIAMETER_UM)


def test_round_trip():
    settings.set_well_diameter_um(2500.0)
    assert settings.well_diameter_um() == pytest.approx(2500.0)


def test_set_returns_what_it_stored():
    assert settings.set_well_diameter_um(1250.0) == pytest.approx(1250.0)


def test_no_half_written_file_left_behind():
    """先写临时文件再改名 —— 半路崩了也不该留下半个坏 JSON。"""
    settings.set_well_diameter_um(2500.0)
    assert not (config.DATA_DIR / "settings.json.new").exists()
    assert settings.well_diameter_um() == pytest.approx(2500.0)


def _write_settings(text: str) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / "settings.json").write_text(text, encoding="utf-8")


@pytest.mark.parametrize("broken", [
    "{ 这不是 JSON",            # JSON 语法坏了
    "[1, 2, 3]",                # 顶层不是对象
    '{"别的键": 1}',             # 缺这个键
    '{"well_diameter_um": "abc"}',   # 值不是数字
    '{"well_diameter_um": 0}',       # 不合理的值
    '{"well_diameter_um": -5}',
])
def test_broken_file_falls_back_to_factory(broken):
    """设置文件坏掉不该让整个工具没法测量 —— 一律回出厂默认。"""
    _write_settings(broken)
    assert settings.well_diameter_um() == pytest.approx(config.WELL_DIAMETER_UM)


def test_rejects_non_positive():
    with pytest.raises(ValueError):
        settings.set_well_diameter_um(0)
    with pytest.raises(ValueError):
        settings.set_well_diameter_um(-100)


# ---------------- /api/settings ----------------

def _upload_one(client) -> int:
    ok, buf = cv2.imencode(".png", np.full((60, 80, 3), 200, np.uint8))
    assert ok
    r = client.post("/api/images/upload",
                    files=[("files", ("照片.png", buf.tobytes(), "image/png"))])
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["failed"] == []
    return j["ok"][0]["id"]


def test_get_reports_both_current_and_factory(client):
    """界面上的「恢复默认」按钮靠 factory_um，不必在网页里再写一份 3400。"""
    j = client.get("/api/settings").json()
    assert j["well_diameter_um"] == pytest.approx(3400.0)
    assert j["factory_um"] == pytest.approx(3400.0)


def test_put_persists(client):
    r = client.put("/api/settings", json={"well_diameter_um": 2500.0})
    assert r.status_code == 200, r.text
    assert r.json()["well_diameter_um"] == pytest.approx(2500.0)
    assert settings.well_diameter_um() == pytest.approx(2500.0)          # 真落盘了
    assert client.get("/api/settings").json()["well_diameter_um"] \
        == pytest.approx(2500.0)


@pytest.mark.parametrize("bad", [0, -1, 50, 200_000])
def test_put_rejects_absurd_values(client, bad):
    """差 1000 倍那种错（把毫米当微米填了）自己不会报错，只会让量出来的
    每一个数静静地偏 1000 倍 —— 必须在入口拦住。"""
    r = client.put("/api/settings", json={"well_diameter_um": bad})
    assert r.status_code == 400
    assert "毫米" in r.json()["detail"]


def test_rejected_value_does_not_disturb_the_stored_one(client):
    client.put("/api/settings", json={"well_diameter_um": 2500.0})
    client.put("/api/settings", json={"well_diameter_um": 0})
    assert settings.well_diameter_um() == pytest.approx(2500.0)


def test_new_circle_uses_the_new_diameter(client):
    """★ 用户的条件：改完直径，接着定的圆必须按新值换算。

    长轴 100px 的孔在 2500μm 下 = 25 μm/px；要是还按 3400 算就是 34。
    """
    iid = _upload_one(client)
    client.put("/api/settings", json={"well_diameter_um": 2500.0})

    r = client.put(f"/api/image/{iid}/circle",
                   json={"cx": 40, "cy": 30, "a": 50, "b": 50, "th": 0})
    assert r.status_code == 200, r.text
    assert r.json()["um_per_px"] == pytest.approx(25.0)

    assert client.get(f"/api/image/{iid}").json()["um_per_px"] \
        == pytest.approx(25.0)


def test_changing_diameter_does_not_touch_existing_photos(client):
    """改设置**不回头改老照片** —— 想更新某张，得把那张图的圆重新拖一下。"""
    iid = _upload_one(client)
    client.put(f"/api/image/{iid}/circle",
               json={"cx": 40, "cy": 30, "a": 50, "b": 50, "th": 0})
    assert client.get(f"/api/image/{iid}").json()["um_per_px"] \
        == pytest.approx(34.0)

    client.put("/api/settings", json={"well_diameter_um": 2500.0})
    assert client.get(f"/api/image/{iid}").json()["um_per_px"] \
        == pytest.approx(34.0)          # 老照片纹丝不动

    # 重新拖一下圆（同一条拖拽路径会带上新直径）→ 才变成 25
    r = client.put(f"/api/image/{iid}/circle",
                   json={"cx": 40, "cy": 30, "a": 50, "b": 50, "th": 0})
    assert r.json()["um_per_px"] == pytest.approx(25.0)


def test_recalibrating_updates_existing_measurements(client):
    """重新定圆后，已经量过的线自动按新系数重算 —— 不用重画。"""
    iid = _upload_one(client)
    client.put(f"/api/image/{iid}/circle",
               json={"cx": 40, "cy": 30, "a": 50, "b": 50, "th": 0})
    client.post(f"/api/image/{iid}/lines",
                json={"x1": 10, "y1": 10, "x2": 30, "y2": 10, "note": "晶体A"})
    assert client.get(f"/api/image/{iid}").json()["lines"][0]["measured_um"] \
        == pytest.approx(680.0)          # 20px × 34

    client.put("/api/settings", json={"well_diameter_um": 2500.0})
    r = client.put(f"/api/image/{iid}/circle",
                   json={"cx": 40, "cy": 30, "a": 50, "b": 50, "th": 0})

    assert r.json()["um_per_px"] == pytest.approx(25.0)
    line = r.json()["lines"][0]
    assert line["measured_um"] == pytest.approx(500.0)    # 20px × 25，重算过
    assert line["note"] == "晶体A"                          # 备注没被动
