"""标注绘制：测量线、长度数值、自适应标尺、裁剪。

设计依据（见设计文档 §5.6.2）：
用户拍摄时为拍到孔牺牲了构图，导出时需要能裁剪聚焦到晶体，
**但标尺必须保留** —— 论文图没有标尺就不可信。

标尺的正确做法：**物理长度固定、像素长自适应**。
标尺代表一个确定的物理量（如 200 μm），它在图上占多少像素
取决于该图的标定系数。若反过来固定像素长，不同图之间的
标尺就不可比，失去科学意义。

⚠️ 裁剪与绘制都**不得改变任何测量数值** —— 纯显示层操作。
"""
from pathlib import Path

import cv2
import numpy as np

# 可选的标尺物理长度（μm）。都是人能一眼读懂的整数。
SCALE_BAR_STEPS_UM = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000]

# 标尺目标像素宽度占裁剪宽度的比例区间
BAR_MIN_RATIO = 0.10
BAR_MAX_RATIO = 0.25
# 实际用的目标比例。⚠️ 试用页的实时比例尺用的是同一个数
# （web/measure.html 的 BAR_TARGET_RATIO）—— 两边不一致的话，
# 屏幕上看到的是「2 mm」、导出图里变成「500 μm」，用户会以为导出错了。
BAR_TARGET_RATIO = (BAR_MIN_RATIO + BAR_MAX_RATIO) / 2.0

# 输出图上限（像素）。4 倍放大一张 4000×3000 的照片 = 1.92 亿像素 ≈ 576 MB，
# 直接把内存吃满 —— 宁可报个清楚的中文错，也别让工具卡死。
MAX_OUTPUT_PIXELS = 40_000_000

# ============ 导出图的绘制尺寸，全部从页面推出来 ============
# 用户的要求是「导出的画面跟我框选的页面一模一样」。做法不是各调各的，
# 而是：试用页的画布短边大约就这么宽，页面上那些写死的像素数
# （线宽 3、端点半径 4.5、字号 13、标尺白线 2.5、刻度 7、标签抬升 20）
# 就是按这个尺度定的。导出图按 min(高,宽)/620 这个系数等比放大，
# 两边就自然一致 —— 而不是像原来那样各算各的（导出的标尺就成了哑铃）。
CANVAS_MIN_SIDE = 620.0

# 页面上的这些数，单位是「页面的屏幕像素」。改这里必须同时改 web/measure.html
PAGE_LINE_W = 3.0          # halo(path, color, 3)
PAGE_HALO_EXTRA = 3.5      # halo() 里深色描边比本色宽多少
PAGE_ENDPOINT_R = 4.5      # dot(p, 4.5, ...)
PAGE_ENDPOINT_RING = 2.5
PAGE_FONT = 13.0           # '600 13px' --num
PAGE_LABEL_LIFT = 20.0     # labelScreen() 里减掉的那 20px
PAGE_BAR_W = 2.5           # drawScaleBar() 的白色线宽
PAGE_BAR_TICK = 7.0        # 两端小刻度
PAGE_BAR_LABEL_GAP = 11.0  # 标尺文字离横线多远（页面 strokeText(t, x2, y - 11)）


def style_for(h: int, w: int) -> dict:
    """算出这张输出图上该用多粗的线、多大的字。

    返回的一切都按「页面画布短边 = 620px」的尺度等比放大，
    所以在 620px 的图上得到的就是页面上的原始数字。
    """
    d = min(h, w) / CANVAS_MIN_SIDE
    line_w = max(2, int(round(PAGE_LINE_W * d)))
    bar_w = max(2, int(round(PAGE_BAR_W * d)))
    return {
        "d": d,
        "line_w": line_w,
        # 描边宽度用的是页面的 halo() 规则：本色 + 3.5
        "line_halo": max(line_w + 1, int(round((PAGE_LINE_W + PAGE_HALO_EXTRA) * d))),
        # 页面 dot(p, 4.5, 线的颜色, 2.5)：填一个半径 4.5 的**本色**圆，
        # 再用 2.5 宽的深色描一圈。canvas 的 stroke 是沿路径居中的，
        # 所以实际看到的是「本色半径 3.25，深色外圈到 5.75」。
        "endpoint_r": max(3, int(round((PAGE_ENDPOINT_R - PAGE_ENDPOINT_RING / 2) * d))),
        "endpoint_ring": max(4, int(round((PAGE_ENDPOINT_R + PAGE_ENDPOINT_RING / 2) * d))),
        "text_size": max(11, int(round(PAGE_FONT * d))),
        "lift": int(round(PAGE_LABEL_LIFT * d)),
        "bar_w": bar_w,
        "bar_halo": max(bar_w + 1, int(round((PAGE_BAR_W + PAGE_HALO_EXTRA) * d))),
        "bar_tick": max(4, int(round(PAGE_BAR_TICK * d))),
    }

# 文字用 PIL 画，不用 cv2.putText。两个原因：
#   ① cv2 的 Hershey 字体和试用页的 Consolas 完全不是一个样子，
#      导出图和屏幕上看着像两个工具（用户指出过）；
#   ② 中文只有 PIL 能画。
# 数字/标尺用等宽字体（和页面的 --num 一致），含中文的备注换成中文字体。
_MONO_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\consolab.ttf",  # Consolas 粗体 —— 页面用的是 600 字重
    r"C:\Windows\Fonts\consola.ttf",
]
_CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
]

# 描边打底色。⚠️ 和试用页的 --ink(#0b0a08) 是同一个颜色 ——
# 用纯黑会在导出图上看出和页面不一样的调子。
INK_RGB = (11, 10, 8)
WHITE_RGB = (255, 255, 255)


def choose_scale_bar_um(um_per_px: float, target_pixel_width: float) -> float:
    """选一个「好看」的标尺物理长度。

    在所有候选中选出像素宽度最接近 target_pixel_width 的那个。
    判据用**对数距离**而不是绝对差：标尺是人眼按比例读的，
    500 和 1000 之间隔着"一倍"，而不是"500 像素"。
    （试用页的实时比例尺用的也是这条规则，两处必须一致，
    否则屏幕上看到的和导出图上的不是同一根尺子。）
    """
    if um_per_px <= 0:
        raise ValueError("标定系数必须大于 0")
    if target_pixel_width <= 0:
        raise ValueError("目标像素宽度必须大于 0")

    target_um = target_pixel_width * um_per_px
    best, best_err = SCALE_BAR_STEPS_UM[0], float("inf")
    for step in SCALE_BAR_STEPS_UM:
        err = abs(np.log(step / target_um))
        if err < best_err:
            best_err, best = err, step
    return float(best)


def format_scale_label(bar_um: float) -> str:
    """把标尺物理长度格式化成人读的标签。"""
    if bar_um < 1000:
        return f"{bar_um:.0f} μm"
    if bar_um < 10000:
        return f"{bar_um / 1000:g} mm"
    return f"{bar_um / 10000:g} cm"


def format_length_label(measured_um: float) -> str:
    """测量长度写成什么。**必须和试用页的 fmtLen() 一模一样** ——
    用户是看着屏幕上的数字把标签拖开的，导出图上的写法变了会认不出来。
    """
    if measured_um < 1000:
        return f"{measured_um:.1f} μm"
    return f"{measured_um / 1000:.2f} mm"


def focus_crop_for_line(x1: float, y1: float, x2: float, y2: float,
                        img_size: tuple[int, int],
                        margin_ratio: float = 0.35
                        ) -> tuple[int, int, int, int]:
    """以一条测量线为中心生成裁剪框。

    这是「一键找回构图」的实现：用户拍孔时被迫放宽取景，
    导出时点一下就把框套到目标晶体周围。

    Args:
        img_size: (height, width)
    Returns:
        (x0, y0, x1, y1)，已夹在图像范围内
    """
    h, w = img_size
    xs, ys = (x1, x2), (y1, y2)
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    margin = max(span * margin_ratio, span * 0.5)
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    half = span / 2.0 + margin
    x0 = int(max(0, min(w - 2, cx - half)))
    y0 = int(max(0, min(h - 2, cy - half)))
    x1i = int(min(w, max(2, cx + half)))
    y1i = int(min(h, max(2, cy + half)))
    return x0, y0, x1i, y1i


def resolve_crop(crop, img_size: tuple[int, int],
                 lines: list[dict]) -> tuple[int, int, int, int]:
    """把前端传来的裁剪请求解析成像素框 (x0, y0, x1, y1)。

    crop 取值：
      - None            → 全图
      - {"mode":"focus", "seq": N}  → 聚焦第 N 号测量线
      - {"mode":"rect", "rect":[x0,y0,x1,y1]} → 手动框
    """
    h, w = img_size
    if crop is None:
        return 0, 0, w, h

    if isinstance(crop, (tuple, list)):        # 直接给 (x0,y0,x1,y1) 像素框
        crop = {"mode": "rect", "rect": list(crop)}

    mode = crop.get("mode")
    if mode == "focus":
        seq = crop.get("seq")
        target = next((l for l in lines if l.get("seq") == seq), None)
        if target is None:
            return 0, 0, w, h
        return focus_crop_for_line(target["x1"], target["y1"],
                                   target["x2"], target["y2"], img_size)

    if mode == "rect":
        r = crop.get("rect") or [0, 0, w, h]
        x0, y0, x1, y1 = (int(v) for v in r)
        x0 = max(0, min(w - 2, x0))
        y0 = max(0, min(h - 2, y0))
        x1 = max(x0 + 1, min(w, x1))
        y1 = max(y0 + 1, min(h, y1))
        return x0, y0, x1, y1

    return 0, 0, w, h


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    if len(c) != 6:
        return (255, 45, 85)
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _first_existing(paths: list[str]) -> str | None:
    return next((p for p in paths if Path(p).exists()), None)


def _font_for(text: str) -> str | None:
    """含中文就用中文字体，否则用等宽字体（和页面上的 Consolas 一致）。"""
    if any(ord(c) > 127 for c in text):
        return _first_existing(_CJK_FONT_CANDIDATES) or _first_existing(_MONO_FONT_CANDIDATES)
    return _first_existing(_MONO_FONT_CANDIDATES) or _first_existing(_CJK_FONT_CANDIDATES)


def _render_texts(img: np.ndarray, items: list) -> np.ndarray:
    """一次性把所有文字画上去。

    items: [(文字, x, y, rgb, anchor, 字号), ...]
    anchor 用 PIL 的写法："mm"=正中（对应 canvas 的 textAlign/middle），
    "mb"=底边中点，"ls"=左下。

    为什么要成批：numpy ↔ PIL 要整图搬一次，一条文字搬一次的话
    出张 8000px 的图会慢到能感觉出来。顺序上文字必须最后画（压在最上层）。
    """
    if not items:
        return img
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return img          # 没装 Pillow 就不画文字，不抛错
    pil = Image.fromarray(img)
    draw = ImageDraw.Draw(pil)
    cache: dict[tuple[str, int], object] = {}
    for text, x, y, rgb, anchor, size in items:
        path = _font_for(text)
        if path is None:
            continue
        key = (path, size)
        if key not in cache:
            try:
                cache[key] = ImageFont.truetype(path, size)
            except OSError:
                continue
        # 和画布上其他图形一样描一圈深色边 —— 亮液滴上白字会糊成一片
        draw.text((x, y), text, font=cache[key], fill=tuple(int(v) for v in rgb),
                  anchor=anchor, stroke_width=max(1, size // 12),
                  stroke_fill=(0, 0, 0))
    return np.array(pil)


def _fill_rect(img: np.ndarray, x0: float, y0: float, x1: float, y1: float,
               color: tuple[int, int, int]) -> None:
    """轴对齐实心矩形。

    画标尺要用它而不是 cv2.line —— cv2.line 的端点是圆的，刻度会变成圆头药丸，
    和页面 ctx.lineCap='butt' 的平头对不上。
    """
    cv2.rectangle(img, (int(round(x0)), int(round(y0))),
                  (int(round(x1)), int(round(y1))), color, -1)


def _draw_scale_bar(img: np.ndarray, bar_um: float, um_per_out_px: float,
                    pos: str, margin: int, st: dict, xy=None) -> tuple:
    """在图上画标尺。白色细线 + 深色描边，和试用页上的画法一致。

    ⚠️ um_per_out_px 是「**每个输出像素**代表多少毫米」，不是原图的 um_per_px。
    输出图放大过 supersample 倍时这两者不同 —— 传错会让标尺短 supersample 倍，
    图上写着 2 mm 实际只有 1 mm，照它量出来的尺寸会翻倍。这是伪造数据，不是显示 bug。
    """
    bar_px = int(round(bar_um / um_per_out_px))
    bar_px = max(10, min(bar_px, img.shape[1] - 2 * margin))
    thick, tick = st["bar_halo"], st["bar_tick"]

    h, w = img.shape[:2]
    if xy is not None:
        x1, y = int(xy[0]), int(xy[1])        # 用户在页面上把标尺拖到的位置（右端点）
        x1 = max(bar_px + 2, min(w - 2, x1))
        y = max(tick + 2, min(h - 2, y))
    elif pos == "br":
        x1 = w - margin
        y = h - margin
    elif pos == "bl":
        x1 = margin + bar_px
        y = h - margin
    elif pos == "tr":
        x1 = w - margin
        y = margin + tick
    else:  # tl
        x1 = margin + bar_px
        y = margin + tick
    x0 = x1 - bar_px

    # 先深色粗边、再白色细线 —— 和页面 halo() 的顺序、比例都一样。
    # ⚠️ 用矩形而不是 cv2.line：cv2.line 的端点是**圆的**，画出来刻度像圆头药丸
    # （整个标尺看着像个哑铃），而页面是 ctx.lineCap='butt' 的平头。
    for color, width in ((INK_RGB, thick), (WHITE_RGB, st["bar_w"])):
        hw = width / 2.0
        _fill_rect(img, x0, y - hw, x1, y + hw, color)              # 横线
        _fill_rect(img, x0 - hw, y - tick, x0 + hw, y + tick, color)  # 左刻度
        _fill_rect(img, x1 - hw, y - tick, x1 + hw, y + tick, color)  # 右刻度

    # 标签返回给调用方，和其他文字一起最后画（压在图形上层）。
    # 页面是 textAlign='right' + textBaseline='bottom'，摆在横线右端上方 ——
    # 所以要右对齐，不能居中（居中看起来像另一个工具的标尺）。
    label = format_scale_label(bar_um)
    gap = int(round(PAGE_BAR_LABEL_GAP * st["d"]))
    above = (pos in ("br", "bl")) if xy is None else (y > h // 2)
    ty = y - gap if above else y + gap
    return (label, x1, ty, WHITE_RGB, "rb" if above else "rt", st["text_size"])


def draw_annotations(img_rgb: np.ndarray, lines: list[dict], um_per_px: float,
                     crop=None, scale_bar: bool = True, scale_bar_pos: str = "br",
                     show_labels: bool = True, show_seq: bool = True,
                     show_notes: bool = False,
                     supersample: int = 1, scale_bar_xy=None) -> np.ndarray:
    """绘制标注并（可选）裁剪。

    ⚠️ 不修改传入的 lines —— 这是纯显示层操作，不得影响数据。

    Args:
        img_rgb: 校正+增强后的 RGB 图（原图坐标）
        lines: 测量线列表（含 x1,y1,x2,y2, measured_um, color, seq, note）
        um_per_px: 标定系数
        crop: None=全图 / {"mode":"focus","seq":N} / {"mode":"rect","rect":[...]}
        scale_bar: 是否画标尺
        scale_bar_pos: br/bl/tr/tl（没拖过标尺时用哪个角）
        scale_bar_xy: 用户把标尺拖到的位置（**原图坐标**，指标尺的右端点）；
                      None = 用 scale_bar_pos 那个角
        supersample: 输出放大倍数（1/2/4），提升出版质量
    Returns:
        绘制并裁剪后的 RGB 图
    """
    if img_rgb.ndim != 3 or img_rgb.shape[2] != 3:
        raise ValueError("draw_annotations 需要 RGB 三通道图像")
    if supersample < 1:
        raise ValueError("输出倍率至少为 1")

    x0, y0, x1, y1 = resolve_crop(crop, img_rgb.shape[:2], lines)
    if (x1 - x0) * (y1 - y0) * supersample * supersample > MAX_OUTPUT_PIXELS:
        raise ValueError(
            f"导出的图太大了（{(x1 - x0) * supersample}×{(y1 - y0) * supersample} 像素）。"
            "请降低倍率，或先用「聚焦某条测量线」裁剪一下。")
    # 必须 copy()：不裁剪时这一片就是整个数组，ascontiguousarray 会原样返回，
    # 后面的 cv2.line 就**画在调用方的图上**了（同一张图导两次，第二次带着第一次的标注）。
    out = np.ascontiguousarray(img_rgb[y0:y1, x0:x1]).copy()

    if supersample != 1:
        out = cv2.resize(out, None, fx=supersample, fy=supersample,
                         interpolation=cv2.INTER_CUBIC)

    s = supersample
    h, w = out.shape[:2]
    st = style_for(h, w)     # 所有尺寸都从页面尺度推出来，见 style_for 的说明
    texts = []               # 文字最后统一画，压在所有图形上层

    for ln in lines:
        # 把原图坐标平移到裁剪坐标系，再放大
        lx1 = (ln["x1"] - x0) * s
        ly1 = (ln["y1"] - y0) * s
        lx2 = (ln["x2"] - x0) * s
        ly2 = (ln["y2"] - y0) * s
        # ⚠️ out 是 **RGB** 图（imageio 读出来就是 RGB，最后才翻成 BGR 交给 imencode）。
        # 这里必须填 RGB 值 —— 按 BGR 的顺序填会把红蓝对调，
        # 页面上是品红的线导出后变成蓝色。
        col = _hex_to_rgb(ln.get("color", "#FF2D55"))

        # 先深色粗边、再彩色主线 —— 和页面 halo() 完全同一个画法
        cv2.line(out, (int(lx1), int(ly1)), (int(lx2), int(ly2)),
                 INK_RGB, st["line_halo"], cv2.LINE_AA)
        cv2.line(out, (int(lx1), int(ly1)), (int(lx2), int(ly2)),
                 col, st["line_w"], cv2.LINE_AA)
        # ⚠️ 圆点填的是**线的颜色**（页面是 dot(p, 4.5, L.color, 2.5)），
        # 不是白色 —— 填白的话一条品红线两头挂着两个白球。
        for px_, py_ in ((lx1, ly1), (lx2, ly2)):
            cv2.circle(out, (int(px_), int(py_)), st["endpoint_ring"], INK_RGB,
                       -1, cv2.LINE_AA)
            cv2.circle(out, (int(px_), int(py_)), st["endpoint_r"], col,
                       -1, cv2.LINE_AA)

        # 长度数值。位置 = 线段中点 + 用户在页面上拖出来的偏移（原图像素），
        # 再按输出倍率放大 —— 和试用页 labelScreen() 的算法是同一套。
        # 颜色同样跟着线走（页面 label(..., L.color)）。
        if show_labels:
            mx = int((lx1 + lx2) / 2 + float(ln.get("label_dx") or 0.0) * s)
            my = max(st["text_size"] + 2, min(h - 2,
                     int((ly1 + ly2) / 2 + float(ln.get("label_dy") or 0.0) * s)
                     - st["lift"]))
            texts.append((format_length_label(float(ln["measured_um"])),
                          mx, my, col, "mm", st["text_size"]))

        # 编号（用线的颜色，和页面一致）
        if show_seq:
            texts.append((f"#{ln['seq']}", int(lx1) + st["endpoint_r"],
                          int(ly1) - st["endpoint_r"],
                          col, "ls", int(st["text_size"] * 0.85)))

        # 备注（中文，换中文字体）
        if show_notes and ln.get("note"):
            texts.append((ln["note"], int(lx2) + st["endpoint_r"],
                          int(ly2) + st["endpoint_r"],
                          WHITE_RGB, "ls", int(st["text_size"] * 0.95)))

    if scale_bar:
        margin = max(12, int(min(h, w) * 0.022))
        target_px = w * BAR_TARGET_RATIO
        # ⚠️ 这里也必须用「每个输出像素多少 μm」。target_px 是输出图的宽度，
        # 配原图的 um_per_px 会算出偏大 supersample 倍的目标，
        # 挑出来的标尺就会长得离谱（2× 导出时能占掉半张图）。
        um_per_out_px = um_per_px / s
        bar_um = choose_scale_bar_um(um_per_out_px, target_px)
        xy = None
        if scale_bar_xy is not None:          # 用户拖过：原图坐标 → 裁剪+放大后的坐标
            xy = ((scale_bar_xy[0] - x0) * s, (scale_bar_xy[1] - y0) * s)
        # 同一个比例尺传给画线 —— 挑长度和画线必须用同一套单位。
        # 用原图的 um_per_px 会让标尺短 s 倍：2× 导出时「2 mm」的尺实际只有 1 mm。
        texts.append(_draw_scale_bar(out, bar_um, um_per_out_px,
                                     scale_bar_pos, margin, st, xy))

    return _render_texts(out, texts)
