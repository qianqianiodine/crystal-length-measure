"""视觉模型：从整张照片直接回归孔的起点圆（ONNX）。

2026-10-22/23 用用户 84 张手工标定做的实验：
- 模型单独：圆心误差中位 0.186 个半径、半径比 1.021；
- 前向约 10ms（原来规则检测约 5 秒）；
- 模型文件缺失 / onnxruntime 不可用 / 输出离谱 → 返回 None，由 bgdetect 回退 Hough。

2026-10-23 补：**围着第一遍的预测再裁一次、再跑一遍**。
用户报"局部孔准了，完整孔反而不准"——84 张里只有 3 张是完整孔，模型见得太少。
它训练时看的是随机方形裁切，推理时却只喂整图；补上两遍裁切后（裁 2.5 倍
半径，越界补黑，正是训练时的样子），84 张上圆心中位 0.177 → 0.127，
3 张完整孔 0.433 → 0.169；三张新拍的完整孔肉眼也明显回到孔上。
代价约 30～60ms，仍然是"快"的一档。

⚠️ 预处理必须和 tools/well_detector/train.py 完全一致：
   长边缩到 512 的黑色 letterbox → 缩到 256 → ImageNet 归一化。
   模型输出是 letterbox 画布坐标 / 512 的 (cx, cy, log a)。
"""
import math
import os
from functools import lru_cache

import cv2
import numpy as np

from app import config

MODEL_PATH = config.PROJECT_ROOT / "app" / "models" / "well_regressor.onnx"
CANVAS = 512
INPUT = 256
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)

# 第二遍裁切：以预测圆心为中心、边长 = 2.5 倍预测半径。
# 训练时模型看的就是这种"围着孔"的方形裁切；完整孔在整图上只占一小块，
# 模型容易把旁边的液滴/相邻孔当成目标，裁近一点就回到训练时的视角。
REFINE_SCALE = 2.5
REFINE_STEPS = 2


def _disabled() -> bool:
    """测试时设 JLTX_NO_DL=1，保证自动化测试可重复、不依赖模型文件。"""
    return os.environ.get("JLTX_NO_DL") == "1"


@lru_cache(maxsize=1)
def _session():
    if not MODEL_PATH.is_file():
        return None
    try:
        import onnxruntime as ort
        return ort.InferenceSession(str(MODEL_PATH),
                                    providers=["CPUExecutionProvider"])
    except Exception:
        return None


def available() -> bool:
    return (not _disabled()) and _session() is not None


def _letterbox(img: np.ndarray, size: int = CANVAS):
    h, w = img.shape[:2]
    s = size / float(max(h, w))
    nw, nh = max(1, int(round(w * s))), max(1, int(round(h * s)))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((size, size, 3), np.uint8)
    ox, oy = (size - nw) // 2, (size - nh) // 2
    canvas[oy:oy + nh, ox:ox + nw] = resized
    return canvas, s, ox, oy


def _infer(sess, canvas: np.ndarray):
    """跑一遍模型，返回画布坐标里的 (cx, cy, a)，单位都是画布像素。"""
    x = cv2.resize(canvas, (INPUT, INPUT), interpolation=cv2.INTER_AREA)
    x = (x.astype(np.float32) / 255.0 - MEAN) / STD
    x = np.transpose(x, (2, 0, 1))[None].copy()
    out = sess.run(None, {sess.get_inputs()[0].name: x})[0][0]
    return (float(out[0]) * CANVAS, float(out[1]) * CANVAS,
            math.exp(float(out[2])) * CANVAS)


def _crop_to_canvas(img: np.ndarray, cx: float, cy: float, side: float):
    """裁 side×side（越界补黑）并缩到 CANVAS。返回 (canvas, x0, y0, side)。

    只 resize 落在图内的那块，避免 a 很大时先分配一个超大的黑图。
    """
    h, w = img.shape[:2]
    side = int(round(side))
    if side < 8:
        return None
    x0 = int(round(cx - side / 2.0))
    y0 = int(round(cy - side / 2.0))
    sx0, sy0 = max(0, x0), max(0, y0)
    sx1, sy1 = min(w, x0 + side), min(h, y0 + side)
    if sx1 <= sx0 or sy1 <= sy0:
        return None
    dx0 = int(round((sx0 - x0) * CANVAS / side))
    dy0 = int(round((sy0 - y0) * CANVAS / side))
    dx1 = max(dx0 + 1, min(CANVAS, int(round((sx1 - x0) * CANVAS / side))))
    dy1 = max(dy0 + 1, min(CANVAS, int(round((sy1 - y0) * CANVAS / side))))
    canvas = np.zeros((CANVAS, CANVAS, 3), np.uint8)
    canvas[dy0:dy1, dx0:dx1] = cv2.resize(
        img[sy0:sy1, sx0:sx1], (dx1 - dx0, dy1 - dy0),
        interpolation=cv2.INTER_AREA)
    return canvas, x0, y0, side


def _plausible(cx: float, cy: float, a: float, w: int, h: int) -> bool:
    if not all(math.isfinite(v) for v in (cx, cy, a)):
        return False
    short = float(min(h, w))
    # 手工圆的半径占短边 0.228～0.655；留一点余量。
    if not (0.05 * short <= a <= 1.05 * short):
        return False
    return -0.5 * w <= cx <= 1.5 * w and -0.5 * h <= cy <= 1.5 * h


def predict(img_rgb: np.ndarray) -> dict | None:
    """返回**原图坐标**的起点圆；不可用或不可信时返回 None。"""
    if _disabled():
        return None
    sess = _session()
    if sess is None:
        return None
    try:
        h, w = img_rgb.shape[:2]
        canvas, s, ox, oy = _letterbox(img_rgb)
        cx, cy, a = _infer(sess, canvas)
        cx, cy, a = (cx - ox) / s, (cy - oy) / s, a / s

        # 围着预测再裁两遍：把完整孔从整图里"拉近"到训练时的尺寸。
        for _ in range(REFINE_STEPS):
            if not _plausible(cx, cy, a, w, h):
                break
            built = _crop_to_canvas(img_rgb, cx, cy, REFINE_SCALE * a)
            if built is None:
                break
            crop, x0, y0, side = built
            px, py, pa = _infer(sess, crop)
            ncx = x0 + px / CANVAS * side
            ncy = y0 + py / CANVAS * side
            na = pa / CANVAS * side
            if not _plausible(ncx, ncy, na, w, h):
                break
            cx, cy, a = ncx, ncy, na

        if not _plausible(cx, cy, a, w, h):
            return None
        return {"cx": cx, "cy": cy, "a": a, "b": a, "th": 0.0, "auto": True}
    except Exception:                       # noqa: BLE001 —— 模型出岔子不能挡住打开
        return None
