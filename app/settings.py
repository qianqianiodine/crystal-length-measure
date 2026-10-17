"""用户可改的设置。

出厂默认写在 `config.py`（那是厂商规格，是源码里的常量）；这里存的只是
**用户改过的值**，落盘到 `data/settings.json`。删掉那个文件 = 恢复出厂。

为什么用一个文件而不是数据库表：`data/` 本来就是用户产物、已经在 .gitignore 里，
而这个值在**建库之前**就可能要用到，塞进 schema 反而要多写一次迁移。

⚠️ 路径一律**现算**（写成 `_path()` 而不是模块级常量）：测试会 monkeypatch
`config.DATA_DIR`，模块级常量 import 那一刻就定型了、带不动，会去读写用户真实的
`data/`。这条和 `config.ensure_dirs()` 里那句警告是同一个坑。

⚠️ 写盘是**合并**写，不是整份覆盖：孔直径和画面风格各占一个键，各存各的时候
把对方抹掉的话，用户会遇到"改一个另一个就悄悄没了"。
"""
import json

from app import config


def _path():
    return config.DATA_DIR / "settings.json"


def _read() -> dict:
    """把设置文件读成 dict。读不出来（不在 / 坏了 / 顶层不是对象）一律当空。"""
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        # OSError=文件不在/读不了 · ValueError=JSON 坏了
        return {}
    return d if isinstance(d, dict) else {}


def _write(updates: dict) -> None:
    """把 `updates` 合进现有设置，落盘。"""
    d = _read()
    d.update(updates)
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再改名：避免半个文件留在那儿（下次读就是坏 JSON）。
    tmp = p.with_name(p.name + ".new")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def well_diameter_um() -> float:
    """点样孔直径（μm）。

    读不出来（文件不在 / 内容坏了 / 值不合理）一律回**出厂默认** ——
    一个设置文件坏掉不该让整个工具没法测量。
    """
    try:
        v = float(_read()["well_diameter_um"])
    except (KeyError, ValueError, TypeError):
        # KeyError=缺这个键 · TypeError=顶层不是对象 · 值不是数字也是 ValueError
        return config.WELL_DIAMETER_UM
    return v if v > 0 else config.WELL_DIAMETER_UM


def set_well_diameter_um(v: float) -> float:
    """改孔直径，落盘，返回存进去的值。

    ⚠️ 只写设置，**不回头改任何已有照片**。每张照片的换算系数
    （calibrations.um_per_px）在它定圆的那一刻就单独存下来了，所以改这里
    只影响"以后新定圆的照片"。想更新某张老照片，把那张图的圆重新拖一下就
    会按新直径重存、并自动重算它所有的线。
    """
    v = float(v)
    if not (v > 0):
        raise ValueError("孔直径必须大于 0")

    _write({"well_diameter_um": v})
    return v


# ---------------- 画面风格（图上字号 / 线条粗细） ----------------
# 测量页侧栏那两个滑块的值。**服务端也要知道**：图库的缩略图和「导出图片」
# 都是服务端画的（app/annotate.py），不共享这两个数，用户在测量页调完，
# 图库那边还是老的 16/2 —— 这就是用户 2026-10-17 报的「没同步」。
# 默认值必须和 web/measure.html 的 FONT_BASE / LINE_DEF 一字不差。
STYLE_FONT_DEFAULT = 16.0
STYLE_LINE_DEFAULT = 2.0
STYLE_FONT_MIN, STYLE_FONT_MAX = 10.0, 34.0
STYLE_LINE_MIN, STYLE_LINE_MAX = 1.0, 8.0

# 测量页画布（#stage）的尺寸，单位是页面 CSS 像素。
# 页面上「字号/线宽 = 照片的一部分」是按这个尺寸换算的（fitFullScale）：
# 同一张照片，窗口越大，16px 的字相对照片就越小。服务端知道这两个数，
# 导出的图和测量页直接导出才能是同一个比例。
STAGE_MIN_PX, STAGE_MAX_PX = 100.0, 20000.0


def _number(d: dict, key: str, default: float,
            lo: float, hi: float) -> float:
    try:
        v = float(d[key])
    except (KeyError, ValueError, TypeError):
        return default
    return min(hi, max(lo, v))


def _stage(d: dict, key: str) -> float | None:
    try:
        v = float(d[key])
    except (KeyError, ValueError, TypeError):
        return None
    return v if STAGE_MIN_PX <= v <= STAGE_MAX_PX else None


def draw_style() -> dict:
    """当前画面风格：字号、线宽、测量页画布尺寸。

    文件坏了/缺键/值超范围一律回默认 —— 图库和导出不该被一个坏设置卡住。
    `stage_w/stage_h` 为 None 时（用户还没打开过测量页）调用方按老的
    「画布短边 ≈ 620px」估算。
    """
    d = _read()
    return {
        "font_px": _number(d, "font_px", STYLE_FONT_DEFAULT,
                           STYLE_FONT_MIN, STYLE_FONT_MAX),
        "line_w": _number(d, "line_w", STYLE_LINE_DEFAULT,
                          STYLE_LINE_MIN, STYLE_LINE_MAX),
        "stage_w": _stage(d, "stage_w"),
        "stage_h": _stage(d, "stage_h"),
    }


def set_draw_style(*, font_px=None, line_w=None,
                   stage_w=None, stage_h=None) -> dict:
    """改画面风格，落盘，返回改完之后的整套值。

    只传一部分键 = 只改那一部分（图库页只知道字号/线宽，不知道画布尺寸）。
    """
    upd: dict = {}
    if font_px is not None:
        v = float(font_px)
        if not (STYLE_FONT_MIN <= v <= STYLE_FONT_MAX):
            raise ValueError(
                f"图上字号只能是 {STYLE_FONT_MIN:g} 到 {STYLE_FONT_MAX:g} 之间")
        upd["font_px"] = v
    if line_w is not None:
        v = float(line_w)
        if not (STYLE_LINE_MIN <= v <= STYLE_LINE_MAX):
            raise ValueError(
                f"线条粗细只能是 {STYLE_LINE_MIN:g} 到 {STYLE_LINE_MAX:g} 之间")
        upd["line_w"] = v
    if stage_w is not None:
        v = float(stage_w)
        if not (STAGE_MIN_PX <= v <= STAGE_MAX_PX):
            raise ValueError("画布宽度不合理")
        upd["stage_w"] = v
    if stage_h is not None:
        v = float(stage_h)
        if not (STAGE_MIN_PX <= v <= STAGE_MAX_PX):
            raise ValueError("画布高度不合理")
        upd["stage_h"] = v

    # 值没变就不写盘（图库每次打开都会把 localStorage 里的值推过来）。
    cur = _read()
    changed = {k: v for k, v in upd.items() if cur.get(k) != v}
    if changed:
        _write(changed)
    return draw_style()
