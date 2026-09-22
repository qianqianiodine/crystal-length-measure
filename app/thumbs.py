"""缩略图：画廊一屏几十张图，不能每张都拉原图（手机照片一张十几 MB）。

按 image_id 缓存到 `data/thumbs/{id}.jpg`。`images.id` 是 AUTOINCREMENT、
**永不重用**，所以缓存不会串图。

⚠️ 原图确实不再改写，但**裁剪和画质调整改的正是这张缩略图的样子** ——
写裁剪 / 调亮度的那两个端点在写库之后必须叫一次 `drop()`（见 `put_crop` /
`put_enhance`）。这个模块原先写着"原图不再改写，因此不需要任何失效逻辑"，
那个前提在 2026-09-22 加裁剪时就不成立了。

单独一个文件而不是塞进 storage.py：那边刻意「不 import cv2」（校验只认字节流），
这里要读图、缩放，必须走 imageio。
"""
import os
from pathlib import Path

import cv2

from app import config, enhance, imageio

# 长边像素。格子墙最多 6 列，320 在 2 倍屏上也够清楚。
MAX_SIDE = 320


def path_for(image_id: int) -> Path:
    return config.THUMBS_DIR / f"{image_id}.jpg"


def path_for_staged(staging_id: int) -> Path:
    """待确认条目的缩略图。

    ⚠️ **不能**和正式图共用 THUMBS_DIR：待确认的 id 从 1 开始，会和 images.id
    撞车，把图库里已有的缩略图无声覆盖掉。
    """
    return config.STAGING_DIR / "thumbs" / f"{staging_id}.jpg"


def ensure(src: Path, image_id: int, *,
           crop_raw: str | None = None, enhance_raw: str | None = None,
           conn=None) -> Path | None:
    """拿到缩略图路径，没有就现做一张。原图读不出来返回 None。

    `crop_raw` / `enhance_raw` 是库里那两列的**原始字符串** ——
    只决定"这张图看起来什么样"，所以调用方原样递进来就行。

    `conn` 传了且这张图**标定过**，就画上测量线和标尺（见 `_annotated`）。
    不传 = 老行为（光图），待确认导入列表那条路走的就是它。
    """
    dst = path_for(image_id)
    if dst.exists():
        return dst
    if conn is not None:
        p = _annotated(conn, image_id, dst)
        if p is not None:
            return p
    return ensure_at(src, dst, crop_raw=crop_raw, enhance_raw=enhance_raw)


def _annotated(conn, image_id: int, dst: Path) -> Path | None:
    """标定过的图：缩略图直接画上测量线和标尺。

    用户要的是"在图库里一眼看见这张量过什么、量出来多少"。一张光秃秃的照片
    看不出这张到底量没量过 —— 卡片上那个「已量 1 条」是文字，得对着数字看，
    而线和长度是直接看得见的。

    **失败一律返回 None，让调用方退回光图**：没标定（正常情况，用户拍了一堆
    只量了其中几张）、原图读不出来、图太大被 `MAX_OUTPUT_PIXELS` 拒了 ——
    图库里一张图出点岔子，不该让整面格子墙开不了。
    """
    from app import db, exporter

    try:
        row = db.get_image(conn, image_id)
        img = exporter.build_annotated_image(
            conn, image_id,
            crop=exporter.parse_crop(row["crop"] if row else None),
            supersample=1,               # 缩略图不需要放大，反正马上要缩到 320
            show_notes=False, show_seq=False,   # 和「导出图片」同一套显示规则
        )
    except (imageio.ImageLoadError, ValueError):
        return None

    # ⚠️ 这里**不能**再走一遍 ensure_at —— 那条路会再叠一次画质调整
    # （build_annotated_image 内部已经调过 enhance.apply_tone 了），
    # 亮度会被压两次。
    h, w = img.shape[:2]
    s = MAX_SIDE / max(h, w)
    if s < 1:
        img = cv2.resize(img, (max(1, round(w * s)), max(1, round(h * s))),
                         interpolation=cv2.INTER_AREA)

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.stem}.new.jpg")
    imageio.save_rgb(tmp, img)
    os.replace(tmp, dst)
    return dst


def ensure_at(src: Path, dst: Path, *,
              crop_raw: str | None = None, enhance_raw: str | None = None) -> Path | None:
    """同上，但目标路径由调用方给（正式图和待确认图各用各的目录）。

    两个 `*_raw` 都不传 = 原来的行为（读原图 → 缩放），所以待确认导入列表
    那条路（api/imports.py）一个字都不用改。

    不往外抛异常 —— 画廊里一张图坏了，不该让整页打不开。
    """
    if dst.exists():
        return dst
    try:
        img = imageio.load_rgb(src)
    except imageio.ImageLoadError:
        return None

    if crop_raw:
        # 函数内 import：exporter 会一路拖进 annotate / measure，比 thumbs 重，
        # 而绝大多数请求根本没有裁剪。
        from app import exporter
        img = exporter.apply_crop(img, exporter.parse_crop(crop_raw))

    h, w = img.shape[:2]
    s = MAX_SIDE / max(h, w)
    if s < 1:                                  # 本来就小的图不放大，放大只会糊
        img = cv2.resize(img, (max(1, round(w * s)), max(1, round(h * s))),
                         interpolation=cv2.INTER_AREA)   # 缩小用 AREA，INTER_LINEAR 会有摩尔纹

    # ⚠️ 画质调整必须在**缩小之后**做：apply_tone 是逐像素的，在 320px 上做
    # 比在 1200 万像素上做快几十倍，而缩放和调色交换顺序的差别肉眼看不出来。
    enh = enhance.parse_enhance({"enhance": enhance_raw})   # 只认这一个键
    if enh != enhance.ENHANCE_NEUTRAL:
        img = enhance.apply_tone(img, enh)

    # 先写临时文件再改名：两个请求同时要同一张缩略图时，直接写 dst 可能让
    # FileResponse 读到写了一半的 JPEG —— 而它会一直躺在缓存里，看不出是坏的。
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.stem}.new.jpg")
    imageio.save_rgb(tmp, img)
    os.replace(tmp, dst)
    return dst


def drop_staged(staging_id: int) -> None:
    """删掉待确认条目的缩略图。行被移除 / 确认 / 取消时都要叫一下，别攒垃圾。"""
    p = path_for_staged(staging_id)
    if p.is_file():
        p.unlink()


def drop(image_id: int) -> None:
    """删掉正式图的缩略图缓存。

    裁剪 / 画质调整一改，这张缩略图的内容就**不再**是"永远不变"了，
    所以那两个端点写完库必须叫一下它。下一次请求会重新生成（一张几十毫秒）。
    """
    p = path_for(image_id)
    if p.is_file():
        p.unlink()
