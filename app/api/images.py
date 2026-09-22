"""图像导入与管理 API。

设计取舍：
- 上传支持三种模式（mode=auto|direct|pending）。默认 auto：**一次传多张**才进
  「待确认导入列表」（需求文档 §3.1.3），单张直接入库走快路径。
- 返回**逐文件结果**，前端才画得出需求文档 §3.1.2 要的那条红字
  "哪个文件失败了、为什么"。
- 撞名的默认策略是加 " (2)"。覆盖要用户在列表里显式点出来 ——
  那会连带删掉已有照片的测量记录，没有二次确认之前不该开这个口子。
"""
import base64
import binascii
import json
import os
import re
import sys
from datetime import datetime

import cv2
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import config, db, exporter, imageio, measure, storage, thumbs
from app.db import get_db

router = APIRouter(prefix="/api", tags=["images"])

NAME_MAX_LEN = 100
_BAD_CHARS = re.compile(f"[{re.escape(storage.BAD_NAME_CHARS)}\x00-\x1f]")

# 手机原图每张几 MB。需求量级是几十张，放开会写爆系统临时目录。
MAX_FILES = 50
MAX_FILE_BYTES = 40 * 1024 * 1024


# ---------------- 名字 ----------------

def _validate_name(name: str) -> str:
    """用户**手输**的名字。不合法就报错让他改。"""
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "名字不能为空")
    if len(name) > NAME_MAX_LEN:
        raise HTTPException(400, f"名字不能超过 {NAME_MAX_LEN} 个字")
    if _BAD_CHARS.search(name):
        raise HTTPException(400, f"名字里不能有 {storage.BAD_NAME_CHARS} 这些符号")
    return name


def _name_from_file(filename: str) -> str:
    """从**文件名**派生的名字。剥掉非法字符，不报错 —— 手机拍的文件名不该拦住导入。

    ⚠️ 纯字符串处理，不要走 Path：Windows 上 Path("照:片.png").stem 会给出 "片"，
    因为 "照:" 被当成了盘符 —— 中文文件名碰上冒号会被无声吃掉一截。
    """
    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]   # 去掉目录部分
    stem = base.rsplit(".", 1)[0] if "." in base else base          # 去掉扩展名
    return _BAD_CHARS.sub("", stem).strip()[:NAME_MAX_LEN] or "未命名"


def _unique_name(conn, name: str) -> str:
    """撞名就加 " (2)"。库里 name 没有唯一约束，这里只为下拉框里能分清谁是谁。"""
    def taken(n: str) -> bool:
        return conn.execute("SELECT 1 FROM images WHERE name=? LIMIT 1", (n,)).fetchone() is not None

    if not taken(name):
        return name
    n = 2
    while taken(f"{name} ({n})"):
        n += 1
    return f"{name} ({n})"


# ---------------- 落库 ----------------

def _summary(conn, row) -> dict:
    lines = measure.list_lines(conn, row["id"])
    return {
        "id": row["id"],
        "name": row["name"],
        "original_filename": row["original_filename"],
        "import_source": row["import_source"],
        "import_time": row["import_time"],
        "line_count": len(lines),
        "folders": db.image_folders(conn, row["id"]),   # 画廊的「归档」按钮要拿它预勾
        "tags": exporter.parse_tags(row["tags"]),       # 卡片上那个标签按钮要拿它显示
        "crop": exporter.load_rect(row),                # 卡片要知道这张裁没裁
        "thumb_ver": _thumb_ver(conn, row, lines),      # 缩略图 URL 的版本号
    }


def _thumb_ver(conn, row, lines) -> int:
    """这张图的缩略图"是第几版"。

    缩略图上画着裁剪框内的测量线和标尺，所以**凡是会改它长相的东西**都得算进来：
    裁剪、画质、标尺位置、以及每条线的位置/颜色/长度标签偏移。

    ⚠️ 这不是画蛇添足：小图在服务端是按 image_id 缓存 + 主动 `thumbs.drop()`
    失效的，服务端那份永远是对的 —— 但**浏览器的缓存不归我们管**。
    URL 不变，它就接着用自己手上那张，表现是"线都画好了、图库里还是张光图"。
    把版本号拼进 URL，值一变地址就变，浏览器只能重新去拿。
    """
    calib = db.get_calibration(conn, row["id"])
    key = [
        row["crop"] or "", row["enhance"] or "",
        # ⚠️ `um_per_px` 也要算进来，别只算 scale_bar —— 标定前后 scale_bar 都是
        # NULL（用户没拖过），只算它的话"刚标定完"这个版本号和"没标定"是同一个数，
        # 浏览器不重新取，卡片上刚该出现的标尺就看不见。
        calib["um_per_px"] if calib else None,     # 标定过没有 / 比例尺是多少
        calib["scale_bar"] if calib else None,     # 标尺被拖到哪了
        [(ln["x1"], ln["y1"], ln["x2"], ln["y2"], ln.get("color"),
          ln.get("label_dx"), ln.get("label_dy")) for ln in lines],
    ]
    h = 0
    for ch in repr(key):
        h = (h * 31 + ord(ch)) & 0x7FFFFFFF
    return h


def _import_one(conn, filename: str, data: bytes, source: str) -> dict:
    """存盘 + 落库。坏图片抛 ValueError（消息可直接给用户看）。"""
    stored = storage.store_original(data)          # 先验后写
    name = _unique_name(conn, _name_from_file(filename))
    iid = db.create_image(conn, name, filename or "未命名", stored, source)
    return _summary(conn, db.get_image(conn, iid))


def _reason(exc: Exception) -> str:
    return exc.detail if isinstance(exc, HTTPException) else str(exc)


# ---------------- 待确认导入列表 ----------------

def _conflict(conn, full_name: str) -> dict | None:
    """库里有没有一张叫这个名字的。

    有就把它的 id、量过几条线、以及"加后缀之后该叫什么"一起带上 ——
    界面靠 line_count 写清"覆盖会连它量过的 3 条线一起删"，
    靠 suggest 免得自己在前端猜后缀（(2) 被占了就得是 (3)）。
    """
    row = conn.execute("SELECT id, name FROM images WHERE name=? LIMIT 1",
                       (full_name,)).fetchone()
    if row is None:
        return None
    n = conn.execute("SELECT COUNT(*) c FROM measurements WHERE image_id=?",
                     (row["id"],)).fetchone()["c"]
    return {"image_id": row["id"], "name": row["name"], "line_count": int(n),
            "suggest": _unique_name(conn, full_name)}


def _pending_summary(conn, row) -> dict:
    """一行待确认条目。形状和画廊里的一张图不同：它还没入库。

    conflict 只在「前缀+名字」和库里的某张完全同名时才有值。
    名字为空的行（等确认时才回退到文件名）和失败的行一律不给冲突 ——
    前者还没定名字，后者根本入不了库，都不该弹警告。
    """
    base = row["name"].strip()
    full = (db.get_prefix(conn) + base) if base else ""
    return {
        "id": row["id"],
        "name": row["name"],
        "original_filename": row["original_filename"],
        "size": row["size"],
        "status": row["status"],
        "reason": row["reason"],
        "import_source": row["import_source"],
        "conflict": (_conflict(conn, full)
                     if (full and row["status"] == "ok") else None),
    }


def _stage_one(conn, filename, data, size, source) -> dict:
    """存进暂存区并记一行。坏图片抛 ValueError（由 _ingest 转成红字行）。"""
    stored = storage.store_staged(data)
    pid = db.add_pending(conn, _name_from_file(filename), filename or "未命名",
                         size, stored, "ok", "", source)
    return _pending_summary(conn, db.get_pending(conn, pid))


def _stage_failed(conn, filename, size, reason, source) -> dict:
    """读不出来的文件也占一行 —— 列表里红字写清它为什么没成功。"""
    pid = db.add_pending(conn, _name_from_file(filename), filename or "未命名",
                         size, None, "failed", reason, source)
    return _pending_summary(conn, db.get_pending(conn, pid))


async def _ingest(conn, files, *, mode: str, source: str, default_name: str) -> dict:
    """把一批上传的文件收进来。mode 决定直接入库还是先进待确认列表。

    - direct：老行为，逐张入库，坏文件进 failed 数组
    - pending：每张都进暂存区；坏文件也占一行（status='failed'）
    - auto：这一次就 ≥2 张 → pending，否则 direct。
      电脑端两个页面都是**一个请求带多个文件**，所以服务端数得清；
      手机端一个请求一张，由手机页自己传 mode（见 api/mobile.py）。
    """
    if mode == "auto":
        mode = "pending" if len(files) >= 2 else "direct"

    ok, failed, staged = [], [], []
    for up in files:
        filename = up.filename or default_name
        size = 0
        try:
            if up.size and up.size > MAX_FILE_BYTES:
                raise ValueError(f"这个文件超过 {MAX_FILE_BYTES // 1024 // 1024} MB，太大了")
            # 逐个读、逐个存 —— 不要先 read() 成一个列表，那会把整批堆在内存里
            data = await up.read()
            size = len(data)
            if mode == "pending":
                staged.append(_stage_one(conn, filename, data, size, source))
            else:
                ok.append(_import_one(conn, filename, data, source))
        except Exception as e:                       # noqa: BLE001 —— 一个坏文件不该中断整批
            reason = _reason(e)
            if mode == "pending":
                staged.append(_stage_failed(conn, filename, size, reason, source))
            else:
                failed.append({"name": filename, "reason": reason})
        finally:
            await up.close()

    return {"ok": ok, "failed": failed, "staged": staged}


# ---------------- 接口 ----------------

@router.post("/images/upload")
async def upload(files: list[UploadFile] = File(...),
                 mode: str = Query("auto", pattern="^(auto|direct|pending)$"),
                 conn=Depends(get_db)) -> dict:
    """多文件上传。前端把同一个字段名 files 重复提交即可。

    mode：auto（默认）= 这一次 ≥2 张就进待确认列表，否则直接入库；
    direct = 一律直接入库；pending = 一律进待确认列表。
    拼错的值当场报 422，不能悄悄当成 direct —— 那样用户以为照片进了待确认列表，
    其实已经直接入库了。
    """
    if len(files) > MAX_FILES:
        raise HTTPException(400, f"一次最多传 {MAX_FILES} 张，这次有 {len(files)} 张")
    return await _ingest(conn, files, mode=mode, source="upload",
                         default_name="未命名")


class PasteIn(BaseModel):
    data_url: str


@router.post("/images/paste")
async def paste(body: PasteIn, conn=Depends(get_db)) -> dict:
    """从剪贴板导入（浏览器读到的 data URL）。"""
    if not body.data_url.startswith("data:image/"):
        raise HTTPException(400, "剪贴板里没找到图片，请先复制一张")

    try:
        _header, b64 = body.data_url.split(",", 1)
        data = base64.b64decode(b64)
    except (ValueError, binascii.Error) as e:
        raise HTTPException(400, "剪贴板里的图片数据读不出来") from e

    try:
        return {"ok": [_import_one(conn, "粘贴的图", data, "paste")], "failed": []}
    except Exception as e:                           # noqa: BLE001
        raise HTTPException(400, _reason(e)) from e


@router.get("/images")
def list_images(limit: int = Query(200, ge=1, le=1000),
                offset: int = Query(0, ge=0),
                order: str = Query("time_desc"),
                status: str = Query("all"),
                q: str = Query(""),
                folder: int | None = Query(None, ge=0),
                conn=Depends(get_db)) -> dict:
    """列出库里的图。画廊的格子墙和测量页的下拉框都用这个。

    不带参数就是「全部、按导入时间倒序」—— 下拉框要的就是这个。
    folder：不传 = 全部，0 = 未分类，其余 = 那个文件夹（只看直接放进去的）。
    """
    rows = db.list_images(conn, limit=limit, offset=offset,
                          order=order, status=status, q=q, folder=folder)
    return {
        "total": db.count_images(conn, status=status, q=q, folder=folder),
        "items": [_summary(conn, r) for r in rows],
    }


@router.get("/images/ids")
def list_image_ids(status: str = Query("all"), q: str = Query(""),
                   folder: int | None = Query(None, ge=0),
                   conn=Depends(get_db)) -> dict:
    """和图库格子墙**同一套筛选**，但只返回 id。

    「选中全部 N 张」用它。**不带上限** —— 这个接口的语义就是"我全要"，
    截断会让用户以为全选了、其实漏了一批。
    """
    ids = db.ids_matching(conn, status=status, q=q, folder=folder)
    return {"ids": ids, "total": len(ids)}


@router.get("/images/{image_id}/thumb")
def thumb(image_id: int, conn=Depends(get_db)):
    """画廊用的小图。第一次请求时现做，之后走磁盘缓存。"""
    row = db.get_image(conn, image_id)
    if row is None:
        raise HTTPException(404, "找不到这张图，可能记录被删了")

    p = thumbs.ensure(storage.resolve_path(row["path"]), image_id,
                      crop_raw=row["crop"], enhance_raw=row["enhance"],
                      conn=conn)          # 标定过就画上测量线和标尺
    if p is None:
        raise HTTPException(404, "这张照片读不出来，可能文件被挪走了")

    # ⚠️ 不能再长缓存了：裁剪和画质调整会改这张图的样子，
    # 长缓存会让用户裁完、调完在图库里看到 24 小时前的旧图。
    # no-cache **不是"不缓存"**，是"可以存，但每次问一下服务器变没变"；
    # FileResponse 自带 ETag + Last-Modified，没变就回 304 空响应（几十字节）。
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache",
                                 "X-Content-Type-Options": "nosniff"})


class RenameIn(BaseModel):
    name: str


@router.patch("/images/{image_id}")
async def patch_image(image_id: int, body: RenameIn, conn=Depends(get_db)) -> dict:
    """改名。目前只有 name 一个字段走这条路 —— 标签/收藏等画廊做出来再加。"""
    if db.get_image(conn, image_id) is None:
        raise HTTPException(404, "找不到这张图，可能记录被删了")

    name = _unique_name(conn, _validate_name(body.name))
    db.update_image(conn, image_id, name=name)
    return _summary(conn, db.get_image(conn, image_id))


@router.delete("/images/{image_id}")
def delete_image(image_id: int, conn=Depends(get_db)) -> dict:
    """删除。原图挪进 data/trash/（不是真删），标定和测量记录由外键级联删掉。

    `file_moved_to_trash=False` 表示原图不在 data/ 底下 —— 那是老记录，
    照片还留在 样品集/，我们**不碰**。前端要据此改提示语。
    """
    row = db.get_image(conn, image_id)
    if row is None:
        raise HTTPException(404, "找不到这张图，可能记录被删了")

    moved = storage.trash_file(storage.resolve_path(row["path"]))
    db.delete_image(conn, image_id)
    return {"deleted": True, "file_moved_to_trash": moved}


# ---------------- 批量操作（图库多选） ----------------

class BatchDeleteIn(BaseModel):
    image_ids: list[int] = []
    dry_run: bool = False


class BatchFoldersIn(BaseModel):
    image_ids: list[int] = []
    folder_ids: list[int] = []
    mode: str = "add"


def _resolve_ids(conn, image_ids: list[int]) -> list[int]:
    """前端给的 id 过一遍库：去重、不存在的跳过。

    照片可能在另一个窗口被删了 —— 为这个让整批失败不值得（设计 §4.1）。
    **dry_run 和真删都走它**，所以预览的张数和实际删的张数永远一致。
    """
    seen, out = set(), []
    for iid in image_ids:
        if iid in seen:
            continue
        seen.add(iid)
        if db.get_image(conn, iid) is not None:
            out.append(iid)
    return out


@router.post("/images/batch-delete")
def batch_delete(body: BatchDeleteIn, conn=Depends(get_db)) -> dict:
    """批量删除。dry_run=True 只算数字，一张都不动。

    dry_run 的存在理由：确认框要写「其中 3 张量过线，测量记录会一起没」，
    而跨页全选时前端手里只有当前页那 24 行的 line_count，算不出来。
    原图走 trash_file() 挪进 data/trash/（不是真删，磁盘上还找得到）。
    """
    if not body.image_ids:
        raise HTTPException(400, "没有选中任何照片")

    ids = _resolve_ids(conn, body.image_ids)
    measured = db.count_measured(conn, ids)
    if body.dry_run:
        return {"count": len(ids), "measured": measured}

    deleted = moved = 0
    for iid in ids:
        row = db.get_image(conn, iid)
        if row is None:                        # 上面刚查过，这里只为拿 path
            continue
        if storage.trash_file(storage.resolve_path(row["path"])):
            moved += 1
        db.delete_image(conn, iid)             # 测量记录由外键级联删
        deleted += 1
    return {"deleted": deleted, "moved_to_trash": moved}


@router.post("/images/batch-folders")
def batch_folders(body: BatchFoldersIn, conn=Depends(get_db)) -> dict:
    """批量归档。

    add（默认，安全的那边）：每张的结果 = 原来在的 ∪ 新的。
    replace：只留新的；新的为空 = 全部回到「未分类」。
    """
    if not body.image_ids:
        raise HTTPException(400, "没有选中任何照片")
    if body.mode not in ("add", "replace"):
        raise HTTPException(400, "mode 只能是 add 或 replace")

    ids = _resolve_ids(conn, body.image_ids)
    folder_ids = sorted(set(body.folder_ids))
    for fid in folder_ids:
        # 不借 folders._require_folder：folders.py 已经 import 了本模块，反向 import 会成环
        if db.get_folder(conn, fid) is None:
            raise HTTPException(400, "找不到这个文件夹，可能已经被删了")

    updated = 0
    for iid in ids:
        final = (folder_ids if body.mode == "replace"
                 else sorted(set(db.image_folders(conn, iid)) | set(folder_ids)))
        db.set_image_folders(conn, iid, final)
        updated += 1
    return {"updated": updated}


# ---------------- 标签（孔位） ----------------

# 标签 = 孔位。A~H 是行、1~12 是列、`-1`/`-2` 是同一个池里的两个孔。
# 界面永远生成不出别的形状，这里是防手改请求。
_TAG_RE = re.compile(r"^[A-H]([1-9]|1[0-2])-[12]$")


class TagItem(BaseModel):
    id: int
    tags: list[str] = []


class BatchTagsIn(BaseModel):
    items: list[TagItem] = []


@router.post("/images/batch-tags")
def batch_tags(body: BatchTagsIn, conn=Depends(get_db)) -> dict:
    """一次给若干张照片写标签。不存在的 id 静默跳过（别人刚删掉）。

    单张那个入口和图库批量共用这一个端点 —— 单张就是只有一行的批量。
    ⚠️ `PATCH /api/images/{id}` **不动**：扩了它也没有调用方，是没人用的代码。
    """
    if not body.items:
        raise HTTPException(400, "没有要打标签的照片")

    for it in body.items:
        for t in it.tags:
            # fullmatch，不是 match：`$` 也匹配结尾换行，match("A1-1\n") 会放行，
            # 那个尾巴会一路带到导出文件名上（_safe_segment 认不出，整段退回「未命名」）。
            if not _TAG_RE.fullmatch(t):
                raise HTTPException(400, f"标签「{t}」不对，应该是 A1-1 这样的")

    updated = 0
    for it in body.items:
        if db.get_image(conn, it.id) is None:
            continue
        db.update_image(conn, it.id, tags=json.dumps(it.tags))
        updated += 1
    return {"updated": updated}


# ---------------- 批量导出图片 ----------------

class ExportIn(BaseModel):
    image_ids: list[int] = []


@router.post("/images/export-images")
def export_images(body: ExportIn, conn=Depends(get_db)) -> dict:
    """把勾中的照片导出成一个真文件夹，并把它弹出来。

    ⚠️ **这是本项目第一个往磁盘写文件的端点**（其余要么只读、要么只写 SQLite）。
    规矩定死：只往 `导出/` 底下写、**不改数据库**、**不动原图**。

    同步返回，不做后台任务：写几十个 PNG 是几秒的事，而「POST 一下拿到结果」
    一个测试就能钉死。前端配转圈 + 走秒提示。
    """
    ids = _resolve_ids(conn, body.image_ids)          # 去重 + 跳过不存在的
    failed: set[int] = set(body.image_ids) - set(ids)   # 库里没有的（别人刚删掉）
    if not ids:
        raise HTTPException(400, "没有可导出的照片")

    images = [db.get_image(conn, i) for i in ids]
    folders = conn.execute("SELECT id, name, parent_id FROM folders").fetchall()
    members = {i: db.image_folders(conn, i) for i in ids}
    plan = exporter.export_plan(images, folders, members)

    out_dir = config.EXPORT_DIR / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    root = str(out_dir.resolve())

    # 按图分组：一张图在几个文件夹里就有几条，**一张图只渲染一次**（渲染是最慢的一步）。
    #
    # ⚠️ **别把所有图的编码结果都塞进一个 dict 缓存** —— 一屏手机照片 12MP，PNG 一张
    # 10~20 MB，勾 30 张就是 300~600 MB 常驻内存。分组处理的话，同一时刻内存里
    # **只有一张图的字节**。分组也顺便不依赖 export_plan 的输出顺序。
    by_image: dict[int, list[dict]] = {}
    for item in plan:
        by_image.setdefault(item["image_id"], []).append(item)

    written: set[int] = set()          # 真正写出去的那些（count / renamed 都按它算）

    for iid, items in by_image.items():
        try:
            img = exporter.export_image(conn, iid)
        except (imageio.ImageLoadError, ValueError):
            # 原图被别的程序删了/损坏了（ImageLoadError），或者这条记录刚被别处删掉（ValueError）。
            # **一张坏图不能拖垮整批**：勾 30 张坏一张，另外 29 张照样得导出来。
            # 报进 `failed` 让它出现在结果里，别静默吞掉 —— 用户得知道少了哪张。
            failed.add(iid)
            continue

        # 直接编码进内存。不走 cv2.imwrite —— 中文路径会静默失败。
        ok, buf = cv2.imencode(".png", img[:, :, ::-1])          # RGB -> BGR
        if not ok:
            raise HTTPException(500, "生成图片失败")
        data = buf.tobytes()

        for item in items:
            # 第六道闸：拼出来的绝对路径必须仍在导出目录底下。
            # _safe_segment 那五条是「尽量修好」，这一条是「修不好就别写」。
            dest = out_dir.joinpath(*item["parts"], item["name"])
            if os.path.commonpath([str(dest.resolve()), root]) != root:
                raise HTTPException(500, "文件名不安全，导出中止")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        written.add(iid)

    # 把窗口弹出来。弹不出来不算失败 —— 图已经在磁盘上了。
    opened = False
    if sys.platform == "win32":
        try:
            os.startfile(out_dir)
            opened = True
        except OSError:
            pass

    # 给前端一个短路径（`导出/2026-09-22_143052`）。测试里 EXPORT_DIR 被指到系统临时
    # 目录，不在 PROJECT_ROOT 底下，relative_to 会抛 ValueError —— 兜住它退回绝对路径。
    try:
        shown = str(out_dir.relative_to(config.PROJECT_ROOT))
    except ValueError:
        shown = str(out_dir)

    return {
        "dir": shown.replace("\\", "/"),
        "count": len(written),             # **导出成功的照片**张数（一张在 2 个文件夹里算 1 张）
        "renamed": sum(1 for p in plan if p["renamed"] and p["image_id"] in written),
        "failed": sorted(failed),          # 库里没有的 + 中途读不出来的，合并报给用户
        "opened": opened,
    }
