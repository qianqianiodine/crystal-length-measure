"""样品孔检测 —— 只做**粗定位**，给用户一个可调整的起点圆。

使用场景：**单孔照片**（手机对着显微镜目镜拍一个孔）。画面里没有阵列、
没有参照物，所以本模块只负责猜一个大概位置，不追求像素级精度。

职责划分（2026-09-19 用户定案）：
- 自动检测**只给粗略起点**，不要求精度。
- **最终由用户手动确定圆**：拖动位置、缩放直径、旋转方位。
- 理由：孔的实体边界在模糊照片上没有唯一位置（同一张图不同算法差 30%，
  见 learnings.md），**机器猜不如人眼准**。既然猜不准，就把主动权交给用户。

因此本模块不做任何"反推比例尺"的自动标定。比例尺由**用户确认的那个圆**
（Φ3.4mm）换算 —— 见 app/calibrate.py。
"""
from dataclasses import dataclass

import cv2
import numpy as np

# 环形对比度的采样参数
N_ANGLES = 72
RING_GAP_PX = 6.0

# 搜索半径范围（占画面短边的比例）。单孔照片里孔通常占画面很大一块。
R_LO_RATIO = 0.08
R_HI_RATIO = 0.48

# 检测前把画面缩到这个尺寸以内。单孔照片动辄 1200 万像素，
# 而检测只需要"大概在哪"，降采样后快几十倍，精度损失对起点圆无所谓。
DETECT_MAX_SIDE = 420


@dataclass
class WellCandidate:
    """一个候选孔。radius_px 是孔壁半径，仅作起点用。"""

    center: tuple[float, float]
    radius_px: float
    score: float

    @property
    def diameter_px(self) -> float:
        return 2.0 * self.radius_px


def default_diameter_px(candidates: list[WellCandidate]) -> float:
    """取评分最高候选的直径，作为界面里起点圆的初始直径。"""
    if not candidates:
        raise ValueError("没有候选孔")
    return candidates[0].diameter_px


def crop_field_circle(img_rgb: np.ndarray,
                      min_keep_ratio: float = 0.55
                      ) -> tuple[np.ndarray, tuple[int, int, int] | None]:
    """裁掉目镜视场圆外的黑边/暗角。

    手机对着显微镜目镜拍摄时，画面四周常有大片黑色视场外区域，
    会干扰检测。这里找出中央亮区并裁到它的外接方形。

    Args:
        img_rgb: RGB 图像
        min_keep_ratio: 裁后边长若小于原图此比例则不裁（避免误裁正常图）
    Returns:
        (处理后的图, (cx, cy, r) 或 None)
    """
    h, w = img_rgb.shape[:2]
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)

    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    num, labels = cv2.connectedComponents(th)
    if num <= 1:
        return img_rgb, None
    center_label = labels[h // 2, w // 2]
    if center_label == 0:
        sizes = [(int((labels == i).sum()), i) for i in range(1, num)]
        if not sizes:
            return img_rgb, None
        center_label = max(sizes)[1]
    mask = (labels == center_label).astype(np.uint8) * 255

    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return img_rgb, None
    cnt = max(cnts, key=cv2.contourArea)
    (cx, cy), r = cv2.minEnclosingCircle(cnt)
    cx, cy, r = float(cx), float(cy), float(r)

    if 2 * r < min(h, w) * min_keep_ratio:
        return img_rgb, None        # 亮区太小，可能本就没有视场圆，不裁

    x0, y0 = max(0, int(cx - r)), max(0, int(cy - r))
    x1, y1 = min(w, int(cx + r)), min(h, int(cy + r))
    if x1 - x0 < 10 or y1 - y0 < 10:
        return img_rgb, None
    return img_rgb[y0:y1, x0:x1], (int(cx), int(cy), int(r))


# ---------------- 环形对比度 ----------------

def _radial_matrix(gray: np.ndarray, cx: float, cy: float, rmax: float,
                   step: float = 1.0
                   ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (半径数组, 亮度矩阵[角度,半径], 有效掩码[角度,半径])。

    掩码标出落在图像外的采样点 —— 圆靠近画面边缘时要用掩码平均，
    否则边缘像素被反复采样会把剖面带偏。
    """
    h, w = gray.shape
    rs = np.arange(0.0, rmax, step)
    angs = np.deg2rad(np.arange(0.0, 360.0, 360.0 / N_ANGLES))
    xs = cx + np.outer(np.cos(angs), rs)
    ys = cy + np.outer(np.sin(angs), rs)
    ok = (xs >= 0.0) & (xs <= w - 1.0) & (ys >= 0.0) & (ys <= h - 1.0)
    xi = np.clip(np.round(xs).astype(np.int32), 0, w - 1)
    yi = np.clip(np.round(ys).astype(np.int32), 0, h - 1)
    return rs, gray[yi, xi].astype(np.float32), ok


def _ring_contrast_rows(m: np.ndarray, ok: np.ndarray,
                        gap: int) -> np.ndarray:
    """逐行算环对比度：该半径比内外两侧亮多少。无效位置给 -1e9。"""
    n = m.shape[1]
    out = np.full(m.shape, -1e9, np.float32)
    if n > 2 * gap:
        left = m[:, :n - 2 * gap]
        right = m[:, 2 * gap:]
        center = m[:, gap:n - gap]
        good = ok[:, :n - 2 * gap] & ok[:, 2 * gap:] & ok[:, gap:n - gap]
        out[:, gap:n - gap] = np.where(good, center - 0.5 * (left + right),
                                       -1e9)
    return out


def _circle_at(gray: np.ndarray, cx: float, cy: float,
               r_lo: float, r_hi: float) -> tuple[float, float, float]:
    """评估以 (cx,cy) 为心的最佳孔壁。返回 (评分, 半径, 圆度)。

    评分 = 环对比度 / 圆度。除以圆度是为了让"更像圆"的候选占优 ——
    孔被拍斜时是椭圆，但不会变成别的形状。
    """
    gap = max(2, int(round(RING_GAP_PX)))
    rs, m, ok = _radial_matrix(gray, cx, cy, r_hi * 1.35 + gap)
    band = (rs >= r_lo) & (rs <= r_hi) & (ok.sum(axis=0) > N_ANGLES * 0.6)
    if not band.any():
        return -1e9, 0.0, 1.0

    rc = np.where(band, _ring_contrast_rows(m, ok, gap), -1e9)
    col = rc.mean(axis=0)
    if not np.isfinite(col[band]).any():
        return -1e9, 0.0, 1.0
    j = int(np.nanargmax(np.where(band, col, -np.inf)))
    r = float(rs[j])
    if r <= 0:
        return -1e9, 0.0, 1.0

    per = rs[np.nanargmax(np.where(ok, rc, -np.inf), axis=1)]
    roundness = (float(np.percentile(per, 75))
                 / max(float(np.percentile(per, 25)), 1e-6))
    return float(col[j]) / max(roundness, 1.0), r, roundness


def _best_circle(gray: np.ndarray, cx: float, cy: float,
                 r_lo: float, r_hi: float,
                 coarse_step: float = 4.0
                 ) -> tuple[float, float, float, float, float]:
    """在 (cx,cy) 附近搜最佳孔壁，返回 (评分, 精修x, 精修y, 半径, 圆度)。

    两级搜索：霍夫给的圆心可能偏向孔的一侧，偏离真圆心可达半个半径，
    所以粗搜范围开到 0.6×r_hi；但那样在全分辨率上太慢 ——
    粗搜在 1/4 分辨率上做，再回全分辨率细搜。
    """
    h, w = gray.shape
    k = 4.0
    small = cv2.resize(gray, (max(1, int(w / k)), max(1, int(h / k))),
                       interpolation=cv2.INTER_AREA)
    span = max(6.0, r_hi * 0.35)

    coarse = (-1e9, cx, cy)
    for dy in np.arange(-span, span + 0.1, coarse_step):
        for dx in np.arange(-span, span + 0.1, coarse_step):
            s, _r, _rd = _circle_at(small, (cx + dx) / k, (cy + dy) / k,
                                    r_lo / k, r_hi / k)
            if s > coarse[0]:
                coarse = (s, cx + dx, cy + dy)

    best = (-1e9, cx, cy, 0.0, 1.0)
    for dy in np.arange(-coarse_step, coarse_step + 0.1, 1.0):
        for dx in np.arange(-coarse_step, coarse_step + 0.1, 1.0):
            s, r, rnd = _circle_at(gray, coarse[1] + dx, coarse[2] + dy,
                                   r_lo, r_hi)
            if s > best[0]:
                best = (s, coarse[1] + dx, coarse[2] + dy, r, rnd)
    return best


# ---------------- 主检测 ----------------

def detect_wells(img_rgb: np.ndarray, max_candidates: int = 3,
                 expected_diameter_px: float | None = None
                 ) -> list[WellCandidate]:
    """粗略找出可能的孔，按评分降序返回。空列表表示没找到。

    结果是**给人做起点用的**，不保证像素级准确 —— 界面上允许用户
    拖动/缩放/旋转来定圆。

    Args:
        img_rgb: RGB 图像
        max_candidates: 最多返回几个候选
        expected_diameter_px: 已知的大致孔直径（像素），给了就据此收窄搜索范围
    返回的坐标在**输入图像**坐标系下。
    """
    if img_rgb.ndim != 3:
        raise ValueError("detect_wells 需要三通道图像")

    cropped, info = crop_field_circle(img_rgb)
    if info is not None:
        ox, oy = max(0, info[0] - info[2]), max(0, info[1] - info[2])
    else:
        ox, oy = 0, 0

    gray = cv2.cvtColor(cropped, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape

    # 降采样再检测：只需要"大概在哪"，缩到 420px 以内快几十倍
    scale = min(1.0, DETECT_MAX_SIDE / float(max(h, w)))
    if scale < 1.0:
        work = cv2.resize(gray, (max(1, int(round(w * scale))),
                                 max(1, int(round(h * scale)))),
                          interpolation=cv2.INTER_AREA)
    else:
        work = gray
    wh, ww = work.shape

    if expected_diameter_px is not None:
        r0 = expected_diameter_px * scale / 2.0
        r_lo, r_hi = max(4.0, r0 * 0.6), r0 * 1.6
    else:
        r_lo, r_hi = min(wh, ww) * R_LO_RATIO, min(wh, ww) * R_HI_RATIO

    clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8))
    g = cv2.GaussianBlur(clahe.apply(work), (5, 5), 0)
    found = cv2.HoughCircles(
        g, cv2.HOUGH_GRADIENT, dp=1.2,
        minDist=int(max(16.0, 2.0 * r_lo)),
        param1=100, param2=30,
        minRadius=int(r_lo), maxRadius=int(max(r_hi, r_lo + 2)))

    inv = 1.0 / scale
    raw: list[WellCandidate] = []
    if found is not None:
        for c in found[0][:8]:
            s, bx, by, r, _rnd = _best_circle(work, float(c[0]), float(c[1]),
                                              r_lo, r_hi)
            if r > 0 and s > 0:
                raw.append(WellCandidate(center=(bx * inv + ox, by * inv + oy),
                                         radius_px=r * inv, score=s))

    # 去重：圆心太近的只留评分最高的
    raw.sort(key=lambda c: -c.score)
    kept: list[WellCandidate] = []
    for c in raw:
        if all(np.hypot(c.center[0] - k.center[0], c.center[1] - k.center[1])
               > 1.2 * min(c.radius_px, k.radius_px) for k in kept):
            kept.append(c)
        if len(kept) >= max_candidates:
            break
    return kept
