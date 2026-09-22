"""标定计算。

标定基准 = **用户确认的那个圆**（Φ3.4mm）。用户在界面上拖动/缩放/旋转
定出孔，我们只把它换算成比例尺 —— 机器不猜孔在哪（见 detect.py 的说明）。

两条路径：
1. 孔直径（主路径）→ from_well_diameter / from_well_ellipse
2. 手动拉线（兜底）→ from_manual_line

两者都产出同一个 CalibResult，由 apply_calibration 统一落库。

⚠️ **所有像素长度都必须在「校正坐标系」下计算。**
标定系数是在校正后的图（正圆孔）上量出来的，测量也必须在同一坐标系，
否则两者不自洽 —— 详见 app/transform.py。

⚠️ 孔被拍成椭圆时**取长轴**换算：透视只压缩一个方向，长轴对应真实的圆直径。
副作用：沿短轴方向量出来的长度会偏小（偏小比例 = 短轴/长轴）。
想彻底消除就先用四角校正把图拉正，那时孔就是正圆了。
"""
from dataclasses import dataclass

from app import settings
from app.transform import apply_h, line_length

CALIB_METHODS = {
    "well_diameter": "孔直径（界面上定圆）",
    "manual": "手动拉线",
}


@dataclass
class CalibResult:
    """标定结果。um_per_px 是派生值，不由调用方传。"""

    method: str
    pixel_length: float
    real_length_um: float
    um_per_px: float

    def __post_init__(self) -> None:
        if self.pixel_length <= 0:
            raise ValueError("像素长度必须大于 0")
        if self.real_length_um <= 0:
            raise ValueError("实际长度必须大于 0")


def make_result(method: str, pixel_length: float, real_um: float) -> CalibResult:
    """由方法名与两个长度构造标定结果。

    公开函数：API 层需要按 method 字符串统一构造，
    不应跨越模块边界调用私有函数。
    """
    if method not in CALIB_METHODS:
        raise ValueError(f"未知的标定方式：{method}")
    if pixel_length <= 0:
        raise ValueError("像素长度必须大于 0，请重新定圆或拉线")
    if real_um <= 0:
        raise ValueError("实际长度必须大于 0")
    return CalibResult(
        method=method,
        pixel_length=float(pixel_length),
        real_length_um=float(real_um),
        um_per_px=float(real_um) / float(pixel_length),
    )


def corrected_pixel_length(x1: float, y1: float, x2: float, y2: float,
                           h=None) -> float:
    """在校正坐标系下计算线段像素长度。h 为 None 时等同于直接算。"""
    if h is not None:
        x1, y1 = apply_h(h, x1, y1)
        x2, y2 = apply_h(h, x2, y2)
    return line_length(x1, y1, x2, y2)


# ---------------- 主路径：用户在界面上定的孔 ----------------

def _real_um(real_um: float | None) -> float:
    """没显式给实际尺寸就现读用户设的孔直径。

    ⚠️ **绝不能写成默认参数。** 见下面 from_well_diameter 的说明。
    """
    return settings.well_diameter_um() if real_um is None else float(real_um)


def from_well_diameter(diameter_px: float,
                       real_um: float | None = None
                       ) -> CalibResult:
    """按孔直径标定。

    diameter_px 应为**长轴**像素长度 —— 孔被拍成椭圆时，
    长轴才对应真实的圆直径。

    ⚠️ `real_um` 刻意**不做成默认参数** `= config.WELL_DIAMETER_UM`：
    Python 的默认值**只在函数定义那一刻求值一次**，用户在界面上把孔直径
    改掉之后它纹丝不动 —— 会按老直径静默算错，而且一句报错都没有。
    必须每次现读（走 app/settings.py）。
    """
    return make_result("well_diameter", diameter_px, _real_um(real_um))


def from_well_ellipse(major_px: float, minor_px: float,
                      real_um: float | None = None
                      ) -> CalibResult:
    """按用户定出的椭圆标定：取**长轴**，短轴不参与换算。

    两个参数传反了也没关系 —— 内部取大的那个当长轴。
    `real_um` 同样必须现读，理由见 from_well_diameter。
    """
    return make_result("well_diameter", max(float(major_px),
                                            float(minor_px)),
                       _real_um(real_um))


def from_manual_line(x1: float, y1: float, x2: float, y2: float,
                     real_um: float, h=None) -> CalibResult:
    """按手动拉的线标定（兜底路径）。"""
    px = corrected_pixel_length(x1, y1, x2, y2, h)
    return make_result("manual", px, real_um)


# ---------------- 落库与重算 ----------------

def apply_calibration(conn, image_id: int, result: CalibResult,
                      circle: str | None = None) -> None:
    """把标定结果落库。circle 为用户定的那个圆（JSON 字符串，原样存）。"""
    from app import db

    db.set_calibration(conn, image_id, result.method,
                       result.pixel_length, result.real_length_um,
                       circle=circle)


def load_h(image_row) -> list | None:
    """从 images.transform 里取出透视矩阵。没有或坏了都返回 None。

    公开函数：measure.py 也要用它，不该各写一份。
    """
    import json

    if image_row is None or not image_row["transform"]:
        return None
    try:
        return json.loads(image_row["transform"]).get("H")
    except (ValueError, TypeError):
        return None


def recompute_measurements(conn, image_id: int) -> int:
    """按当前标定重算该图所有测量值，返回更新条数。

    ★ 这是「非破坏性变换链」的核心价值体现：用户重标定后，
    已有的测量线不需要重画，数值自动跟着更新。

    端点是原图坐标，需经该图的透视校正 h 变换后算长度。
    """
    from app import db
    from app.transform import measurement_um

    calib = db.get_calibration(conn, image_id)
    if calib is None:
        raise ValueError("该图像尚未标定，无法重算测量值")

    h = load_h(db.get_image(conn, image_id))
    um_per_px = float(calib["um_per_px"])

    rows = db.list_measurements(conn, image_id)
    for m in rows:
        px, um = measurement_um(m["x1"], m["y1"], m["x2"], m["y2"],
                                um_per_px, h)
        conn.execute(
            "UPDATE measurements SET pixel_length=?, measured_um=?, "
            "calib_snapshot=? WHERE id=?",
            (px, um, um_per_px, m["id"]),
        )
    conn.commit()
    return len(rows)
