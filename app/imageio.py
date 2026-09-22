"""Windows 中文路径安全的图像读写。

⚠️ 核心坑：cv2.imread / cv2.imwrite 在 Windows 下遇到非 ASCII 路径
会静默失败（imread 返回 None，imwrite 返回 False 且不抛异常）。
本模块是项目内**唯一**允许做图像 IO 的地方。

原理：用 np.fromfile 把文件读成 uint8 字节流，交给 cv2.imdecode。
写图则 cv2.imencode 到内存缓冲，再用 ndarray.tofile 落盘。
"""
from pathlib import Path

import cv2
import numpy as np


class ImageLoadError(Exception):
    """图像无法读取时抛出，附带用户可读的中文原因。"""


def imread_unicode(path: str | Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    """中文路径安全读图。读不出返回 None（宽松，供探测用）。"""
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if buf.size == 0:
        return None
    img = cv2.imdecode(buf, flags)
    return img


def imwrite_unicode(path: str | Path, img: np.ndarray) -> bool:
    """中文路径安全写图。返回是否成功。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ext = p.suffix if p.suffix else ".png"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        return False
    buf.tofile(str(p))
    return True


def load_rgb(path: str | Path) -> np.ndarray:
    """读图并保证返回 3 通道 RGB。失败抛 ImageLoadError。"""
    p = Path(path)
    if not p.exists():
        raise ImageLoadError(f"文件不存在：{p.name}")
    img = imread_unicode(p, cv2.IMREAD_COLOR)   # 强制 3 通道 BGR
    if img is None:
        raise ImageLoadError(f"文件读取失败，可能不是有效图像：{p.name}")
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def save_rgb(path: str | Path, img: np.ndarray) -> None:
    """保存 RGB 图像。失败抛 ImageLoadError。"""
    if img.ndim == 3 and img.shape[2] == 3:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    else:
        bgr = img
    if not imwrite_unicode(path, bgr):
        raise ImageLoadError(f"图像保存失败：{Path(path).name}")
