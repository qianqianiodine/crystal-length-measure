"""标注绘制与标尺测试。"""
import numpy as np
import pytest

from app import annotate


# ---------- 标尺长度选择 ----------

def test_scale_bar_picks_round_physical_value():
    """像素长 100px 的目标应是 10/20/50/100 这类整数，不是 137。"""
    bar = annotate.choose_scale_bar_um(um_per_px=10.0, target_pixel_width=100.0)
    assert bar in {10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000}
    assert bar == pytest.approx(1000.0)      # 100px × 10um/px = 1000um


def test_scale_bar_targets_10_to_25_percent_width():
    for um_per_px, width in ((32.0, 800), (5.0, 1200), (120.0, 600)):
        bar = annotate.choose_scale_bar_um(um_per_px, width * 0.10)
        px = bar / um_per_px
        assert 0.05 * width <= px <= 0.35 * width, (um_per_px, width, bar, px)


def test_scale_bar_rejects_bad_input():
    with pytest.raises(ValueError):
        annotate.choose_scale_bar_um(0.0, 100.0)
    with pytest.raises(ValueError):
        annotate.choose_scale_bar_um(10.0, 0.0)


def test_scale_label_units():
    assert annotate.format_scale_label(200) == "200 μm"
    assert annotate.format_scale_label(500) == "500 μm"
    assert annotate.format_scale_label(1000) == "1 mm"
    assert annotate.format_scale_label(2000) == "2 mm"
    assert annotate.format_scale_label(10000) == "1 cm"


# ---------- 长度数值的写法 ----------

def test_length_label_matches_what_the_page_shows():
    """页面上写 "1.70 mm"，导出图上也得是 1.70 mm，不能变成 1700.0 μm。"""
    assert annotate.format_length_label(1700.0) == "1.70 mm"
    assert annotate.format_length_label(999.9) == "999.9 μm"
    assert annotate.format_length_label(1000.0) == "1.00 mm"


# ---------- 裁剪 ----------

def test_focus_crop_contains_line_and_margin():
    crop = annotate.focus_crop_for_line(500, 400, 600, 400, (1000, 1200),
                                        margin_ratio=0.35)
    x0, y0, x1, y1 = crop
    assert x0 <= 500 and x1 >= 600          # 包含线段
    assert y0 <= 400 and y1 >= 400
    assert x1 - x0 > 100                    # 有边距
    assert 0 <= x0 and 0 <= y0


def test_focus_crop_clamped_to_image():
    """线段贴近边缘时裁剪框不能越界。"""
    crop = annotate.focus_crop_for_line(5, 5, 15, 5, (200, 300))
    x0, y0, x1, y1 = crop
    assert x0 >= 0 and y0 >= 0
    assert x1 <= 300 and y1 <= 200


def test_resolve_crop_none_means_full_image():
    assert annotate.resolve_crop(None, (400, 600), []) == (0, 0, 600, 400)


def test_resolve_crop_focus_mode():
    lines = [{"seq": 1, "x1": 300, "y1": 200, "x2": 400, "y2": 200}]
    crop = annotate.resolve_crop({"mode": "focus", "seq": 1}, (500, 600), lines)
    x0, y0, x1, y1 = crop
    assert x0 <= 300 and x1 >= 400


def test_resolve_crop_manual_rect():
    crop = annotate.resolve_crop({"mode": "rect", "rect": [100, 50, 400, 350]},
                                 (500, 600), [])
    assert crop == (100, 50, 400, 350)


def test_resolve_crop_manual_rect_clamped():
    """越界的矩形要被夹回图像范围内。"""
    crop = annotate.resolve_crop({"mode": "rect", "rect": [-50, -20, 999, 999]},
                                 (500, 600), [])
    assert crop == (0, 0, 600, 500)


# ---------- 绘制 ----------

def _img(h=400, w=600):
    return np.full((h, w, 3), 128, np.uint8)


def test_draw_returns_same_shape_when_no_crop():
    out = annotate.draw_annotations(_img(), [], 10.0)
    assert out.shape == (400, 600, 3)
    assert out.dtype == np.uint8


def test_draw_with_crop_returns_cropped_size():
    out = annotate.draw_annotations(_img(), [], 10.0,
                                    crop=(100, 50, 400, 350))
    assert out.shape[0] == 300 and out.shape[1] == 300


def test_draw_with_supersample_scales_output():
    out = annotate.draw_annotations(_img(), [], 10.0, supersample=2)
    assert out.shape[0] == 800 and out.shape[1] == 1200


def test_draw_marks_pixels_on_line():
    """画了线的地方像素必须被改动。"""
    img = _img()
    lines = [{"seq": 1, "x1": 150, "y1": 200, "x2": 450, "y2": 200,
              "measured_um": 3000.0, "color": "#FF2D55"}]
    out = annotate.draw_annotations(img, lines, 10.0, scale_bar=False)
    changed = np.any(out != img, axis=2)
    assert changed.sum() > 50


def test_line_color_is_not_channel_swapped():
    """★ 线的颜色必须和页面上一样。

    out 是 RGB 图（imageio 读出来就是 RGB），填色时必须填 RGB 值。
    按 BGR 的顺序填会让红蓝对调 —— 页面上品红的线导出后变成蓝色。
    """
    img = np.zeros((60, 200, 3), np.uint8)
    lines = [{"seq": 1, "x1": 20, "y1": 30, "x2": 180, "y2": 30,
              "measured_um": 1700.0, "color": "#FF2D55"}]
    out = annotate.draw_annotations(img, lines, 10.0, scale_bar=False,
                                    show_labels=False, show_seq=False)
    px = out[30, 100]                      # 线正中间那一点
    assert tuple(int(v) for v in px) == pytest.approx((255, 45, 85), abs=12), \
        f"要 RGB(255,45,85) 品红，实际 RGB{tuple(int(v) for v in px)}"


def test_endpoint_dots_use_the_line_colour_not_white():
    """★ 端点圆点是**线的颜色**，不是白色。

    页面是 dot(p, 4.5, L.color, 2.5)：填本色、外面再描一圈深色。
    导出填成白色的话，一条品红线两头挂着两个白球（用户指出过）。
    """
    img = np.zeros((60, 200, 3), np.uint8)
    lines = [{"seq": 1, "x1": 40, "y1": 30, "x2": 160, "y2": 30,
              "measured_um": 1000.0, "color": "#FF2D55"}]
    out = annotate.draw_annotations(img, lines, 10.0, scale_bar=False,
                                    show_labels=False, show_seq=False)
    # 正好落在左端点上：圆点是后画的，这一点的颜色就是圆点的颜色
    got = tuple(int(v) for v in out[30, 40])
    assert got == pytest.approx((255, 45, 85), abs=14), f"端点应是本色，实际 RGB{got}"


def test_length_label_uses_the_line_colour():
    """★ 长度数字也是跟着线走色的（页面 label(..., L.color)）。

    用 620px 的图（= 页面尺度）避免撞到最小尺寸下限，那样标签会贴到线上。
    """
    img = np.full((620, 620, 3), 90, np.uint8)
    lines = [{"seq": 1, "x1": 100, "y1": 400, "x2": 500, "y2": 400,
              "measured_um": 1000.0, "color": "#30D158"}]
    out = annotate.draw_annotations(img, lines, 10.0, scale_bar=False, show_seq=False)
    band = out[368:392]                     # 标签在线的上方 20px 处
    green = np.all(np.abs(band.astype(int) - np.array([48, 209, 88])) < 70, axis=2)
    assert green.sum() > 20, "标签没跟着线的颜色走"
    # 而且要真的是绿色 —— 白色也会被上面那条宽松的匹配漏进来
    assert green.sum() < 4000, "标签区域绿得太多，可能是整块都被判进去了"


def test_all_line_colors_round_trip():
    """把页面上那 8 个颜色全过一遍，一个都不能串。"""
    from app.measure import LINE_COLORS
    img = np.zeros((60, 200, 3), np.uint8)
    for i, hexc in enumerate(LINE_COLORS):
        lines = [{"seq": i + 1, "x1": 20, "y1": 30, "x2": 180, "y2": 30,
                  "measured_um": 1000.0, "color": hexc}]
        out = annotate.draw_annotations(img, lines, 10.0, scale_bar=False,
                                        show_labels=False, show_seq=False)
        want = annotate._hex_to_rgb(hexc)
        got = tuple(int(v) for v in out[30, 100])
        assert got == pytest.approx(want, abs=12), (hexc, want, got)


def test_scale_bar_label_is_right_aligned_to_the_bar():
    """★ 标尺文字右对齐在横线右端上方。

    页面是 textAlign='right' + textBaseline='bottom'。导出原来居中摆在横线中间，
    看着就是两个工具做出来的标尺。
    """
    img = np.full((620, 620, 3), 30, np.uint8)
    st = annotate.style_for(620, 620)
    text, x, y, rgb, anchor, size = annotate._draw_scale_bar(
        img, 2000.0, 10.0, "br", 14, st)
    assert x == 620 - 14                # 右端（_draw_scale_bar 里叫 x1）
    assert anchor == "rb"
    assert y == 620 - 14 - round(11 * st["d"])
    assert text == "2 mm" and rgb == (255, 255, 255)


def test_scale_bar_ticks_have_flat_ends():
    """★ 刻度必须是**平头**的。

    cv2.line 的端点是圆的，画出来整个标尺像个哑铃 ——
    页面用的是 ctx.lineCap='butt'，所以导出得用矩形画。
    """
    img = np.full((400, 600, 3), 40, np.uint8)
    st = annotate.style_for(400, 600)                 # d≈0.645 → tick=5, margin=12
    annotate._draw_scale_bar(img, 2000.0, 10.0, "br", 12, st)

    y = 400 - 12
    x0 = (600 - 12) - 200                             # 右端 x1 - bar_px
    rows = np.where(np.all(img[:, x0] == 255, axis=1))[0]
    assert rows.min() == y - st["bar_tick"], "上端不是平头（圆头会多出半个线宽）"
    assert rows.max() == y + st["bar_tick"], "下端不是平头"


def test_export_bar_matches_what_the_page_would_pick():
    """★ 页面上的实时比例尺和导出图必须挑**同一个物理长度**。

    页面算法（web/measure.html 的 scaleBarInfo）：
        target_um = 照片宽(原图像素) × BAR_TARGET_RATIO × um_per_px
    导出算法：target_px = 输出图像素宽 × BAR_TARGET_RATIO，再乘 um_per_out_px。
    两者必须给出同一个 target_um —— 否则屏幕上写着「2 mm」、
    导出图里变成「1 mm」，用户会以为导出算错了（他确实这么问过）。
    """
    for umpp, w in ((7.768, 960), (5.603, 1280), (32.0, 800)):
        page_target_um = w * annotate.BAR_TARGET_RATIO * umpp
        for sup in (1, 2, 4):
            export_target_um = (w * sup * annotate.BAR_TARGET_RATIO) * (umpp / sup)
            assert export_target_um == pytest.approx(page_target_um, rel=1e-9)
            assert (annotate.choose_scale_bar_um(umpp, w * annotate.BAR_TARGET_RATIO)
                    == annotate.choose_scale_bar_um(
                        umpp / sup, w * sup * annotate.BAR_TARGET_RATIO)), (umpp, w, sup)


def test_style_matches_the_page_at_canvas_scale():
    """★ 「导出的画面和页面一模一样」的可执行版本。

    试用页画布短边约 620px，页面上那些写死的像素数就是按这个尺度定的。
    在 620px 的输出图上，导出的绘制尺寸必须正好等于页面上的原始数字。
    以前导出自己另算一套（线宽按图高的 1/220），结果标尺成了个哑铃。
    """
    st = annotate.style_for(620, 620)
    assert st["line_w"] == 3        # 页面 halo(path, color, 3)
    assert st["line_halo"] == 6     # 页面 halo() 的 3 + 3.5
    assert st["text_size"] == 13    # 页面 '600 13px'
    assert st["lift"] == 20         # 页面 labelScreen() 里减掉的那 20
    assert st["bar_tick"] == 7      # 页面 BAR_TICK
    assert st["bar_w"] in (2, 3)    # 页面 2.5
    assert st["bar_halo"] == 6      # 页面 2.5 + 3.5


def test_style_scales_with_the_output():
    """图大一倍，所有尺寸等比大一倍 —— 不是各调各的。"""
    a = annotate.style_for(620, 620)
    b = annotate.style_for(1240, 1240)
    for key in ("line_w", "line_halo", "text_size", "lift", "bar_w",
                "bar_halo", "bar_tick", "endpoint_r"):
        assert b[key] == pytest.approx(a[key] * 2, abs=1), key


def test_style_uses_the_page_ink_colour():
    """描边打底色要和页面的 --ink(#0b0a08) 一致，不能用纯黑。"""
    assert annotate.INK_RGB == (11, 10, 8)


def test_draw_scale_bar_changes_pixels():
    img = _img()
    without = annotate.draw_annotations(img, [], 10.0, scale_bar=False)
    with_bar = annotate.draw_annotations(img, [], 10.0, scale_bar=True)
    assert np.any(with_bar != without)


def test_draw_does_not_change_um_values():
    """★ 不变式：裁剪与绘制不影响任何测量数值。"""
    lines = [{"seq": 1, "x1": 100, "y1": 100, "x2": 200, "y2": 100,
              "measured_um": 1234.5, "color": "#00C7BE"}]
    before = [dict(l) for l in lines]
    annotate.draw_annotations(_img(), lines, 10.0, crop=(50, 50, 300, 250))
    assert lines == before


def test_draw_does_not_modify_the_input_image():
    """★ 不变式：标注不能画在调用方传进来的那张图上。

    不裁剪时切片就是整个数组，漏了 copy() 的话 cv2.line 会直接改到入参 ——
    同一张图导两次，第二次就带着第一次的标注了。
    """
    img = _img()
    before = img.copy()
    lines = [{"seq": 1, "x1": 150, "y1": 200, "x2": 450, "y2": 200,
              "measured_um": 3000.0, "color": "#FF2D55"}]
    annotate.draw_annotations(img, lines, 10.0)      # 不裁剪 = 最容易踩的路径
    assert np.array_equal(img, before)


def _white_bar_span(img):
    """量出图上标尺那根白横线有多长（取最下面一条够长的白色横线）。"""
    h = img.shape[0]
    for y in range(h - 1, 0, -1):
        xs = np.where(np.all(img[y] == 255, axis=1))[0]
        if len(xs) > 20:
            return int(xs.max() - xs.min() + 1)
    raise AssertionError("图上找不到标尺")


def test_scale_bar_pixel_length_matches_its_label():
    """★ 标尺画的像素长必须真的等于它标的那个长度。

    输出图放大过 supersample 倍，标尺也必须跟着放大。
    差一倍就意味着：图上写着 2 mm 的尺实际只有 1 mm，照它量出来的尺寸翻倍
    —— 那是伪造数据，比显示错位严重得多。
    """
    for sup in (1, 2, 4):
        out = annotate.draw_annotations(_img(400, 600), [], 10.0,
                                        supersample=sup, scale_bar_pos="br")
        # 选长度时用的也是"每个输出像素多少 μm"
        bar_um = annotate.choose_scale_bar_um(10.0 / sup, out.shape[1] * 0.175)
        expected = bar_um / 10.0 * sup          # 物理长度 ÷ 每个原图像素 → × 输出倍率
        measured = _white_bar_span(out)
        assert abs(measured - expected) <= 20, (sup, measured, expected)


def test_scale_bar_takes_10_to_25_percent_of_the_exported_width():
    """★ 标尺在导出图里该占 10~25% 宽。

    挑长度用的是「输出图的像素宽度」，配的比例尺也必须是「每个输出像素多少 μm」；
    混成原图的比例尺会挑出偏大 supersample 倍的目标，标尺长得能占掉半张图。
    """
    for sup in (1, 2, 4):
        for crop in (None, (0, 0, 300, 200)):
            out = annotate.draw_annotations(_img(400, 600), [], 10.0,
                                            crop=crop, supersample=sup)
            frac = _white_bar_span(out) / out.shape[1]
            assert 0.10 <= frac <= 0.30, (sup, crop, round(frac, 3))


def test_draw_rejects_output_that_would_exhaust_memory():
    """4 倍放大一张大图能吃掉几百 MB，宁可报错也别把工具卡死。"""
    huge = np.full((3000, 4000, 3), 120, np.uint8)
    with pytest.raises(ValueError, match="太大"):
        annotate.draw_annotations(huge, [], 10.0, supersample=4)
    # 同样的大小，裁剪过就完全没问题
    assert annotate.draw_annotations(
        huge, [], 10.0, crop=(0, 0, 800, 600), supersample=4).shape == (2400, 3200, 3)


def test_draw_accepts_a_raw_pixel_box():
    """crop 也可以直接给 (x0,y0,x1,y1) 像素框，不必包成 dict。"""
    assert annotate.draw_annotations(_img(), [], 10.0,
                                     crop=(100, 50, 400, 350)).shape[:2] == (300, 300)


def test_scale_bar_positions_all_render():
    for pos in ("br", "bl", "tr", "tl"):
        out = annotate.draw_annotations(_img(), [], 10.0, scale_bar_pos=pos)
        assert out.shape == (400, 600, 3)


def test_drawn_label_follows_the_offset_the_user_dragged():
    """★ 用户在页面上把长度数字拖开了，导出必须照他放的位置画。

    用零长度线（画出来是个点）隔离出标签，看改动像素的重心有没有跟着往下走。
    """
    img = _img(400, 600)

    def centroid_row(label_dy):
        lines = [{"seq": 1, "x1": 300, "y1": 200, "x2": 300, "y2": 200,
                  "measured_um": 1700.0, "color": "#FF2D55",
                  "label_dx": 0.0, "label_dy": label_dy}]
        out = annotate.draw_annotations(img, lines, 10.0,
                                        scale_bar=False, show_seq=False)
        ys, _ = np.where(np.any(out != img, axis=2))
        return ys.mean()

    default_row = centroid_row(0.0)
    moved_row = centroid_row(150.0)
    assert default_row < 200 < moved_row, (default_row, moved_row)
