"""图像质量评估与传统增强。

⚠️ 关键不变式：所有增强操作**不得改变图像尺寸**。
尺寸一变，标定系数立刻失效，所有测量值都错。
深度增强（超分）因此被排除在本阶段之外（见设计文档 §5.4）。

增强不改坐标，所以对变换链透明 —— 见 app/transform.py 的说明。
"""
import json
from dataclasses import dataclass

import cv2
import numpy as np


def assess_quality(img_rgb: np.ndarray) -> dict:
    """评估图像质量，返回指标与中文提示。

    Args:
        img_rgb: RGB 三通道图像
    Returns:
        {
          "blur": 拉普拉斯方差（越大越清晰）,
          "contrast": 灰度标准差,
          "noise": 噪声估计（高频残差标准差）,
          "illumination": 光照不均程度（背景梯度幅值）,
          "warnings": ["对比度偏低，建议增强", ...]
        }
    """
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    gray_f = gray.astype(np.float32)

    # 模糊度：拉普拉斯方差
    blur = float(cv2.Laplacian(gray_f, cv2.CV_32F).var())

    # 对比度：灰度标准差
    contrast = float(gray_f.std())

    # 噪声：原图与中值滤波之差的标准差（高频部分）
    med = cv2.medianBlur(gray, 3).astype(np.float32)
    noise = float(np.abs(gray_f - med).std())

    # 光照不均：大尺度背景的梯度幅值
    bg = cv2.GaussianBlur(gray_f, (0, 0), sigmaX=32.0)
    gx = cv2.Sobel(bg, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(bg, cv2.CV_32F, 0, 1, ksize=3)
    illumination = float(np.hypot(gx, gy).mean())

    warnings: list[str] = []
    if contrast < 20:
        warnings.append("检测到对比度偏低，建议增强")
    if blur < 60:
        warnings.append("图像可能偏模糊，建议轻锐化")
    if noise > 18:
        warnings.append("检测到噪声较大，建议降噪")
    if illumination > 12:
        warnings.append("检测到光照不均匀，建议平衡光照")

    return {"blur": blur, "contrast": contrast, "noise": noise,
            "illumination": illumination, "warnings": warnings}


@dataclass
class EnhanceParams:
    """增强参数。三类处理都可通过参数设为无效。"""

    clahe_clip: float = 2.0        # 对比度增强强度，0 = 关闭
    denoise_h: float = 5.0         # 降噪强度，0 = 关闭
    sharpen_amount: float = 0.6    # 锐化强度，0 = 关闭
    illum_balance: bool = False    # 是否做光照均衡


CLIP_LIMIT = 8.0        # CLAHE clipLimit 上限，防止过度增强
DENOISE_H_MAX = 25.0    # 降噪强度上限，超过会明显拖慢且糊细节


def auto_params(report: dict) -> EnhanceParams:
    """根据质量评估结果自动选参数。"""
    clahe = 2.0
    if report.get("contrast", 60.0) < 20:
        clahe = 4.0
    elif report.get("contrast", 60.0) < 35:
        clahe = 3.0

    denoise = 0.0
    if report.get("noise", 0.0) > 18:
        denoise = 8.0
    elif report.get("noise", 0.0) > 10:
        denoise = 4.0

    sharpen = 0.6
    if report.get("blur", 500.0) < 60:
        sharpen = 1.0

    return EnhanceParams(
        clahe_clip=min(clahe, CLIP_LIMIT),
        denoise_h=min(denoise, DENOISE_H_MAX),
        sharpen_amount=sharpen,
        illum_balance=report.get("illumination", 0.0) > 12,
    )


def apply_enhance(img_rgb: np.ndarray, params: EnhanceParams) -> np.ndarray:
    """应用增强。保证输出尺寸与 dtype 与输入一致。"""
    if img_rgb.ndim != 3 or img_rgb.shape[2] != 3:
        raise ValueError("apply_enhance 需要 RGB 三通道图像")

    out = img_rgb.astype(np.uint8).copy()

    # 1) 降噪（先降噪再增强，避免把噪声一起放大）
    if params.denoise_h > 0:
        h = float(min(params.denoise_h, DENOISE_H_MAX))
        out = cv2.fastNlMeansDenoisingColored(out, None, h, h, 7, 21)

    # 2) 光照均衡（形态学开运算估背景后相减）
    if params.illum_balance:
        gray = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY)
        k = max(15, (min(out.shape[:2]) // 20) | 1)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        bg = cv2.morphologyEx(gray, cv2.MORPH_OPEN, kernel)
        bg = cv2.GaussianBlur(bg, (0, 0), sigmaX=k / 2.0)
        bg3 = cv2.cvtColor(bg, cv2.COLOR_GRAY2RGB).astype(np.float32)
        out = np.clip(out.astype(np.float32) - bg3 + 128.0, 0, 255).astype(np.uint8)

    # 3) 对比度 CLAHE（在 LAB 的 L 通道做，避免偏色）
    if params.clahe_clip > 0:
        clip = float(min(params.clahe_clip, CLIP_LIMIT))
        lab = cv2.cvtColor(out, cv2.COLOR_RGB2LAB)
        l_ch, a_ch, b_ch = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8))
        l_ch = clahe.apply(l_ch)
        out = cv2.cvtColor(cv2.merge([l_ch, a_ch, b_ch]), cv2.COLOR_LAB2RGB)

    # 4) 锐化 USM
    if params.sharpen_amount > 0:
        amount = float(params.sharpen_amount)
        blurred = cv2.GaussianBlur(out, (0, 0), sigmaX=1.5)
        out = cv2.addWeighted(out, 1.0 + amount, blurred, -amount, 0.0)

    # 尺寸契约：若尺寸变了说明实现有 bug，直接报错而不是静默返回错图
    if out.shape != img_rgb.shape:
        raise AssertionError(
            f"增强改变了图像尺寸：{img_rgb.shape} → {out.shape}（会破坏标定）")
    return out


def enhance_for_preview(img_rgb: np.ndarray, params: EnhanceParams,
                        max_side: int = 1200) -> np.ndarray:
    """预览用增强：先缩到 max_side 以内再处理，保证拖动滑块时的响应速度。

    ⚠️ 仅用于界面预览，不可用于任何测量计算。
    """
    h, w = img_rgb.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return apply_enhance(img_rgb, params)
    k = max_side / float(longest)
    small = cv2.resize(img_rgb, (max(1, int(w * k)), max(1, int(h * k))),
                       interpolation=cv2.INTER_AREA)
    return apply_enhance(small, params)


# 画质调整（亮度/对比度/饱和度）。⚠️ 取值范围必须和 web/measure.html 的滑块一致，
# 不然界面能拖到的值和后端存下来的值对不上。
ENHANCE_RANGE = {"b": (0.5, 2.0), "c": (0.5, 2.5), "s": (0.0, 2.0)}
ENHANCE_NEUTRAL = {"b": 1.0, "c": 1.0, "s": 1.0}


def parse_enhance(row) -> dict:
    """这张图的画质调整参数。没调过、或者存档坏了，一律回中性值。

    返回的是**填满中性值的 dict**，不是 None —— 前端拿它直接喂滑块。

    ⚠️ 它只读 `row["enhance"]` **这一个键**，所以传个普通字典
    `{"enhance": raw}` 也行 —— thumbs.py 手上只有列里的原始字符串、没有 Row。
    """
    try:
        d = json.loads(row["enhance"]) if row["enhance"] else {}
    except ValueError:
        d = {}
    if not isinstance(d, dict):
        d = {}
    out = {}
    for k, v in ENHANCE_NEUTRAL.items():
        try:
            out[k] = float(d.get(k, v))
        except (TypeError, ValueError):    # 单项是乱码/null → 只这一项回中性值
            out[k] = v
    return out


# 亮度系数（Rec.709），和浏览器 saturate() 用的是同一组
_LUMA = np.array([0.213, 0.715, 0.072], np.float32)


def apply_tone(img_rgb: np.ndarray, enh: dict) -> np.ndarray:
    """把测量页那三个滑块（亮度 b / 对比度 c / 饱和度 s）烘进像素。

    ⚠️⚠️ 必须和 **web/measure.html 里屏幕上的样子逐像素一致** —— 这是
    「同一件事实」的两半。改这里之前先看 tests/test_enhance.py 的
    test_apply_tone_和浏览器算的一模一样，那条会真的去跑 node 比对。

    顺序：伽马曲线（亮度）→ CSS contrast（对比度）→ CSS saturate（饱和度）。
    和浏览器 ctx.filter 的 `contrast(c) saturate(s)` 顺序一致 ——
    三个都是非线性运算，顺序换一下结果就不一样。
    """
    b = float(enh.get("b", 1.0))
    c = float(enh.get("c", 1.0))
    s = float(enh.get("s", 1.0))
    if (b, c, s) == (1.0, 1.0, 1.0):
        return img_rgb                        # 原样，一个像素都不动

    out = img_rgb
    if b != 1.0:
        # 和 measure.html:988 的 toneLut 是同一条式子：L[i] = 255 * (i/255) ** (1/b)
        lut = np.clip(np.round(
            255.0 * (np.arange(256) / 255.0) ** (1.0 / b)), 0, 255).astype(np.uint8)
        out = lut[out]

    x = out.astype(np.float32) / 255.0
    if c != 1.0:
        x = c * (x - 0.5) + 0.5                       # CSS contrast(k)
    if s != 1.0:
        luma = x @ _LUMA                              # 按行点积 = 每个像素一个灰度值
        x = luma[..., None] * (1.0 - s) + x * s       # CSS saturate(k) 的矩阵
    return np.clip(np.round(x * 255.0), 0, 255).astype(np.uint8)
