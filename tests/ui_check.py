"""试用页交互的真机验证（Playwright）。

**不属于 pytest 套件** —— 它需要一个跑起来的服务，pytest 收集不到它（文件名不是 test_*）。
用法：先 `python -m app.launch`（或双击 启动.bat），再

    python tests/ui_check.py [端口]

为什么要有这个文件：这个页面的 bug 大多是"数值对、单元对、屏幕上就是错的"，
接口测试和几何断言一个都抓不到。改动 web/measure.html 之后跑一遍，再自己看截图。
跑完会把测试留下的测量记录清干净，不污染你的数据。
"""
import pathlib
import shutil
import sys

from PIL import Image
from playwright.sync_api import sync_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "8510"
URL = f"http://127.0.0.1:{PORT}"
SHOT = "C:/Users/Admin/AppData/Local/Temp/"
fails = []


def check(name, ok, detail=""):
    print(("  OK   " if ok else "  FAIL ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        fails.append(name)


with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    # 页面现在会从 localStorage 恢复上次的照片/缩放 —— 测试要干净起点。
    # 用 sessionStorage 打标记：只在第一次进页面时清，刷新时留着（⑦ 要验刷新）。
    pg.add_init_script(
        "try { if (!sessionStorage.getItem('ui_check_cleared')) {"
        "  localStorage.removeItem('jltx.session');"
        "  sessionStorage.setItem('ui_check_cleared', '1'); } } catch {}")
    pg.goto(URL)

    pg.wait_for_selector("#picker option", state="attached")
    iid = pg.evaluate(          # 优先用样品2_1（自动粗定位准），没有就用第一张
        "() => { const os = [...document.querySelectorAll('#picker option')];"
        "        const hit = os.find(o => o.textContent.includes('样品2_1')) || os[0];"
        "        return hit ? hit.value : ''; }"
    )
    print(f"照片 id：{iid}")
    pg.select_option("#picker", iid)
    pg.click("#btnOpen")
    pg.wait_for_selector("#empty", state="hidden", timeout=60000)
    pg.wait_for_timeout(300)

    # 上次跑崩了可能留下记录，先清干净，否则条数对不上
    pg.evaluate("async () => {"
                "  const d = await (await fetch('/api/image/' + S.imageId)).json();"
                "  for (const L of d.lines) await fetch('/api/lines/' + L.id, {method:'DELETE'});"
                "  await fetch('/api/image/' + S.imageId + '/clear-lines', {method:'POST'});"
                "  S.lines = []; syncPanel(); }")
    print(f"起始记录数：{pg.evaluate('() => S.lines.length')}")

    box = pg.locator("#cv").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    # 页面里的坐标是"画布内"坐标，Playwright 用的是"视口"坐标，差一个画布原点
    to_page = lambda p: (box["x"] + p[0], box["y"] + p[1])
    nlines = lambda: pg.evaluate("() => S.lines.length")

    def draw(a, bb, steps=7):
        pg.mouse.move(cx + a[0], cy + a[1])
        pg.mouse.down()
        pg.mouse.move(cx + bb[0], cy + bb[1], steps=steps)
        pg.mouse.up()

    # ---------- ④ 缩放灵敏度 ----------
    print("\n④ 缩放")
    def zoom_by(steps, dy):
        pg.mouse.dblclick(cx, cy)
        pg.wait_for_timeout(120)
        s0 = pg.evaluate("() => S.view.s")
        for _ in range(steps):
            pg.mouse.move(cx, cy)
            pg.mouse.wheel(0, -dy)
        return pg.evaluate("() => S.view.s") / s0

    r_pad = zoom_by(25, 4)                  # 触控板：25 个小 delta，共 100 个单位
    check("触控板滑一整下跟手（既不会失控、也不是没反应）",
          1.3 < r_pad < 1.9, f"共放大 {r_pad:.3f} 倍")

    r_wheel = zoom_by(1, 100)               # 鼠标滚轮一格
    check("鼠标滚轮一格 ≈ 放大 16%", 1.1 < r_wheel < 1.25, f"{r_wheel:.3f} 倍")
    check("同样 100 个 delta 单位下触控板更跟手", r_pad > r_wheel,
          f"滚轮 {r_wheel:.3f} vs 触控板 {r_pad:.3f}")

    # 用户的原话：「一次双指滑动比鼠标一格少一半放大就可以了」
    gesture = zoom_by(5, 4)                 # 5 个小 delta ≈ 一次双指轻滑
    check("一次双指滑动 ≈ 半格滚轮放大",
          0.35 < (gesture - 1) / (r_wheel - 1) < 0.8,
          f"滑动放大 {(gesture-1)*100:.1f}% vs 滚轮 {(r_wheel-1)*100:.1f}%"
          f"（比值 {(gesture-1)/(r_wheel-1):.2f}）")

    for _ in range(60):
        pg.mouse.wheel(0, -120)
    s, fit = pg.evaluate("() => [S.view.s, S.fit]")
    check("放大有上限（不会甩飞）", s <= fit * 24 + 1e-6, f"s={s:.3f} 上限={fit*24:.3f}")
    for _ in range(120):
        pg.mouse.wheel(0, 120)
    s, fit = pg.evaluate("() => [S.view.s, S.fit]")
    check("缩小有下限", s >= fit / 8 - 1e-6, f"s={s:.3f} 下限={fit/8:.3f}")
    pg.mouse.dblclick(cx, cy)
    pg.wait_for_timeout(100)

    # ---------- ③ 中键挪照片 ----------
    print("\n③ 中键拖动")
    tx0 = pg.evaluate("() => S.view.tx")
    pg.mouse.move(cx, cy)
    pg.mouse.down(button="middle")
    pg.mouse.move(cx + 80, cy + 40, steps=5)
    pg.mouse.up(button="middle")
    check("中键拖动平移了画面",
          abs(pg.evaluate("() => S.view.tx") - tx0 - 80) < 2,
          f"位移 {pg.evaluate('() => S.view.tx') - tx0:.1f}px（期望 80）")
    pg.mouse.dblclick(cx, cy)
    pg.wait_for_timeout(100)

    # ---------- ⑤ 划线：按下→锚点→实时→松手成线 ----------
    print("\n⑤ 划线")
    pg.mouse.move(cx - 160, cy - 110)
    pg.mouse.down()
    pg.mouse.move(cx - 160, cy - 110)       # 还没拖动
    pending = pg.evaluate(
        "() => ({kind: drag && drag.kind, "
        "len: drag ? Math.hypot(drag.x2-drag.x1, drag.y2-drag.y1) : -1})")
    check("按下即进入划线（此刻长度 0）",
          pending["kind"] == "line" and pending["len"] < 0.5, str(pending))
    pg.screenshot(path=SHOT + "shot_anchor.png")

    pg.mouse.move(cx - 40, cy - 30, steps=8)
    live = pg.evaluate("() => Math.hypot(drag.x2-drag.x1, drag.y2-drag.y1)")
    check("拖动过程中线在实时变长", live > 10, f"当前 {live:.1f} 图像素")
    pg.screenshot(path=SHOT + "shot_drawing.png")
    pg.mouse.up()
    pg.wait_for_function("() => S.lines.length === 1", timeout=8000)
    check("松手后成为一条记录", True,
          f"{pg.evaluate('() => S.lines[0].measured_um'):.1f} μm")

    pg.mouse.move(cx + 200, cy + 150)       # 误触：点一下不拖
    pg.mouse.down()
    pg.mouse.up()
    pg.wait_for_timeout(300)
    check("原地点一下不产生记录", nlines() == 1)

    draw((-150, 60), (-30, 100))
    pg.wait_for_timeout(500)
    draw((20, -140), (150, -90))
    pg.wait_for_timeout(500)
    check("一共画了 3 条", nlines() == 3, f"{nlines()} 条")
    pg.screenshot(path=SHOT + "shot_lines.png")

    # ---------- ② 撤销 / 重做 ----------
    print("\n② 撤销 / 重做")
    pg.keyboard.press("Control+z")
    pg.wait_for_function("() => S.lines.length === 2", timeout=8000)
    check("Ctrl+Z 撤掉了刚画的那条线", True, "3 → 2")

    pg.keyboard.press("Control+Shift+z")
    pg.wait_for_function("() => S.lines.length === 3", timeout=8000)
    check("Ctrl+Shift+Z 又把它放回来了", True, "2 → 3")

    pg.locator("#lines li .del").first.click()
    pg.wait_for_timeout(600)
    check("点 ✕ 删掉一条", nlines() == 2, f"{nlines()} 条")
    pg.keyboard.press("Control+z")
    pg.wait_for_function("() => S.lines.length === 3", timeout=8000)
    check("Ctrl+Z 把删掉的线找回来了", True, "2 → 3")

    pg.on("dialog", lambda d: d.accept())
    pg.click("#btnClear")
    pg.wait_for_timeout(800)
    check("清空把记录清零", nlines() == 0)
    pg.keyboard.press("Control+z")
    pg.wait_for_function("() => S.lines.length === 3", timeout=12000)
    check("Ctrl+Z 撤销「清空」，全部回来了", True, f"{nlines()} 条")

    # 在备注框里打字时，Ctrl+Z 该归浏览器，不能撤销测量
    pg.locator("#lines li input").first.click()
    pg.keyboard.type("测试备注")
    pg.keyboard.press("Control+z")
    pg.wait_for_timeout(400)
    check("备注框里 Ctrl+Z 归浏览器（不撤销测量）", nlines() == 3, f"{nlines()} 条")
    pg.mouse.move(cx, cy)                   # 点开画布，把输入框失焦收尾
    pg.mouse.click(cx, cy)
    pg.wait_for_timeout(300)

    # ---------- ① Ctrl + 左键拖标签 ----------
    print("\n① Ctrl 拖标签")
    pg.keyboard.down("Control")
    pg.wait_for_timeout(150)
    lb = pg.evaluate(
        "() => { const L = S.lines[0], p = labelScreen(L);"
        "        return {x: p[0], y: p[1], dx: L.label_dx, dy: L.label_dy, id: L.id}; }")
    pg.mouse.move(*to_page((lb["x"], lb["y"])))
    pg.wait_for_timeout(150)
    check("Ctrl 悬停在标签上，光标变抓手",
          pg.evaluate("() => cv.style.cursor") == "grab",
          pg.evaluate("() => cv.style.cursor"))
    check("悬停的标签被高亮",
          pg.evaluate("() => hoverLabel && hoverLabel.id") == lb["id"])
    pg.screenshot(path=SHOT + "shot_ctrl_pick.png")

    pg.mouse.down()
    pg.mouse.move(*to_page((lb["x"] + 70, lb["y"] - 55)), steps=6)
    pg.mouse.up()
    pg.keyboard.up("Control")
    pg.wait_for_timeout(500)
    mv = pg.evaluate("() => ({dx: S.lines[0].label_dx, dy: S.lines[0].label_dy})")
    scale = pg.evaluate("() => S.view.s")
    check("标签跟着鼠标走了",
          abs(mv["dx"] - lb["dx"] - 70 / scale) < 2
          and abs(mv["dy"] - lb["dy"] + 55 / scale) < 2,
          f"偏移 ({mv['dx']:.1f}, {mv['dy']:.1f})")
    check("标签位置存进了数据库（刷新不丢）",
          abs(pg.evaluate("async () => (await (await fetch('/api/image/'+S.imageId))"
                          ".json()).lines.find(L => L.id === " + str(lb["id"]) +
                          ").label_dx") - mv["dx"]) < 0.01)
    pg.screenshot(path=SHOT + "shot_label_moved.png")

    # 不按 Ctrl 拖同一处 = 画线，标签不动
    pg.mouse.move(*to_page((lb["x"], lb["y"])))
    pg.mouse.down()
    pg.mouse.move(*to_page((lb["x"] + 40, lb["y"] + 40)), steps=4)
    pg.mouse.up()
    pg.wait_for_timeout(600)
    check("不按 Ctrl 拖同一处 = 画线，标签不动",
          pg.evaluate("() => S.lines[0].label_dx") == mv["dx"] and nlines() == 4,
          f"{nlines()} 条")
    pg.keyboard.press("Control+z")          # 撤掉刚画的这条
    pg.wait_for_function("() => S.lines.length === 3", timeout=8000)

    # Ctrl+Z 也能撤销「挪标签」
    pg.keyboard.press("Control+z")
    pg.wait_for_function(
        f"() => Math.abs(S.lines[0].label_dx - {lb['dx']}) < 0.01", timeout=8000)
    check("Ctrl+Z 撤销「挪标签」，标签回到原位", True,
          f"{mv['dx']:.1f} → {lb['dx']:.1f}")

    pg.screenshot(path=SHOT + "shot_final.png")

    # ---------- ⑥ 导出 ----------
    print("\n⑥ 导出")
    W, H = pg.evaluate("() => [S.W, S.H]")

    def grab(selector):
        with pg.expect_download(timeout=60000) as dl:
            pg.click(selector)
        return dl.value

    d = grab("#btnExportPng")
    check("文件名是中文的（不是 download.png）",
          "标注" in d.suggested_filename, d.suggested_filename)
    full = pathlib.Path(d.path())
    im = Image.open(full)
    check("2× 导出尺寸 = 原图 × 2", im.size == (W * 2, H * 2), f"{im.size} vs {(W*2, H*2)}")
    shutil.copy(full, SHOT + "export_full.png")

    pg.select_option("#exCrop", "focus:1")
    pg.select_option("#exSuper", "1")
    # 图上字号滑块（2026-09-23 顶掉原来那两个勾选框的）。真浏览器里拖一下：
    # 旁边那个数字和 FONT_PX 都得跟着走 —— 下面这次导出顺便就是"非默认字号下也能导"。
    # （"导出的大小不跟着屏幕缩放变"那几条在 tests/web_geom_check.mjs 里，那边能
    #   把导出绘制的每一次 lineWidth / font 都记下来比。）
    pg.evaluate("() => { const el = document.getElementById('exFont');"
                " el.value = '24'; el.dispatchEvent(new Event('input')); }")
    check("字号滑块能改图上字号，旁边数字跟着变",
          pg.evaluate("() => FONT_PX") == 24
          and pg.evaluate("() => document.getElementById('exFontV').textContent") == "24",
          f"FONT_PX={pg.evaluate('() => FONT_PX')}")
    d = grab("#btnExportPng")
    im2 = Image.open(pathlib.Path(d.path()))
    check("聚焦裁剪后明显变小", im2.size[0] < im.size[0] and im2.size[1] < im.size[1],
          f"{im2.size} vs {im.size}")
    shutil.copy(pathlib.Path(d.path()), SHOT + "export_focus.png")

    pg.select_option("#exCrop", "")          # 换回不裁剪
    d = grab("#btnExportCsv")
    csv_text = pathlib.Path(d.path()).read_bytes().decode("utf-8-sig")
    check("CSV 有表头和数据行",
          csv_text.startswith("图像名称") and csv_text.count("\n") == 4,
          f"{csv_text.count(chr(10))} 行")

    check("导出没有改动任何东西（导出前后都是 3 条）", nlines() == 3)

    # ---------- ⑦ 刷新保持现状 ----------
    # 用户原话：「每一个页面刷新后都保持现状」—— 他把刷新当"卡住了的恢复手段"，
    # 所以刷新绝不能让他丢工作。
    print("\n⑦ 刷新保持现状")
    pg.mouse.move(cx, cy)
    pg.mouse.wheel(0, -300)                 # 先改一下缩放，才验得出来
    pg.mouse.move(cx, cy)
    pg.mouse.down(button="middle")
    pg.mouse.move(cx + 60, cy + 30, steps=4)
    pg.mouse.up(button="middle")
    pg.wait_for_timeout(900)                # 等防抖的 500ms 落地
    before = pg.evaluate("() => ({id: S.imageId, s: S.view.s, tx: S.view.tx, ty: S.view.ty})")

    pg.reload()
    pg.wait_for_selector("#empty", state="hidden", timeout=60000)
    pg.wait_for_timeout(400)
    after = pg.evaluate("() => ({id: S.imageId, s: S.view.s, tx: S.view.tx, ty: S.view.ty})")

    check("刷新后回到同一张照片", after["id"] == before["id"],
          f'{before["id"]} → {after["id"]}')
    check("刷新后缩放没变", abs(after["s"] - before["s"]) < 1e-6,
          f'{before["s"]:.4f} → {after["s"]:.4f}')
    check("刷新后平移没变",
          abs(after["tx"] - before["tx"]) < 0.5 and abs(after["ty"] - before["ty"]) < 0.5,
          f'({before["tx"]:.1f}, {before["ty"]:.1f}) → ({after["tx"]:.1f}, {after["ty"]:.1f})')
    check("刷新后线和圆都还在", nlines() == 3, f"{nlines()} 条")

    # ---------- 清理 ----------
    pg.evaluate("async () => {"
                "  const d = await (await fetch('/api/image/' + S.imageId)).json();"
                "  for (const L of d.lines) await fetch('/api/lines/' + L.id, {method:'DELETE'});"
                "  await fetch('/api/image/' + S.imageId + '/clear-lines', {method:'POST'});"
                "}")
    left = pg.evaluate("async () => (await (await fetch('/api/image/' + S.imageId))"
                       ".json()).lines.length")
    print(f"\n清理后该照片剩余记录：{left}")
    b.close()

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
