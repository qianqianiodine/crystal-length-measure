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

from app import config, main, settings, thumbs


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


# ---------------- 画面风格（字号 / 线宽 / 画布） ----------------

def test_style_defaults_match_the_measure_page():
    """★ 默认值必须和 web/measure.html 的 FONT_BASE / LINE_DEF 一样。

    不一样的话，用户一次滑块都没动过，图库缩略图和批量导出也会和测量页
    对不上 —— 2026-10-17 用户报的就是这个（服务端还停在 13/3）。
    """
    st = settings.draw_style()
    assert st["font_px"] == pytest.approx(16)
    assert st["line_w"] == pytest.approx(2)
    assert st["stage_w"] is None and st["stage_h"] is None


def test_style_round_trip():
    st = settings.set_draw_style(font_px=24, line_w=3.5,
                                 stage_w=1200, stage_h=800)
    assert st["font_px"] == pytest.approx(24)
    assert st["line_w"] == pytest.approx(3.5)
    assert st["stage_w"] == pytest.approx(1200)
    assert st["stage_h"] == pytest.approx(800)
    assert settings.draw_style() == pytest.approx(st)


def test_partial_update_keeps_diameter_and_other_style_keys():
    """图库只知道字号/线宽，不知道画布尺寸 —— 部分更新不能把别的键抹掉。
    孔直径和画面风格在同一个文件里，更要互相不打架。"""
    settings.set_well_diameter_um(2500.0)
    settings.set_draw_style(font_px=20, line_w=3, stage_w=1000, stage_h=700)
    settings.set_draw_style(font_px=22)             # 只改字号
    assert settings.well_diameter_um() == pytest.approx(2500.0)
    st = settings.draw_style()
    assert st["font_px"] == pytest.approx(22)
    assert st["line_w"] == pytest.approx(3)
    assert st["stage_w"] == pytest.approx(1000)


@pytest.mark.parametrize("kw", [
    {"font_px": 9}, {"font_px": 35},
    {"line_w": 0.5}, {"line_w": 9},
    {"stage_w": 0}, {"stage_h": 999999},
])
def test_style_rejects_absurd_values(kw):
    with pytest.raises(ValueError):
        settings.set_draw_style(**kw)


def test_broken_style_values_fall_back():
    """文件里是垃圾值时：读得出来的夹到范围里，读不出来的回默认。"""
    _write_settings('{"font_px": "abc", "line_w": 999, "stage_w": "x"}')
    st = settings.draw_style()
    assert st["font_px"] == pytest.approx(16)      # 不是数字 → 默认
    assert st["line_w"] == pytest.approx(8)        # 超范围 → 夹到上限
    assert st["stage_w"] is None                   # 不是数字 → 当没有


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


# ---------------- /api/settings/style ----------------

def test_put_style_persists(client):
    r = client.put("/api/settings/style", json={"font_px": 24, "line_w": 3})
    assert r.status_code == 200, r.text
    assert r.json()["font_px"] == pytest.approx(24)
    assert r.json()["line_w"] == pytest.approx(3)
    assert settings.draw_style()["font_px"] == pytest.approx(24)     # 真落盘了
    assert client.get("/api/settings/style").json()["line_w"] \
        == pytest.approx(3)


@pytest.mark.parametrize("body", [
    {"font_px": 9}, {"font_px": 35}, {"line_w": 0.5}, {"line_w": 9},
])
def test_put_style_rejects_absurd_values(client, body):
    r = client.put("/api/settings/style", json=body)
    assert r.status_code == 400
    assert "只能" in r.json()["detail"]


def test_style_change_drops_only_calibrated_thumbnails(client):
    """★ 字号/线宽变了，**标定过**的缩略图必须重画（上面画着线和字）。

    没标定的缩略图是光图，不该跟着被删 —— 删了下次打开要重新解码原图，
    几十张照片会让图库卡半天。
    """
    iid = _upload_one(client)
    client.put(f"/api/image/{iid}/circle",
               json={"cx": 40, "cy": 30, "a": 50, "b": 50, "th": 0})
    assert client.get(f"/api/images/{iid}/thumb").status_code == 200
    assert thumbs.path_for(iid).exists()

    plain = _upload_one(client)                       # 没标定
    assert client.get(f"/api/images/{plain}/thumb").status_code == 200
    assert thumbs.path_for(plain).exists()

    client.put("/api/settings/style", json={"font_px": 20})
    assert not thumbs.path_for(iid).exists(), "标定过的缩略图没作废，样式还是旧的"
    assert thumbs.path_for(plain).exists(), "没标定的光图不该被删"


def test_stage_size_change_does_not_drop_thumbnails(client):
    """拉伸窗口不该让整面格子墙重画 —— 缩略图就 320px，那点比例差看不出。"""
    iid = _upload_one(client)
    client.put(f"/api/image/{iid}/circle",
               json={"cx": 40, "cy": 30, "a": 50, "b": 50, "th": 0})
    client.get(f"/api/images/{iid}/thumb")
    assert thumbs.path_for(iid).exists()

    client.put("/api/settings/style", json={"stage_w": 1200, "stage_h": 800})
    assert thumbs.path_for(iid).exists()
