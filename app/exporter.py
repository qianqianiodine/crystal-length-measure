"""导出：CSV 记录与出版级标注图。

CSV 字段对应需求文档 3.8.2。开头带 UTF-8 BOM，
否则 Excel 打开会把中文显示成乱码 —— 这是国内用户的常见痛点。

⚠️ 导出全程**只读**：不改数据库、不改照片文件、不改传入的测量记录。
"""
import csv
import io
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

from app import annotate, config, db, imageio, measure

CSV_HEADER = [
    "图像名称", "图像唯一标识", "测量编号", "起点坐标", "终点坐标",
    "像素长度", "实际长度(μm)", "标定系数", "备注", "测量时间",
]


def _fetch_rows(conn, image_id: int | None):
    """取（图像, 测量）配对的行。image_id 为 None 时取全部。"""
    if image_id is not None:
        img = db.get_image(conn, image_id)
        if img is None:
            raise ValueError(f"图像不存在：{image_id}")
        images = [img]
    else:
        images = conn.execute(
            "SELECT * FROM images ORDER BY import_time DESC, id DESC"
        ).fetchall()

    pairs = []
    for img in images:
        for m in db.list_measurements(conn, img["id"]):
            pairs.append((img, m))
    return pairs


def measurements_to_csv(conn, image_id: int | None = None) -> str:
    """生成 CSV 文本（含 UTF-8 BOM）。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_HEADER)

    for img, m in _fetch_rows(conn, image_id):
        ts = datetime.fromtimestamp(m["created_time"]).strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow([
            img["name"],
            img["id"],
            m["seq"],
            f"({m['x1']:.1f}, {m['y1']:.1f})",
            f"({m['x2']:.1f}, {m['y2']:.1f})",
            f"{m['pixel_length']:.2f}",
            f"{m['measured_um']:.1f}",
            f"{m['calib_snapshot']:.4f}",
            m["note"],
            ts,
        ])
    return "﻿" + buf.getvalue()


def export_csv_file(conn, out_path: str | Path, image_id: int | None = None) -> Path:
    """写 CSV 文件。返回实际写入路径。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(measurements_to_csv(conn, image_id), encoding="utf-8-sig")
    return p


def _source_path(stored: str) -> Path:
    """把库里存的路径还原成实际文件位置。

    两种形状都要认：
      - 相对路径（正式导入流程）：相对 IMAGES_DIR
      - 绝对路径（试用页）：照片留在 样品集/ 原地，不拷贝
    """
    p = Path(stored)
    if p.is_absolute():
        return p
    in_images = config.IMAGES_DIR / p
    return in_images if in_images.exists() else config.PROJECT_ROOT / p


def build_annotated_image(conn, image_id: int, *,
                          crop=None, scale_bar: bool = True,
                          scale_bar_pos: str = "br",
                          show_labels: bool = True, show_seq: bool = True,
                          show_notes: bool = False,
                          supersample: int = 2,
                          scale_bar_xy=None) -> np.ndarray:
    """生成标注图。

    流程：载入原图 → 应用透视校正（若有）→ 应用增强（若有）→ 绘制标注。

    ⚠️ crop 的坐标以**原图坐标系**表达。前端传原图坐标即可，
    不要预先做任何变换 —— 本函数内部已负责把端点与裁剪框
    统一映射到绘制坐标系。

    Args:
        crop: None=全图；{"mode":"focus","seq":N}；{"mode":"rect","rect":[x0,y0,x1,y1]}
        supersample: 输出放大倍数
    Returns:
        RGB 图像，尺寸为裁剪区域 × supersample
    """
    import cv2

    img_row = db.get_image(conn, image_id)
    if img_row is None:
        raise ValueError(f"图像不存在：{image_id}")

    src = _source_path(img_row["path"])
    base = imageio.load_rgb(src)

    calib = db.get_calibration(conn, image_id)
    if calib is None:
        raise ValueError("该图像尚未标定，请先完成标定再导出")

    H = None
    if img_row["transform"]:
        try:
            H = json.loads(img_row["transform"]).get("H")     # 只留 H
        except (ValueError, TypeError):
            pass

    if H is not None:
        base = cv2.warpPerspective(base, np.asarray(H, dtype=np.float64),
                                   (base.shape[1], base.shape[0]),
                                   flags=cv2.INTER_LINEAR)

    # ⚠️ 画质调整在 images.enhance 列里，**不在** transform 里。
    # 以前这里读的是 t["enh"] —— 全项目没人往那里写过，所以服务端出的图
    # 一直是丢用户亮度/对比度的。别改回去。
    from app import enhance
    enh = enhance.parse_enhance(img_row)
    if enh != enhance.ENHANCE_NEUTRAL:
        base = enhance.apply_tone(base, enh)

    um_per_px = float(calib["um_per_px"])
    lines = measure.list_lines(conn, image_id)

    # 用户把标尺拖到过哪，导出就画在哪（没拖过则为 None，用下拉框选的角）
    if scale_bar_xy is None and calib["scale_bar"]:
        try:
            b = json.loads(calib["scale_bar"])
            scale_bar_xy = (float(b["x"]), float(b["y"]))
        except (ValueError, TypeError, KeyError):
            scale_bar_xy = None

    # ★ 线的端点存的是原图坐标，而 base 可能已被 H 变换。
    # 若做了校正，需把端点也映射到校正坐标系后再绘制。
    if H is not None:
        from app.transform import apply_h
        mapped = []
        for ln in lines:
            d = dict(ln)
            d["x1"], d["y1"] = apply_h(H, ln["x1"], ln["y1"])
            d["x2"], d["y2"] = apply_h(H, ln["x2"], ln["y2"])
            mapped.append(d)
        lines = mapped

    return annotate.draw_annotations(
        base, lines, um_per_px, crop=crop, scale_bar=scale_bar,
        scale_bar_pos=scale_bar_pos, show_labels=show_labels,
        show_seq=show_seq, show_notes=show_notes, supersample=supersample,
        scale_bar_xy=scale_bar_xy,
    )


def export_annotated_png(conn, image_id: int, out_path: str | Path, **kwargs) -> Path:
    """导出标注 PNG。返回实际写入路径。"""
    img = build_annotated_image(conn, image_id, **kwargs)
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    imageio.save_rgb(p, img)
    return p


def parse_crop(raw: str | None) -> dict | None:
    """`images.crop` 那一列（JSON 字符串）→ annotate 认的 crop 结构。

    空 / 坏数据 / 形状不对，一律 None（= 不裁剪）—— 裁剪只决定"怎么看"，
    读不懂就当没设，绝不让它把打开照片这条路弄挂。

    坐标是**原图像素、左上原点**，和前端 S.crop 完全一致。
    """
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    try:
        rect = [float(d[k]) for k in ("x0", "y0", "x1", "y1")]
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in rect):
        return None
    return {"mode": "rect", "rect": rect}


def load_rect(row) -> dict | None:
    """`images.crop` 那一列 → 前端 S.crop 的形状 {x0,y0,x1,y1}。坏数据当没设。

    和 parse_crop 是同一份数据的两种形状：那个喂 annotate（要 mode/rect），
    这个喂前端画布（要 x0..y1）。转换在 Python 里做一次，前端就不必知道两种形状。

    放这里而不是 main.py：api/images.py 也要用它，而那个模块不能 import main（会成环）。
    """
    spec = parse_crop(row["crop"])
    if spec is None:
        return None
    x0, y0, x1, y1 = spec["rect"]
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}


def apply_crop(img: np.ndarray, crop: dict | None) -> np.ndarray:
    """按裁剪框切片。**越界自动夹到图内**；crop 为 None 就原样返回。

    服务端存裁剪时不知道图多大（那一刻没载图），所以钳制放在这里 ——
    图就在手上，顺手就夹住了。
    """
    if not crop:
        return img
    h, w = img.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in crop["rect"])
    x0, x1 = max(0, min(w, x0)), max(0, min(w, x1))
    y0, y1 = max(0, min(h, y0)), max(0, min(h, y1))
    if x1 <= x0 or y1 <= y0:      # 整块在图外面：给原图好过给一张空图
        return img
    return img[y0:y1, x0:x1]


def export_image(conn, image_id: int) -> np.ndarray:
    """批量导出用的一张图。

    标定过的画标尺和测量线；**没标定的给干净原图** —— 库里允许有没量过的照片
    （用户拍了一堆，只量了其中几张），整批导出不该被它们卡住。

    两条路都做了画质调整和裁剪，所以导出文件夹里的图**就是屏幕上那张**。

    ⚠️ 这个函数**只读**：不改数据库、不动原图（和其他导出函数一样的铁律）。
    """
    row = db.get_image(conn, image_id)
    if row is None:
        raise ValueError(f"图像不存在：{image_id}")

    spec = parse_crop(row["crop"])

    if db.get_calibration(conn, image_id) is not None:
        # supersample=1 = 原始分辨率不缩小 —— 这是给论文 / PPT 用的，不是缩略图。
        #
        # ⚠️ **不要**用 try/except ValueError 来兜「没标定」那条路。
        # `annotate` 还会因为「导出的图太大了」（>4000 万像素，见 annotate.py 的
        # MAX_OUTPUT_PIXELS）抛 ValueError —— 一起吞掉的话，用户会拿到一张
        # **没有标尺和测量线的干净原图，而且没有任何提示**。
        # 有没有标定是**能直接查的**，别用异常做控制流；别的错误照原样往上抛。
        return build_annotated_image(conn, image_id, crop=spec,
                                     supersample=1, show_notes=False,
                                     # ⚠️ `show_seq` 的默认值是 True，**必须显式关掉**。
                                     # 不关的话每张导出图上会冒出一个 `#1`/`#2` 的编号 ——
                                     # 那是"这张图里的第几条线"，批量导出的文件夹里
                                     # 每张图都从 #1 开始，除了碍眼没有任何信息量。
                                     # 测量页导出面板的「显示测量编号」默认也是不勾的，
                                     # 关掉它才和屏幕一致。
                                     show_seq=False)

    # 没标定过：给干净原图（画质调整和裁剪照样生效）。
    #
    # ⚠️ 这条路不重复 build_annotated_image 的透视校正 —— 全项目**没有任何地方**
    # 写过 images.transform（2026-09-22 核实过），那条路是休眠的。
    # 哪天真用上透视校正了，这里要跟着补。
    from app import enhance

    img = imageio.load_rgb(_source_path(row["path"]))
    enh = enhance.parse_enhance(row)
    if enh != enhance.ENHANCE_NEUTRAL:
        img = enhance.apply_tone(img, enh)
    return apply_crop(img, spec)


# ---------------- 导出到磁盘：路径安全 ----------------

# 不能出现在**磁盘路径**里的字符。storage.BAD_NAME_CHARS 是给"显示名"用的，
# 这里更严：连正斜杠和反斜杠都算（它们是路径分隔符，最危险的那两个）。
_BAD_PATH_CHARS = set('<>:"/\\|?*')

# Windows 保留设备名。大小写不敏感，而且**带扩展名一样保留**（`CON.png` 也建不出来）
_RESERVED = {"CON", "NUL", "PRN", "AUX",
             *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}

MAX_SEGMENT = 60


def _safe_segment(name: str) -> str:
    """把用户打的字变成一段**安全**的磁盘路径（一层文件夹名，或一个文件名）。

    文件夹名和照片名都是用户输入的字，会被当成磁盘路径用 —— 这是全项目唯一
    一处"用户的字变成文件系统操作"的地方，必须夹住。不夹住的后果：
    文件夹叫 `..\\..\\Windows` → 文件写到工具目录外面去；叫 `D:\\某处` →
    绝对路径（pathlib 遇到绝对路径右操作数会**整个丢弃左边**，项目实测过）；
    叫 `CON` → Windows 保留设备名，建不出来；带 `< > : " | ? *` 或结尾空格 →
    Windows 直接拒绝。

    六条规则（前五条在这儿，第六条"拼出来的路径必须仍在导出目录底下"
    由调用方断言，见 export_images 端点）：
      1. 只留最后一段路径成分（`..\\..\\Windows` → `Windows`）
      2. 非法字符换成 `_`
      3. 去掉结尾的空格和点（Windows 会静默截掉，导致"文件建了却找不到"）
      4. 撞上保留设备名 → 前面加 `_`
      5. 截到 60 个字符；空了 → `未命名`
    """
    s = str(name)
    # 1) 两种分隔符都切，只留最后一段。
    #    不用 os.path.basename：非 Windows 上它认不出反斜杠，行为会跟着平台变。
    for sep in ("/", "\\"):
        s = s.replace(sep, "\n")
    s = s.split("\n")[-1]
    if s.endswith(":"):            # `D:` 这种光秃秃的盘符（带路径的上一步已经切掉了）
        s = s[:-1]
    s = s.replace("..", "_")       # 上一步之后还剩下的 `..`（比如名字就叫 `..`）
    # 2)
    s = "".join("_" if (ch in _BAD_PATH_CHARS or ord(ch) < 32) else ch for ch in s)
    # 3)
    s = s.rstrip(" .")
    # 4)
    if s.upper() in _RESERVED or s.split(".")[0].upper() in _RESERVED:
        s = "_" + s
    # 5)
    s = s[:MAX_SEGMENT].rstrip(" .")
    return s or "未命名"


def parse_tags(raw: str | None) -> list[str]:
    """`images.tags` 那一列（JSON 数组字符串）→ 字符串列表。坏数据一律空表。

    这一轮界面只写一个标签，但列里存的是数组 —— 以后要多标签不用改库。
    """
    try:
        d = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return []
    if not isinstance(d, list):
        return []
    return [t for t in d if isinstance(t, str) and t]


# 没归档进任何文件夹的照片，导出时放这一层
UNCATEGORIZED = "未分类"


def export_plan(images, folders, members) -> list[dict]:
    """算清楚每一张图导出到哪、叫什么名字。**纯函数，不碰磁盘。**

    images:  sqlite3.Row 列表，每行要 id / original_filename / tags
    folders: sqlite3.Row 列表，每行要 id / name / parent_id（整张 folders 表）
    members: {image_id: [folder_id, ...]}

    返回 [{"image_id","parts","name","renamed"}]：
      - parts 是目录层次（每段都洗过），`["样品A","6月批次"]` = 两层
      - name 是文件名（已洗干净、已去重），带 `.png`
      - 一张图在几个文件夹里就有几条（多对多如实摊开，不替用户挑一个）
      - 没归档的放「未分类」一层，**不加前缀**（那是用户的原始文件名更好认）

    ⚠️ 文件名前缀用的是**直接父文件夹**的名字，不是整条路径 ——
    `样品A/6月批次/A1-1` 出来是 `6月批次_A1-1.png`。
    """
    by_id = {r["id"]: r for r in folders}

    def folder_path(fid: int) -> list[str]:
        """沿 parent_id 往上走，拼出整条路径。带环的护栏（数据被外部改坏时不转死）。"""
        out, seen, cur = [], set(), fid
        while cur is not None and cur not in seen:
            seen.add(cur)
            row = by_id.get(cur)
            if row is None:            # 文件夹被删了，但关系还在（外键应该拦住，防一手）
                break
            out.append(_safe_segment(row["name"]))
            cur = row["parent_id"]
        return list(reversed(out))

    plan: list[dict] = []
    used: set[str] = set()             # 这次导出里已经用掉的相对路径

    for img in images:
        tags = parse_tags(img["tags"])
        # 有标签就用标签，没有就用原文件名（去掉扩展名，后面统一加 .png）
        stem = tags[0] if tags else Path(img["original_filename"]).stem

        dirs = [folder_path(f) for f in members.get(img["id"], [])]
        if not dirs:
            dirs = [[UNCATEGORIZED]]

        for parts in dirs:
            # 前缀用直接父文件夹的名字；「未分类」不算文件夹，不加前缀
            prefix = "" if parts[-1] == UNCATEGORIZED else f"{parts[-1]}_"
            base = _safe_segment(prefix + stem)

            # 同一次导出里撞名 → 后一个加 (2)。绝不静默覆盖。
            # 键按大小写不敏感归一化：Windows 上 `样品A` 和 `样品a` 是同一个目录，
            # 不归一化的话两个不同的键会拼到同一个文件上（照样是静默覆盖）。
            # 用 casefold 不用 lower：宁可多出一个 (2)，也不能漏合并。
            name, n = f"{base}.png", 2
            while "/".join(parts + [name]).casefold() in used:
                name = f"{base} ({n}).png"
                n += 1
            used.add("/".join(parts + [name]).casefold())

            plan.append({"image_id": img["id"], "parts": parts,
                         "name": name, "renamed": n > 2})
    return plan
