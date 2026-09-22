"""FastAPI 应用入口 + 试用页接口。

⚠️ 这是**试用页**用的接口，不是计划里的正式 Task 11/12 API。
它故意做得简陋：照片按原路径引用（不拷贝进 data/）、没有缩略图、
没有导入来源管理。目的是让用户能马上在浏览器里拖圆、拉线、看数值。

所有端点都是同步 def —— FastAPI 会把它们丢进线程池，
所以粗定位那几秒不会卡住别的请求。

坐标约定：本阶段还没有透视校正，h 恒为 None，
因此「校正坐标 == 原图坐标」，圆和测量线都直接存原图像素。
"""
import ipaddress
import json
import math
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import cv2
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import config, db, detect, enhance, exporter, imageio, measure, settings, storage, thumbs
from app.api import folders as folders_api
from app.api import images as images_api
from app.api import imports as imports_api
from app.api import mobile as mobile_api
from app.calibrate import apply_calibration, from_well_ellipse, recompute_measurements
from app.db import get_db

SAMPLES_ROOT = config.PROJECT_ROOT / "样品集"

# TestClient 报的来源就是这个串。uvicorn 报的永远是数字 IP，网上伪造不出来。
LOCAL_NAMES = frozenset({"testclient"})


def _is_local(request: Request) -> bool:
    """请求是不是本机来的。判不出来一律当外部（fail closed）。"""
    peer = request.client
    if peer is None:                       # ASGI 允许 client 为 None
        return False
    try:
        return ipaddress.ip_address(peer.host).is_loopback   # 覆盖整个 127/8 + ::1
    except ValueError:                     # 只可能是 "testclient" 这种名字
        return peer.host in LOCAL_NAMES


@asynccontextmanager
async def lifespan(_app: FastAPI):
    conn = db.get_conn()
    db.init_db(conn)
    conn.close()
    app.state.mobile_token = storage.mobile_token()
    app.state.mobile_on = False            # 局域网默认关着，用户在界面上开
    app.state.mobile_off_at = 0.0
    # launch.py 用环境变量把实际端口传进来（端口是它挑的）。
    # 没传就用 8510 —— 和 launch.py 的 PREFERRED_PORT 一致。
    app.state.port = int(os.environ.get("JLTX_PORT", 8510))
    yield


app = FastAPI(title="晶体长度测量工具", lifespan=lifespan)
config.ensure_dirs()
app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
app.include_router(images_api.router)
app.include_router(folders_api.router)
app.include_router(mobile_api.router)
app.include_router(imports_api.router)     # 电脑：/api/imports
app.include_router(imports_api.phone)      # 手机：/m/{token}/imports（守卫只放行这个前缀）


@app.middleware("http")
async def lan_guard(request: Request, call_next):
    """局域网守卫。

    服务绑在 0.0.0.0 上（手机要连），所以同一 WiFi 下的人都能找到这个端口。
    默认**全部拒绝**，只在"用户开了手机传图"且"路径正好是那个带令牌的上传页"时放行。
    忘了开、令牌过期、路径拼错 —— 结果都是进不来。
    """
    if _is_local(request):
        return await call_next(request)

    root = f"/m/{getattr(request.app.state, 'mobile_token', '')}"
    path = request.url.path                 # 已解码，不含 query
    if (getattr(request.app.state, "mobile_on", False)
            and time.time() < request.app.state.mobile_off_at
            and (path == root or path.startswith(root + "/"))):
        return await call_next(request)

    return JSONResponse({"detail": "这个地址只能在电脑上打开"}, status_code=403)


def _require_image(conn, image_id: int):
    row = db.get_image(conn, image_id)
    if row is None:
        raise HTTPException(404, "找不到这张图，可能记录被删了")
    return row


# ---------------- 照片列表 ----------------

@app.get("/api/photos")
def list_photos() -> dict:
    """列出样品集里的照片，供试用页选择。"""
    if not SAMPLES_ROOT.exists():
        return {"photos": []}
    photos = [
        {"rel": p.relative_to(SAMPLES_ROOT).as_posix(), "name": p.name}
        for p in sorted(SAMPLES_ROOT.rglob("*"))
        if p.is_file() and p.suffix.lower() in config.SUPPORTED_EXTS
    ]
    return {"photos": photos}


def _resolve_sample(rel: str) -> Path:
    """把相对路径限定在样品集目录内，防止越界读到别的文件。"""
    root = SAMPLES_ROOT.resolve()
    p = (root / rel).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        raise HTTPException(404, "找不到这张照片")
    return p


# ---------------- 打开照片 ----------------

class PhotoIn(BaseModel):
    rel: str | None = None
    image_id: int | None = None


def _rough_circle(path: Path, width: int, height: int) -> dict:
    """粗定位一个起点圆。检不出就给个居中的默认圆 —— 保证界面上总有圆可拖。"""
    try:
        cands = detect.detect_wells(imageio.load_rgb(path))
    except Exception:
        cands = []
    if cands:
        r = float(cands[0].radius_px)
        return {"cx": float(cands[0].center[0]), "cy": float(cands[0].center[1]),
                "a": r, "b": r, "th": 0.0, "auto": True}
    r = 0.25 * min(width, height)
    return {"cx": width / 2.0, "cy": height / 2.0, "a": r, "b": r, "th": 0.0,
            "auto": True}


def _circle_json(c: dict) -> str:
    return json.dumps({"cx": c["cx"], "cy": c["cy"], "a": c["a"],
                       "b": c["b"], "th": c["th"], "auto": c.get("auto", False)})


def _save_circle(conn, image_id: int, c: dict) -> float:
    """按用户定的圆重算比例尺并落库，同时重算该图所有测量值。

    返回新的 um_per_px。短轴不参与换算（拍斜时只有长轴对应真实直径）。
    """
    result = from_well_ellipse(major_px=2.0 * float(c["a"]),
                               minor_px=2.0 * float(c["b"]))
    apply_calibration(conn, image_id, result, circle=_circle_json(c))
    recompute_measurements(conn, image_id)
    return result.um_per_px


def _state(conn, row) -> dict:
    """该图的完整状态：尺寸 + 用户定的圆 + 比例尺 + 所有测量线。"""
    img = imageio.imread_unicode(storage.resolve_path(row["path"]))
    if img is None:
        raise HTTPException(500, f"这张照片读不出来：{row['original_filename']}")

    calib = db.get_calibration(conn, row["id"])
    circle = None
    if calib is not None and calib["circle"]:
        try:
            circle = json.loads(calib["circle"])
        except ValueError:
            circle = None

    scale_bar = None
    if calib is not None and calib["scale_bar"]:
        try:
            scale_bar = json.loads(calib["scale_bar"])
        except ValueError:
            scale_bar = None

    return {
        "image_id": row["id"],
        "name": row["name"],
        "width": int(img.shape[1]),
        "height": int(img.shape[0]),
        "circle": circle,
        "scale_bar": scale_bar,
        "um_per_px": float(calib["um_per_px"]) if calib else None,
        "enhance": enhance.parse_enhance(row),
        "crop": exporter.load_rect(row),
        "lines": measure.list_lines(conn, row["id"]),
    }


@app.post("/api/open")
def open_photo(body: PhotoIn, conn=Depends(get_db)) -> dict:
    """打开一张照片。

    - 给 `image_id`：打开库里已有的图（下拉框走这条）
    - 给 `rel`：打开 样品集/ 里的文件；同一路径重复打开会复用旧记录（保住已画的线和圆）

    ⚠️ 两条路都必须走到下面那段"没有圆就自动放一个"—— 界面里**没有**
    从零画一个圆的入口，跳过这一步用户就会卡在"拖不了圆、量不了线"的死局里。
    """
    if body.image_id is not None:
        row = _require_image(conn, body.image_id)
        p = storage.resolve_path(row["path"])
    else:
        if not body.rel:
            raise HTTPException(400, "没说清楚要打开哪张照片")
        p = _resolve_sample(body.rel)
        row = conn.execute(
            "SELECT * FROM images WHERE path=? ORDER BY id DESC LIMIT 1", (str(p),)
        ).fetchone()
        if row is None:
            iid = db.create_image(conn, p.stem, p.name, str(p), "sample")
            row = db.get_image(conn, iid)

    state = _state(conn, row)
    if state["circle"] is None:
        # 初次打开：自动放一个起点圆并直接标定。
        # 用户一拖就覆盖它，之前的测量值会自动重算 —— 所以起点粗糙没关系。
        c = _rough_circle(p, state["width"], state["height"])
        state["um_per_px"] = _save_circle(conn, row["id"], c)
        state["circle"] = c
        state["lines"] = measure.list_lines(conn, row["id"])
    return state


@app.get("/api/image/{image_id}")
def get_state(image_id: int, conn=Depends(get_db)) -> dict:
    return _state(conn, _require_image(conn, image_id))


@app.get("/api/image/{image_id}/file")
def get_file(image_id: int, conn=Depends(get_db)):
    p = storage.resolve_path(_require_image(conn, image_id)["path"])
    if not p.exists():
        raise HTTPException(404, "这张照片的文件找不到了，可能被挪走或删了")
    # nosniff：别让浏览器把图片当别的类型解释
    return FileResponse(p, headers={"X-Content-Type-Options": "nosniff"})


# ---------------- 定圆（标定） ----------------

class CircleIn(BaseModel):
    cx: float
    cy: float
    a: float    # 长半轴（像素）
    b: float    # 短半轴（像素）
    th: float   # 长轴与水平方向的夹角（弧度）


@app.put("/api/image/{image_id}/circle")
def put_circle(image_id: int, c: CircleIn, conn=Depends(get_db)) -> dict:
    """用户拖动圆后调用：重算比例尺、落库、并把所有测量值重算一遍。"""
    _require_image(conn, image_id)
    try:
        um_per_px = _save_circle(conn, image_id, c.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    _drop_thumb(image_id)      # 标定一变，卡片上那张小图才有标尺、长度也跟着变
    return {"um_per_px": um_per_px, "lines": measure.list_lines(conn, image_id)}


# ---------------- 测量线 ----------------

class LineIn(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float
    note: str = ""


class NoteIn(BaseModel):
    note: str


class LabelIn(BaseModel):
    dx: float    # 相对线段中点的偏移（原图像素）
    dy: float


def _drop_thumb(image_id: int) -> None:
    """量完/挪完/删完，图库卡片上那张小图得重画。

    ⚠️ 缩略图上**画着测量线**（`thumbs._annotated`）。不叫这一下的话，
    用户新拉了一条线、进图库一看还是张光图，会以为"没存上"。
    """
    thumbs.drop(image_id)


def _image_of_line(conn, meas_id: int) -> int | None:
    row = conn.execute("SELECT image_id FROM measurements WHERE id = ?",
                       (meas_id,)).fetchone()
    return row["image_id"] if row is not None else None


@app.post("/api/image/{image_id}/lines")
def add_line(image_id: int, body: LineIn, conn=Depends(get_db)) -> dict:
    _require_image(conn, image_id)
    try:
        r = measure.add_line(conn, image_id, body.x1, body.y1,
                             body.x2, body.y2, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _drop_thumb(image_id)
    return r


@app.delete("/api/lines/{meas_id}")
def delete_line(meas_id: int, conn=Depends(get_db)) -> dict:
    image_id = _image_of_line(conn, meas_id)
    measure.delete_line(conn, meas_id)
    if image_id is not None:
        _drop_thumb(image_id)
    return {"ok": True}


@app.put("/api/lines/{meas_id}/note")
def update_note(meas_id: int, body: NoteIn, conn=Depends(get_db)) -> dict:
    measure.update_note(conn, meas_id, body.note)
    return {"ok": True}


@app.put("/api/lines/{meas_id}/label")
def update_label(meas_id: int, body: LabelIn, conn=Depends(get_db)) -> dict:
    """挪动长度标签。只影响显示位置，不改测量值。"""
    measure.update_label(conn, meas_id, body.dx, body.dy)
    image_id = _image_of_line(conn, meas_id)
    if image_id is not None:
        _drop_thumb(image_id)
    return {"ok": True}


@app.post("/api/image/{image_id}/undo")
def undo(image_id: int, conn=Depends(get_db)) -> dict:
    r = {"removed": measure.undo_last(conn, image_id),
         "lines": measure.list_lines(conn, image_id)}
    if r["removed"]:
        _drop_thumb(image_id)
    return r


@app.post("/api/image/{image_id}/clear-lines")
def clear_lines(image_id: int, conn=Depends(get_db)) -> dict:
    removed = measure.clear_all(conn, image_id)
    if removed:
        _drop_thumb(image_id)
    return {"removed": removed, "lines": []}


class BarIn(BaseModel):
    x: float | None = None      # 标尺右端点的原图坐标
    y: float | None = None


@app.put("/api/image/{image_id}/scale-bar")
def put_scale_bar(image_id: int, body: BarIn, conn=Depends(get_db)) -> dict:
    """记下用户把标尺拖到哪了。x/y 传 null 就恢复成默认那个角。"""
    _require_image(conn, image_id)
    if body.x is None or body.y is None:
        db.set_scale_bar(conn, image_id, None)
    else:
        db.set_scale_bar(conn, image_id,
                         json.dumps({"x": float(body.x), "y": float(body.y)}))
    _drop_thumb(image_id)      # 卡片上的小图也画着标尺，挪了位置得重画
    return {"ok": True}


# ---------------- 画质调整 ----------------

class EnhanceIn(BaseModel):
    b: float = 1.0     # 亮度
    c: float = 1.0     # 对比度
    s: float = 1.0     # 饱和度


@app.put("/api/image/{image_id}/enhance")
def put_enhance(image_id: int, body: EnhanceIn, conn=Depends(get_db)) -> dict:
    """记下这张图的画质调整。

    这几个值只决定**画出来长什么样**（页面和导出图都算），原图一个字节都不动、
    坐标也不受影响 —— 所以一条测量记录都不用重算。
    """
    _require_image(conn, image_id)
    if not all(math.isfinite(getattr(body, k)) for k in enhance.ENHANCE_RANGE):
        raise HTTPException(400, "调整参数不对")

    vals = {k: round(min(max(getattr(body, k), lo), hi), 3)
            for k, (lo, hi) in enhance.ENHANCE_RANGE.items()}
    # 全中性就存 NULL：库里不该留下"用户调过"的假象
    db.update_image(conn, image_id,
                    enhance=None if vals == enhance.ENHANCE_NEUTRAL
                    else json.dumps(vals))
    thumbs.drop(image_id)          # 图库卡片要跟着变，别留旧的小图
    return {"enhance": vals}


# ---------------- 裁剪 ----------------

class CropIn(BaseModel):
    """裁剪框。**None = 取消裁剪** —— 这是 PUT，整个资源替换，
    所以"不裁剪"就是四个数全不给。"""
    x0: float | None = None
    y0: float | None = None
    x1: float | None = None
    y1: float | None = None


@app.put("/api/image/{image_id}/crop")
def put_crop(image_id: int, body: CropIn, conn=Depends(get_db)) -> dict:
    """记下这张图裁到哪。

    裁剪只决定**画出来什么样**（测量页、图库缩略图、导出的图都算），
    原图一个字节都不动、坐标也不受影响 —— 所以一条测量记录都不用重算。
    """
    _require_image(conn, image_id)
    vals = [body.x0, body.y0, body.x1, body.y1]
    given = [v is not None for v in vals]

    if not any(given):                       # 四个都不给 = 取消裁剪
        db.update_image(conn, image_id, crop=None)
        thumbs.drop(image_id)
        return {"crop": None}

    if not all(given):
        raise HTTPException(400, "裁剪参数不对：四个数要么全给、要么全不给")
    if not all(math.isfinite(v) for v in vals):
        raise HTTPException(400, "裁剪参数不对")
    if vals[2] - vals[0] < 8 or vals[3] - vals[1] < 8:
        raise HTTPException(400, "裁得太小了，再拖大一点")

    # ⚠️ 这里**不做**边界钳制：服务端此刻没载图，不知道照片多大。
    # 越界在渲染时夹住（exporter.apply_crop），那一刻图就在手上。
    rect = {"x0": vals[0], "y0": vals[1], "x1": vals[2], "y1": vals[3]}
    db.update_image(conn, image_id, crop=json.dumps(rect))
    thumbs.drop(image_id)            # 图库卡片要跟着变
    return {"crop": rect}


# ---------------- 导出 ----------------

def _parse_crop(spec: str):
    """把前端传来的裁剪参数解析成 annotate 认的结构。

    格式：""=全图 / "focus:3"=聚焦第 3 条线 / "rect:x0,y0,x1,y1"=手动框
    """
    if spec.startswith("focus:"):
        return {"mode": "focus", "seq": int(spec[6:])}
    if spec.startswith("rect:"):
        return {"mode": "rect", "rect": [float(v) for v in spec[5:].split(",")]}
    return None


def _download_header(stem: str, ext: str) -> dict:
    """带中文文件名的下载头。

    非 ASCII 文件名必须走 RFC 5987 的 filename*=utf-8''...，
    否则浏览器那边会变成一堆问号或直接叫 "download"。
    """
    safe = "".join(c for c in stem if c not in storage.BAD_NAME_CHARS).strip() or "导出"
    name = f"{safe}_标注.{ext}"
    return {"Content-Disposition": f"attachment; filename*=utf-8''{quote(name)}"}


@app.get("/api/image/{image_id}/export.png")
def export_png(image_id: int, crop: str = "", scale_bar: bool = True,
               scale_bar_pos: str = "br", show_seq: bool = True,
               show_notes: bool = False, supersample: int = 2,
               conn=Depends(get_db)):
    """导出带标注的 PNG。全程只读，不改数据库也不改照片文件。"""
    row = _require_image(conn, image_id)
    try:
        img = exporter.build_annotated_image(
            conn, image_id, crop=_parse_crop(crop), scale_bar=scale_bar,
            scale_bar_pos=scale_bar_pos, show_seq=show_seq,
            show_notes=show_notes, supersample=max(1, min(4, supersample)))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError:
        raise HTTPException(404, f"照片文件找不到了：{row['original_filename']}")

    # 直接编码成内存里的 PNG 字节。不走 cv2.imwrite —— 中文路径会静默失败。
    ok, buf = cv2.imencode(".png", img[:, :, ::-1])       # RGB -> BGR
    if not ok:
        raise HTTPException(500, "生成图片失败")
    return Response(content=buf.tobytes(), media_type="image/png",
                    headers=_download_header(row["name"], "png"))


@app.get("/api/image/{image_id}/export.csv")
def export_csv(image_id: int, conn=Depends(get_db)):
    """导出这张图的测量记录。带 BOM，Excel 打开中文不乱码。"""
    row = _require_image(conn, image_id)
    try:
        text = exporter.measurements_to_csv(conn, image_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return Response(content=text, media_type="text/csv; charset=utf-8",
                    headers=_download_header(row["name"], "csv"))


# ---------------- 设置 ----------------

# 孔直径的合理范围（μm）。0.1mm ~ 100mm —— 比任何真实的坐滴板都宽得多，
# 但足以拦住"把毫米当微米填了"这类差 1000 倍的错：那种错不会报错，
# 只会让量出来的每一个数都静静地偏 1000 倍。
_WELL_MIN_UM = 100.0
_WELL_MAX_UM = 100_000.0


class SettingsIn(BaseModel):
    well_diameter_um: float


@app.get("/api/settings")
def get_settings() -> dict:
    """当前设置。`factory_um` 是出厂默认值，界面上「恢复默认」那一项用它。"""
    return {"well_diameter_um": settings.well_diameter_um(),
            "factory_um": config.WELL_DIAMETER_UM}


@app.put("/api/settings")
def put_settings(body: SettingsIn) -> dict:
    """改设置。

    ⚠️ **不回头改任何已有照片。** 每张照片的换算系数在它定圆那一刻就单独存进
    calibrations 表了，所以这里只影响"以后新定圆的照片"。想让某张老照片也用上
    新直径，把那张图的圆重新拖一下 —— 会按新值重存，并自动重算它所有的线。
    """
    v = float(body.well_diameter_um)
    if not (_WELL_MIN_UM <= v <= _WELL_MAX_UM):
        raise HTTPException(
            400,
            f"孔直径填得不对：{v / 1000:g} 毫米。请填 0.1 到 100 毫米之间的数。"
            f"（现在用的是 {settings.well_diameter_um() / 1000:g} 毫米）")
    return {"well_diameter_um": settings.set_well_diameter_um(v)}


# ---------------- 页面 ----------------

@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "measure.html")


@app.get("/gallery")
def gallery():
    return FileResponse(config.WEB_DIR / "gallery.html")


@app.get("/api/health")
def health() -> dict:
    """给 launch.py 判断"端口上跑的是不是我们、是不是同一版本"。"""
    return {"status": "ok", "api": config.API_VERSION}
