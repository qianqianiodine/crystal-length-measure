"""后台预识别孔：把「起点圆」提前算好，打开图片时不用等。

为什么要有它（2026-10-22 实测）：
- 打开一张图时，解码只要 0.02 秒，`detect.detect_wells()` 却要 **4.4～5.0 秒**；
- 用户一张一张打开，每张都要等这 5 秒。

用户的原话：「加入文件夹的图片是我肯定要看的」。所以只给**已经在文件夹里**的
照片提前算，没进文件夹的不动，省 CPU。

⚠️ 存的是 `images.rough_circle` 里的**起点圆缓存**，不是正式标定：
- 不写 `calibrations` 表；
- 图库缩略图不会提前画出基于"没核对过的圆"的标尺；
- 用户打开后照旧可以拖 / 缩 / 转，第一次存圆时才写正式标定。

后台一次只算一张（5 秒一张，不抢满 CPU）。任何一张出错都只跳过它，不能把
后台线程弄死。
"""
import json
import queue
import threading

from app import db, detect, detect_dl, imageio, storage

# 队列和去重集合。queue.Queue 自己线程安全；_PENDING 需要 _LOCK 保护。
_QUEUE: queue.Queue = queue.Queue()
_PENDING: set[int] = set()
_LOCK = threading.Lock()
_STARTED = False


def _fallback(width: int, height: int) -> dict:
    """和原来 main._rough_circle 的兜底一样：居中的圆，保证界面上总有圆可拖。"""
    r = 0.25 * min(width, height)
    return {"cx": width / 2.0, "cy": height / 2.0,
            "a": r, "b": r, "th": 0.0, "auto": True}


def circle_from_image(img) -> dict:
    """从已经解码好的 RGB 图里猜起点圆。检不出就给居中兜底。"""
    h, w = img.shape[:2]
    try:
        cands = detect.detect_wells(img)
    except Exception:                       # noqa: BLE001 —— 检测失败不该挡住打开
        cands = []
    if cands:
        r = float(cands[0].radius_px)
        return {"cx": float(cands[0].center[0]), "cy": float(cands[0].center[1]),
                "a": r, "b": r, "th": 0.0, "auto": True}
    return _fallback(int(w), int(h))


def circle_from_file(path, width: int | None = None,
                     height: int | None = None) -> dict | None:
    """解码 + 猜圆。**模型优先**（约 10ms），不可用才回退规则检测（约 5s）。

    读不出来时：给了宽高就用居中兜底，没给就返回 None。
    """
    try:
        img = imageio.load_rgb(path)
    except Exception:                       # noqa: BLE001
        if width and height:
            return _fallback(int(width), int(height))
        return None
    c = detect_dl.predict(img)
    if c is not None:
        return c
    return circle_from_image(img)


def parse_cached(row) -> dict | None:
    """从 images.rough_circle 读缓存。空串 / 坏 JSON / 形状不对都当没有。"""
    try:
        raw = row["rough_circle"]
    except (KeyError, IndexError, TypeError):
        return None
    if not raw:
        return None
    try:
        c = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(c, dict) or not all(
            k in c for k in ("cx", "cy", "a", "b", "th")):
        return None
    c.setdefault("auto", True)
    return c


def cached_or_detect(conn, row, path, width: int, height: int) -> dict:
    """打开图片时用：有缓存直接返回；没有就现算并存下来（下次就快了）。"""
    cached = parse_cached(row)
    if cached is not None:
        return cached
    c = circle_from_file(path, width, height)
    if c is None:
        c = _fallback(int(width), int(height))
    db.set_image_rough_circle(conn, row["id"], json.dumps(c))
    return c


# ---------------- 后台队列 ----------------

def missing_ids(conn) -> list[int]:
    """该补的图：在至少一个文件夹里、没缓存、没正式标定。"""
    rows = conn.execute(
        "SELECT DISTINCT i.id FROM images i"
        " JOIN image_folders f ON f.image_id = i.id"
        " LEFT JOIN calibrations c ON c.image_id = i.id"
        " WHERE c.id IS NULL"
        "   AND (i.rough_circle IS NULL OR i.rough_circle = '')"
        " ORDER BY i.id").fetchall()
    return [int(r["id"]) for r in rows]


def start() -> None:
    """启动后台线程。只有 app.launch 设了 JLTX_BG_DETECT=1 才会走到这里。"""
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    threading.Thread(target=_loop, name="jltx-bgdetect", daemon=True).start()


def enqueue(ids) -> None:
    """把图片 id 排队。去重；后台没启动时什么都不做（测试环境不会偷偷跑）。"""
    if not _STARTED:
        return
    with _LOCK:
        for iid in ids:
            iid = int(iid)
            if iid not in _PENDING:
                _PENDING.add(iid)
                _QUEUE.put(iid)


def enqueue_existing() -> None:
    """启动时补旧图：已经在文件夹里、但还没识别过的。"""
    if not _STARTED:
        return
    conn = db.get_conn()
    try:
        ids = missing_ids(conn)
    finally:
        conn.close()
    enqueue(ids)


def _loop() -> None:
    """一张一张处理。线程自己的数据库连接只在这个线程里用。"""
    conn = db.get_conn()
    try:
        while True:
            iid = _QUEUE.get()
            with _LOCK:
                _PENDING.discard(iid)
            try:
                _process(conn, iid)
            except Exception:               # noqa: BLE001 —— 一张坏图不能弄死队列
                pass
    finally:
        conn.close()


def _process(conn, iid: int) -> None:
    """算一张。任何"不该算"的情况都直接跳过。"""
    row = db.get_image(conn, iid)
    if row is None:
        return
    if parse_cached(row) is not None:
        return
    if db.get_calibration(conn, iid) is not None:
        return
    if not db.image_folders(conn, iid):
        return
    path = storage.resolve_path(row["path"])
    if not path.is_file():
        return
    c = circle_from_file(path)
    if c is None:
        return
    db.set_image_rough_circle(conn, iid, json.dumps(c))
