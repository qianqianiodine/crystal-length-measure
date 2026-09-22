"""坐标变换链 —— 本项目正确性的根基。

术语：
- **原图坐标**：原始图像素坐标，测量线端点一律存这个坐标系
- **显示坐标**：屏幕上看到的坐标 = 原图坐标先经透视校正 H、再缩放平移
- **校正坐标**：原图坐标经 H 变换后的坐标

变换顺序（正向 = 原图 → 屏幕）：
    校正坐标 = H · 原图坐标
    显示坐标 = 校正坐标 × scale + (tx, ty)

测量数值一律在**校正坐标**下计算：
    pixel_length = |P2' - P1'|     其中 P' = H · P
    measured_um  = pixel_length × um_per_px

若 H 为 None（未做透视校正），校正坐标 == 原图坐标。
"""
from dataclasses import dataclass

import cv2
import numpy as np


def line_length(x1: float, y1: float, x2: float, y2: float) -> float:
    """两点欧氏距离（像素）。"""
    return float(np.hypot(x2 - x1, y2 - y1))


def px_to_um(pixel_length: float, um_per_px: float) -> float:
    """像素长度换算成微米。"""
    return float(pixel_length) * float(um_per_px)


def build_homography(src_pts, dst_pts) -> np.ndarray:
    """由 4 组对应点求单应矩阵。

    Args:
        src_pts, dst_pts: shape (4, 2) 的 float32 数组
    Returns:
        3×3 float64 矩阵
    Raises:
        ValueError: 点数不为 4，或四点退化（共线/重合）导致无解
    """
    src = np.asarray(src_pts, dtype=np.float32).reshape(-1, 2)
    dst = np.asarray(dst_pts, dtype=np.float32).reshape(-1, 2)
    if src.shape[0] != 4 or dst.shape[0] != 4:
        raise ValueError("需要恰好 4 组对应点")
    # 退化检查：任一点集围成的面积过小则无解
    if abs(cv2.contourArea(src.reshape(-1, 1, 2))) < 1e-6:
        raise ValueError("源四点退化（共线或重合），无法计算校正变换")
    if abs(cv2.contourArea(dst.reshape(-1, 1, 2))) < 1e-6:
        raise ValueError("目标四点退化（共线或重合），无法计算校正变换")
    H = cv2.getPerspectiveTransform(src, dst)
    if not np.all(np.isfinite(H)):
        raise ValueError("变换矩阵求解失败（含非法值）")
    return H.astype(np.float64)


def invert_h(H: np.ndarray) -> np.ndarray:
    """求逆矩阵。"""
    return np.linalg.inv(np.asarray(H, dtype=np.float64))


def apply_h(H: np.ndarray, x: float, y: float) -> tuple[float, float]:
    """把点从原图坐标变到校正坐标。"""
    H = np.asarray(H, dtype=np.float64)
    v = H @ np.array([x, y, 1.0])
    if abs(v[2]) < 1e-12:
        raise ValueError("变换后齐次坐标为零，该点不可映射")
    return float(v[0] / v[2]), float(v[1] / v[2])


def apply_h_inv(H_inv: np.ndarray, x: float, y: float) -> tuple[float, float]:
    """把点从校正坐标变回原图坐标。"""
    return apply_h(H_inv, x, y)


@dataclass
class DisplayTransform:
    """原图坐标 ↔ 屏幕显示坐标 的双向映射。

    scale 必须 > 0。H 为 None 表示不做透视校正。
    """

    scale: float
    tx: float
    ty: float
    H: np.ndarray | None = None

    def __post_init__(self) -> None:
        if self.scale <= 0:
            raise ValueError("缩放系数必须大于 0")
        if self.H is not None:
            self.H = np.asarray(self.H, dtype=np.float64)
            self._H_inv = invert_h(self.H)
        else:
            self._H_inv = None

    def original_to_display(self, x: float, y: float) -> tuple[float, float]:
        if self._H_inv is not None:
            x, y = apply_h(self.H, x, y)
        return x * self.scale + self.tx, y * self.scale + self.ty

    def display_to_original(self, x: float, y: float) -> tuple[float, float]:
        cx = (x - self.tx) / self.scale
        cy = (y - self.ty) / self.scale
        if self._H_inv is not None:
            cx, cy = apply_h_inv(self._H_inv, cx, cy)
        return cx, cy


def measurement_um(x1: float, y1: float, x2: float, y2: float,
                   um_per_px: float,
                   H: np.ndarray | None = None) -> tuple[float, float]:
    """由原图坐标的端点计算 (像素长度, 微米长度)。

    有透视校正时，长度须在**校正坐标**下计算 —— 因为标定系数
    是在校正后的图（正圆孔）上得到的，两者必须同一坐标系。
    """
    if H is not None:
        x1, y1 = apply_h(H, x1, y1)
        x2, y2 = apply_h(H, x2, y2)
    px = line_length(x1, y1, x2, y2)
    return px, px_to_um(px, um_per_px)


def ellipse_axes_from_H(H: np.ndarray, axes_px: tuple[float, float],
                        angle_deg: float) -> tuple[float, float]:
    """把椭圆经 H 变换后的长短轴长度算出来。

    用途：判断校正效果——校正恰当的话，原本的椭圆应趋近正圆
    （长短轴比接近 1）。

    Args:
        H: 校正变换矩阵
        axes_px: 椭圆原长短轴像素长度 (d1, d2)
        angle_deg: 椭圆旋转角（度），cv2.fitEllipse 的第三个返回值
    Returns:
        (变换后长轴, 变换后短轴)，长轴 >= 短轴
    """
    H = np.asarray(H, dtype=np.float64)
    a, b = axes_px[0] / 2.0, axes_px[1] / 2.0
    th = np.deg2rad(angle_deg)
    # 参数化椭圆周上的点
    t = np.linspace(0, 2 * np.pi, 361)
    ex = a * np.cos(t)
    ey = b * np.sin(t)
    # 旋转到椭圆自身朝向
    rx = ex * np.cos(th) - ey * np.sin(th)
    ry = ex * np.sin(th) + ey * np.cos(th)
    # 施加单应变换
    ones = np.ones_like(rx)
    pts = np.vstack([rx, ry, ones])
    tp = H @ pts
    tx = tp[0] / tp[2]
    ty = tp[1] / tp[2]
    # 用离心率最大的两点近似长短轴
    d = np.hypot(tx - tx.mean(), ty - ty.mean())
    long_ax = 2.0 * d.max()
    # 短轴：取与长轴方向垂直方向上的最大跨度
    # 简化做法：用二维协方差特征值
    cov = np.cov(np.vstack([tx, ty]))
    ev = np.linalg.eigvalsh(cov)
    ev = np.sort(np.abs(ev))[::-1]
    short_ax = 2.0 * np.sqrt(ev[1]) * np.sqrt(2.0)
    return float(long_ax), float(short_ax)
