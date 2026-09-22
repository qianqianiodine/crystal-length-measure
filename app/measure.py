"""测量记录管理。

返回值统一为 dict（而非 sqlite3.Row），便于直接 JSON 序列化给前端。

设计要点：
- 添加测量前必须已标定，否则报错（对应需求文档 3.9「未标定就测量」）
- 端点存原图坐标，长度经该图透视校正 H 后计算
- `calib_snapshot` 记录本次算数用的系数，保证历史可追溯
- `delete_line` **不重排 seq** —— 保持历史编号，避免用户记录错乱
"""
from app import db
from app.calibrate import load_h
from app.transform import measurement_um

# 测量线配色：高饱和度、在灰度显微图上都醒目，且两两区分度高
LINE_COLORS = [
    "#FF2D55",   # 品红
    "#00C7BE",   # 青
    "#FF9500",   # 橙
    "#5E5CE6",   # 靛蓝
    "#FFD60A",   # 黄
    "#30D158",   # 绿
    "#BF5AF2",   # 紫
    "#FF6482",   # 珊瑚
]

DEFAULT_PRECISION_TARGET_PCT = 2.0


def color_for_seq(seq: int) -> str:
    """按测量编号取颜色，循环使用。"""
    return LINE_COLORS[(seq - 1) % len(LINE_COLORS)]


def _to_dict(row) -> dict:
    return {
        "id": row["id"],
        "image_id": row["image_id"],
        "seq": row["seq"],
        "x1": row["x1"], "y1": row["y1"],
        "x2": row["x2"], "y2": row["y2"],
        "pixel_length": row["pixel_length"],
        "measured_um": row["measured_um"],
        "calib_snapshot": row["calib_snapshot"],
        "note": row["note"],
        "created_time": row["created_time"],
        "label_dx": row["label_dx"],
        "label_dy": row["label_dy"],
        "color": color_for_seq(row["seq"]),
    }


def add_line(conn, image_id: int, x1: float, y1: float, x2: float, y2: float,
             note: str = "") -> dict:
    """新增一条测量线。返回完整记录 dict。

    Raises:
        ValueError: 未标定，或线段长度为 0
    """
    calib = db.get_calibration(conn, image_id)
    if calib is None:
        raise ValueError("请先完成标定")

    h = load_h(db.get_image(conn, image_id))
    um_per_px = float(calib["um_per_px"])
    px, um = measurement_um(x1, y1, x2, y2, um_per_px, h)

    if px <= 0:
        raise ValueError("测量线长度不能为 0，请重新绘制")

    mid = db.add_measurement(conn, image_id, x1, y1, x2, y2, px, um,
                             um_per_px, note)
    row = conn.execute("SELECT * FROM measurements WHERE id=?",
                       (mid,)).fetchone()
    return _to_dict(row)


def list_lines(conn, image_id: int) -> list[dict]:
    return [_to_dict(r) for r in db.list_measurements(conn, image_id)]


def delete_line(conn, meas_id: int) -> None:
    db.delete_measurement(conn, meas_id)


def undo_last(conn, image_id: int) -> bool:
    """撤销最近一条测量。无记录返回 False。"""
    rows = db.list_measurements(conn, image_id)
    if not rows:
        return False
    db.delete_measurement(conn, rows[-1]["id"])
    return True


def clear_all(conn, image_id: int) -> int:
    """清空该图所有测量。返回删除条数。"""
    rows = db.list_measurements(conn, image_id)
    for r in rows:
        db.delete_measurement(conn, r["id"])
    return len(rows)


def update_note(conn, meas_id: int, note: str) -> None:
    conn.execute("UPDATE measurements SET note=? WHERE id=?", (note, meas_id))
    conn.commit()


def update_label(conn, meas_id: int, dx: float, dy: float) -> None:
    """挪动长度标签的位置。dx/dy 是相对线段中点的偏移（原图像素）。"""
    db.set_label_offset(conn, meas_id, dx, dy)


def meets_precision_target(measured_um: float, reference_um: float,
                           target_pct: float = DEFAULT_PRECISION_TARGET_PCT
                           ) -> bool:
    """判断测量值是否落在精度目标内（默认 ±2%）。

    供第一阶段验收用：与游标卡尺读数比对。
    """
    if reference_um <= 0:
        raise ValueError("参考值必须大于 0")
    return abs(measured_um - reference_um) / reference_um * 100.0 <= target_pct
