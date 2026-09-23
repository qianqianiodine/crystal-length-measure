"""SQLite schema 与仓储层。

设计要点（见设计文档 §4）：
- calibrations 对 image_id 唯一（UNIQUE），重复标定是覆盖而非新增
- measurements.calib_snapshot 记录算这条线时用的 um_per_px，
  保证历史记录可追溯 —— 标定日后改了，旧记录仍能复现
- 删除图像时级联删除其标定与测量记录
"""
import sqlite3
import time
from pathlib import Path

from app import config

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS images (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    original_filename TEXT    NOT NULL,
    path              TEXT    NOT NULL,
    import_time       INTEGER NOT NULL,
    import_source     TEXT    NOT NULL DEFAULT 'unknown',
    tags              TEXT    NOT NULL DEFAULT '[]',
    favorite          INTEGER NOT NULL DEFAULT 0,
    transform         TEXT,
    thumb_path        TEXT,
    enhance           TEXT,
    crop              TEXT
);

CREATE TABLE IF NOT EXISTS calibrations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id       INTEGER NOT NULL UNIQUE REFERENCES images(id) ON DELETE CASCADE,
    method         TEXT    NOT NULL,
    pixel_length   REAL    NOT NULL,
    real_length_um REAL    NOT NULL,
    um_per_px      REAL    NOT NULL,
    created_time   INTEGER NOT NULL,
    circle         TEXT,
    scale_bar      TEXT
);

CREATE TABLE IF NOT EXISTS measurements (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id       INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    x1             REAL NOT NULL,
    y1             REAL NOT NULL,
    x2             REAL NOT NULL,
    y2             REAL NOT NULL,
    pixel_length   REAL NOT NULL,
    measured_um    REAL NOT NULL,
    calib_snapshot REAL NOT NULL,
    note           TEXT NOT NULL DEFAULT '',
    created_time   INTEGER NOT NULL,
    label_dx       REAL NOT NULL DEFAULT 0,
    label_dy       REAL NOT NULL DEFAULT 0
);

-- 文件夹。parent_id 自引用做套层，层数不限。
-- ON DELETE CASCADE 是为了删父文件夹时子文件夹跟着走 —— 应用层会先把整棵
-- 子树算出来处理照片，这里的级联只负责收拾行。
CREATE TABLE IF NOT EXISTS folders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    parent_id    INTEGER REFERENCES folders(id) ON DELETE CASCADE,
    created_time INTEGER NOT NULL
);

-- 照片 ↔ 文件夹，多对多：一张照片可以同时在好几个文件夹里。
-- 两边都 CASCADE：删照片掉关系，删文件夹也掉关系（照片本身还在）。
CREATE TABLE IF NOT EXISTS image_folders (
    image_id  INTEGER NOT NULL REFERENCES images(id)  ON DELETE CASCADE,
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    PRIMARY KEY (image_id, folder_id)
);

-- 待确认导入的暂存条目。**没有批次 id** —— 手机端一个请求传一张，
-- 服务端看不出"这 5 张是同一批"，所以全库共用一份列表。
-- name 就是**确认导入时要用**的名字（以前这里存不含前缀的半截，前缀另有一张
-- pending_state 表 —— 2026-09-23 用户要求去掉统一前缀，那张表也一起删了。
-- 要批量为一批照片起名，用图库打标签对话框里的「编号」：编号 + 孔位 = 名字）。
-- status='failed' 的行要在列表里占一行（需求 §3.1.2 要红字标出文件名和原因），
-- 它有**两类**，区别全在 staged_path 上：
--   ① 从没读出来的（"这不是图片"）—— staged_path 为空，改名字也进不了库；
--   ② 暂存成功了、只是上一次没进去的（名字超长、写库失败）—— **staged_path 还在**，
--      用户改完名字再点「确认导入」就能重试。
-- ⚠️ 别把 ② 当 ① 清掉：清了等于让用户重传照片（见 tests 里
-- test_a_staged_row_that_failed_on_its_name_survives_a_second_click）。
-- folder_id：这一行确认导入时要放进哪个文件夹。NULL = 不放进文件夹（未分类）。
--   默认值由上传请求带进来（手机扫码的 URL 里带着图库当时在看的文件夹），
--   存在**行上**而不是全局 —— 否则「传了 3 张放 A、又传 2 张放 B」会把前面的
--   也一起改掉（见 set_all_pending_folders）。
-- tags：确认导入时要写进 images.tags 的标签（JSON 数组字符串）。手机上传时
--   名字长得像孔位（A1-1）才会有值，否则是空数组。
CREATE TABLE IF NOT EXISTS pending_imports (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    original_filename TEXT    NOT NULL,
    size              INTEGER NOT NULL DEFAULT 0,
    staged_path       TEXT,
    status            TEXT    NOT NULL DEFAULT 'ok',
    reason            TEXT    NOT NULL DEFAULT '',
    import_source     TEXT    NOT NULL DEFAULT 'unknown',
    folder_id         INTEGER,
    tags              TEXT    NOT NULL DEFAULT '[]',
    created_time      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_meas_image ON measurements(image_id, seq);
CREATE INDEX IF NOT EXISTS idx_img_time   ON images(import_time DESC);
CREATE INDEX IF NOT EXISTS idx_if_folder  ON image_folders(folder_id);
CREATE INDEX IF NOT EXISTS idx_fd_parent  ON folders(parent_id);
"""

# update_image 只允许改这些字段，防止 SQL 注入与误改
UPDATABLE_FIELDS = {"name", "tags", "favorite", "transform", "thumb_path",
                    "enhance", "crop"}


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    """打开连接。row_factory=Row 便于按列名取值。"""
    p = Path(db_path) if db_path else config.DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    """FastAPI 依赖：每个请求一条连接 —— 开连接很便宜，省去跨线程共享的麻烦。

    放在这里而不是 main.py：router 模块也要用它，从 main 导入会成环。
    本模块不依赖 fastapi —— FastAPI 靠「这是个生成器函数」认出来，不需要 import。
    """
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


# 后加的列：(表, 列名, 列定义)。SCHEMA 里的 CREATE TABLE IF NOT EXISTS
# **不会**给已存在的表补列，所以老库必须靠这里 ALTER 一次。
MIGRATIONS = [
    ("measurements", "label_dx", "REAL NOT NULL DEFAULT 0"),
    ("measurements", "label_dy", "REAL NOT NULL DEFAULT 0"),
    ("calibrations", "circle", "TEXT"),
    ("calibrations", "scale_bar", "TEXT"),
    ("images", "enhance", "TEXT"),
    ("images", "crop", "TEXT"),
    ("pending_imports", "folder_id", "INTEGER"),
    ("pending_imports", "tags",      "TEXT NOT NULL DEFAULT '[]'"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """给老库补上后加的列。幂等：列已存在就跳过。"""
    for table, col, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    """建表 + 补列。幂等，可重复调用。"""
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


# ---------- images ----------

def create_image(conn, name: str, original_filename: str, path: str,
                 import_source: str, transform: str | None = None,
                 tags: str = "[]") -> int:
    cur = conn.execute(
        "INSERT INTO images (name, original_filename, path, import_time, "
        "import_source, tags, favorite, transform) VALUES (?,?,?,?,?,?,0,?)",
        (name, original_filename, path, int(time.time()), import_source, tags, transform),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_image(conn, image_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()


# 画廊的排序方式。只认这几个键 —— 用户输入**绝不**拼进 SQL。
# ⚠️ SQLite 默认按字节序排，中文名字是「按 Unicode 码位」而不是按拼音。
# 想要拼音序得在 Python 里排，不值得；先这样。
ORDERS = {
    "time_desc": "import_time DESC, id DESC",
    "time_asc":  "import_time ASC, id ASC",
    "name_asc":  "name ASC, id ASC",
    "name_desc": "name DESC, id DESC",
}


def _filter(conn, status: str, q: str, folder: int | None = None) -> tuple[str, list]:
    """画廊的筛选条件。status 只认 measured / unmeasured，别的一律当「全部」。

    folder：None = 不限，0 = 未分类（哪个文件夹都不属于），
    其余 = 那个文件夹**连同它下面所有子文件夹**里的照片。

    ⚠️ 是子树，不是"只看直接放进去的"（2026-09-23 用户要求改的）。
    改成子树之后，「选中全部 N 张」的作用面跟着变大 —— 点一个父文件夹再全选，
    选中的是整棵子树的照片。这是想要的，但删/移动之前要看清弹窗里的数字。
    """
    conds: list[str] = []
    params: list = []
    if status == "measured":
        conds.append("EXISTS (SELECT 1 FROM measurements m WHERE m.image_id=images.id)")
    elif status == "unmeasured":
        conds.append("NOT EXISTS (SELECT 1 FROM measurements m WHERE m.image_id=images.id)")

    q = (q or "").strip()
    if q:
        # % 和 _ 是 LIKE 的通配符 —— 用户真打出这两个字符时要当普通字符搜
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conds.append("name LIKE ? ESCAPE '\\'")
        params.append(f"%{esc}%")

    if folder is not None:
        if folder == 0:
            conds.append("NOT EXISTS (SELECT 1 FROM image_folders f WHERE f.image_id=images.id)")
        else:
            subs = subtree_ids(conn, folder)
            marks = ",".join("?" * len(subs))
            # ⚠️ 必须写成 `images.id IN (子查询)`，**不能**写成
            # `EXISTS (SELECT 1 FROM image_folders f WHERE f.image_id=images.id
            #          AND f.folder_id IN (...))`。
            # 后者在 SQLite 3.51.0 上会**按子查询的匹配行数吐重复的外层行**：
            # 一张照片同时在父和子文件夹里（多对多是设计好的）时，图库里会出现
            # 两张一模一样的卡片。`IN (1)` 单元素不出问题、`IN (1,2)` 就出，
            # 跑一次 ANALYZE 又正常 —— 是查询计划器选错计划的坑，别去赌它。
            # 记在 .claude/memory/learnings.md 里了。
            conds.append(f"images.id IN (SELECT f.image_id FROM image_folders f"
                         f" WHERE f.folder_id IN ({marks}))")
            params.extend(subs)

    return ("WHERE " + " AND ".join(conds) if conds else ""), params


def list_images(conn, limit: int = 24, offset: int = 0, *,
                order: str = "time_desc", status: str = "all",
                q: str = "", folder: int | None = None) -> list[sqlite3.Row]:
    where, params = _filter(conn, status, q, folder)
    return conn.execute(
        f"SELECT * FROM images {where} "
        f"ORDER BY {ORDERS.get(order, ORDERS['time_desc'])} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()


def count_images(conn, *, status: str = "all", q: str = "",
                 folder: int | None = None) -> int:
    where, params = _filter(conn, status, q, folder)
    return int(conn.execute(
        f"SELECT COUNT(*) c FROM images {where}", params
    ).fetchone()["c"])


def ids_matching(conn, *, status: str = "all", q: str = "",
                 folder: int | None = None) -> list[int]:
    """按筛选条件取**全部** id，不分页。

    「选中全部 N 张」用它。筛选条件必须和图库格子墙一模一样 ——
    所以复用 _filter()，别另写一份 SQL（两份迟早会分叉）。

    不带 LIMIT 是故意的：这个接口的语义就是"我全要"，
    悄悄截断会让用户以为全选了、其实漏了一批。
    """
    where, params = _filter(conn, status, q, folder)
    return [int(r["id"]) for r in
            conn.execute(f"SELECT id FROM images {where} ORDER BY id", params)]


def count_measured(conn, image_ids: list[int]) -> int:
    """这批照片里有**几张**画过线（不是几条线）。

    删除确认框里那句「其中 3 张量过线」用它。分批查是因为 SQLite 对
    一条语句里的参数个数有上限（老版本只有 999），而"选中全部"可能上千张。
    """
    total = 0
    ids = list(image_ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        total += int(conn.execute(
            f"SELECT COUNT(DISTINCT image_id) c FROM measurements"
            f" WHERE image_id IN ({marks})", chunk).fetchone()["c"])
    return total


def update_image(conn, image_id: int, **fields) -> None:
    """更新白名单字段。传了非白名单字段会抛 ValueError。"""
    bad = set(fields) - UPDATABLE_FIELDS
    if bad:
        raise ValueError(f"不允许更新的字段：{sorted(bad)}")
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE images SET {sets} WHERE id=?", (*fields.values(), image_id))
    conn.commit()


def delete_image(conn, image_id: int) -> None:
    """删除图像记录。标定与测量由外键级联删除。"""
    conn.execute("DELETE FROM images WHERE id=?", (image_id,))
    conn.commit()


# ---------- calibrations ----------

def set_calibration(conn, image_id: int, method: str,
                    pixel_length: float, real_length_um: float,
                    circle: str | None = None) -> int:
    """写入或覆盖该图的标定。um_per_px 由公式派生。

    circle：用户在界面上定的那个圆的几何（JSON 字符串，不透明存储），
    用于刷新页面后把圆原样画回来。传 None 表示清掉旧圆。
    """
    if pixel_length <= 0:
        raise ValueError("像素长度必须大于 0")
    um_per_px = real_length_um / pixel_length
    conn.execute(
        "INSERT INTO calibrations (image_id, method, pixel_length, real_length_um,"
        " um_per_px, created_time, circle) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(image_id) DO UPDATE SET "
        "method=excluded.method, pixel_length=excluded.pixel_length, "
        "real_length_um=excluded.real_length_um, um_per_px=excluded.um_per_px, "
        "created_time=excluded.created_time, circle=excluded.circle",
        (image_id, method, pixel_length, real_length_um, um_per_px,
         int(time.time()), circle),
    )
    conn.commit()
    return int(conn.execute("SELECT id FROM calibrations WHERE image_id=?",
                            (image_id,)).fetchone()["id"])


def get_calibration(conn, image_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM calibrations WHERE image_id=?",
                        (image_id,)).fetchone()


def set_scale_bar(conn, image_id: int, spec: str | None) -> None:
    """记下用户把标尺拖到哪了（JSON，原图像素）。传 None 表示回到默认角落。

    单独一个函数而不是塞进 set_calibration：改圆的时候不该把标尺位置冲掉。
    """
    conn.execute("UPDATE calibrations SET scale_bar=? WHERE image_id=?",
                 (spec, image_id))
    conn.commit()


# ---------- measurements ----------

def next_seq(conn, image_id: int) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq),0) m FROM measurements WHERE image_id=?",
                       (image_id,)).fetchone()
    return int(row["m"]) + 1


def add_measurement(conn, image_id: int, x1: float, y1: float, x2: float, y2: float,
                    pixel_length: float, measured_um: float, calib_snapshot: float,
                    note: str = "") -> int:
    """新增测量记录。seq 自动递增，不用调用方传。"""
    seq = next_seq(conn, image_id)
    cur = conn.execute(
        "INSERT INTO measurements (image_id, seq, x1, y1, x2, y2, pixel_length,"
        " measured_um, calib_snapshot, note, created_time) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (image_id, seq, x1, y1, x2, y2, pixel_length, measured_um,
         calib_snapshot, note, int(time.time())),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_measurements(conn, image_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM measurements WHERE image_id=? ORDER BY seq", (image_id,)
    ).fetchall()


def delete_measurement(conn, meas_id: int) -> None:
    conn.execute("DELETE FROM measurements WHERE id=?", (meas_id,))
    conn.commit()


def set_label_offset(conn, meas_id: int, dx: float, dy: float) -> None:
    """记下长度标签相对线段中点的偏移（原图像素）。

    存原图坐标而不是屏幕坐标：标签是贴在照片上的，
    放大缩小时它应该跟着照片走，而不是跟着屏幕走。
    """
    conn.execute("UPDATE measurements SET label_dx=?, label_dy=? WHERE id=?",
                 (dx, dy, meas_id))
    conn.commit()


# ---------- folders（文件夹） ----------
#
# 设计定案（2026-09-20 用户拍板，见 .claude/memory/memory.md）：
# - 多对多：一张照片可以同时在好几个文件夹里
# - 可套层，层数不限；移动文件夹要防环（不能移进自己的子孙里）
# - 删文件夹**不**无条件连坐照片：只有「删完之后不属于任何文件夹」的才真删，
#   还留在别的文件夹里的只是解除关联。这一步由应用层算，见 folder_delete_plan
# - 复制文件夹只复制结构 + 成员关系，**不复制照片文件**（多对多本来就允许共享）
# - 「未分类」不是真文件夹，是「哪个文件夹都不属于」的查询（folder=0）

def create_folder(conn, name: str, parent_id: int | None) -> int:
    cur = conn.execute(
        "INSERT INTO folders (name, parent_id, created_time) VALUES (?,?,?)",
        (name, parent_id, int(time.time())),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_folder(conn, folder_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()


def update_folder(conn, folder_id: int, **fields) -> None:
    bad = set(fields) - {"name", "parent_id"}
    if bad:
        raise ValueError(f"不允许更新的字段：{sorted(bad)}")
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE folders SET {sets} WHERE id=?",
                 (*fields.values(), folder_id))
    conn.commit()


def delete_folder(conn, folder_id: int) -> None:
    """只删文件夹本身。子文件夹和成员关系由外键级联收拾，照片一张都不动。"""
    conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))
    conn.commit()


def subtree_ids(conn, folder_id: int) -> list[int]:
    """自己 + 所有后代。

    UNION（不是 UNION ALL）：万一数据被外部改出环，UNION 会去重、能停下来，
    UNION ALL 会一直转下去把页面卡死。
    """
    rows = conn.execute(
        "WITH RECURSIVE sub(id) AS ("
        "  SELECT id FROM folders WHERE id=? "
        "  UNION "
        "  SELECT f.id FROM folders f JOIN sub ON f.parent_id = sub.id"
        ") SELECT id FROM sub", (folder_id,)).fetchall()
    return [r["id"] for r in rows]


def list_folders(conn) -> list[dict]:
    """整棵树，深度优先展开成一维（自带 depth 给界面缩进用），同级按名字排。

    在 Python 里拼而不是写递归 SQL：文件夹总共几十个，可读性划算得多。
    界面靠「后代紧跟在自己后面、且 depth 更大」这个顺序来算子树（见 gallery.html）。
    """
    rows = conn.execute("SELECT * FROM folders").fetchall()
    kids: dict[int | None, list] = {}
    for r in rows:
        kids.setdefault(r["parent_id"], []).append(r)

    out: list[dict] = []
    seen: set[int] = set()

    def walk(parent: int | None, depth: int) -> None:
        for r in sorted(kids.get(parent, []), key=lambda r: (r["name"], r["id"])):
            if r["id"] in seen:          # 数据被外部改出环时的护栏，别转死
                continue
            seen.add(r["id"])
            out.append({"id": r["id"], "name": r["name"],
                        "parent_id": r["parent_id"], "depth": depth})
            walk(r["id"], depth + 1)

    walk(None, 0)
    return out


def folder_name_taken(conn, name: str, parent_id: int | None,
                      exclude: int | None = None) -> bool:
    row = conn.execute(
        "SELECT 1 FROM folders WHERE name=? AND parent_id IS ? AND id IS NOT ? LIMIT 1",
        (name, parent_id, exclude)).fetchone()
    return row is not None


def unique_folder_name(conn, name: str, parent_id: int | None,
                       exclude: int | None = None) -> str:
    """同一个父文件夹里重名就加 " (2)"。改了名再改回来不该变成 "X (2)"，所以有 exclude。"""
    if not folder_name_taken(conn, name, parent_id, exclude):
        return name
    n = 2
    while folder_name_taken(conn, f"{name} ({n})", parent_id, exclude):
        n += 1
    return f"{name} ({n})"


def move_folder(conn, folder_id: int, parent_id: int | None) -> None:
    """把文件夹挪到 parent_id 底下（None = 最外层）。防环，坏了就抛 ValueError。"""
    if parent_id is not None:
        if parent_id == folder_id:
            raise ValueError("不能把文件夹移到自己里面")
        if parent_id in subtree_ids(conn, folder_id):
            raise ValueError("不能把文件夹移到它自己的子文件夹里")
    update_folder(conn, folder_id, parent_id=parent_id)


def copy_folder(conn, folder_id: int, parent_id: int | None, new_name: str) -> int:
    """深拷贝整棵子树的**结构**和**成员关系**，返回副本根节点的 id。

    照片文件一个都不复制 —— 多对多本来就允许一张图同时属于多个文件夹，
    副本里的照片是「也放进来」，不是「再存一份」。
    new_name 由调用方查好重名（只有副本根节点需要，子树内部不可能撞）。
    """
    if get_folder(conn, folder_id) is None:
        raise ValueError("找不到这个文件夹")
    new_id = create_folder(conn, new_name, parent_id)
    _copy_children(conn, folder_id, new_id)
    return new_id


def _copy_children(conn, src_id: int, dst_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO image_folders (image_id, folder_id) "
        "SELECT image_id, ? FROM image_folders WHERE folder_id=?", (dst_id, src_id))
    conn.commit()
    for kid in conn.execute("SELECT * FROM folders WHERE parent_id=?",
                            (src_id,)).fetchall():
        nid = create_folder(conn, kid["name"], dst_id)
        _copy_children(conn, kid["id"], nid)


def folder_counts(conn) -> dict[int, int]:
    """每个文件夹**连同它下面所有子文件夹**一共装了几张照片。

    ⚠️ `COUNT(DISTINCT image_id)` 不是可选的：一张照片可以同时被放进父和子
    （多对多是设计好的），不去重就会把一个孔算两遍，树上写 6 张、点进去 3 张。

    ⚠️ 这是 N 次 subtree_ids + N 次 COUNT（N = 文件夹数，几十个）。
    SQLite 本地跑，够快；真慢了再说 —— 别提前上记忆化。
    """
    out: dict[int, int] = {}
    for row in conn.execute("SELECT id FROM folders"):
        subs = subtree_ids(conn, row["id"])
        marks = ",".join("?" * len(subs))
        out[int(row["id"])] = int(conn.execute(
            f"SELECT COUNT(DISTINCT image_id) c FROM image_folders"
            f" WHERE folder_id IN ({marks})", subs).fetchone()["c"])
    return out


def uncategorized_count(conn) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) c FROM images WHERE NOT EXISTS "
        "(SELECT 1 FROM image_folders f WHERE f.image_id=images.id)").fetchone()["c"])


def image_folders(conn, image_id: int) -> list[int]:
    return [r["folder_id"] for r in conn.execute(
        "SELECT folder_id FROM image_folders WHERE image_id=? ORDER BY folder_id",
        (image_id,))]


def set_image_folders(conn, image_id: int, folder_ids: list[int]) -> None:
    """覆盖式设置这张照片属于哪些文件夹。传空列表 = 回到「未分类」。"""
    conn.execute("DELETE FROM image_folders WHERE image_id=?", (image_id,))
    conn.executemany("INSERT OR IGNORE INTO image_folders (image_id, folder_id)"
                     " VALUES (?,?)",
                     [(image_id, f) for f in sorted(set(folder_ids))])
    conn.commit()


def folder_delete_plan(conn, folder_id: int) -> dict:
    """算清楚「删掉这个文件夹」会牵连到什么。**只看不删**，confirm 弹窗靠它出数字。

    关键那条：一张照片只要还留在被删子树**以外**的某个文件夹里，就不该被删，
    只是解除这里的关联。多对多下这是唯一安全的做法 ——
    否则删「6月批次」会顺手删掉一张同时归档在「样品A」里、已经量过的照片。
    """
    subs = subtree_ids(conn, folder_id)
    marks = ",".join("?" * len(subs))
    ids = [r["image_id"] for r in conn.execute(
        f"SELECT DISTINCT image_id FROM image_folders WHERE folder_id IN ({marks})",
        subs).fetchall()]

    mem: dict[int, set[int]] = {}
    if ids:
        marks2 = ",".join("?" * len(ids))
        for r in conn.execute(
                f"SELECT image_id, folder_id FROM image_folders"
                f" WHERE image_id IN ({marks2})", ids):
            mem.setdefault(r["image_id"], set()).add(r["folder_id"])

    sub_set = set(subs)
    doomed = [i for i in ids if not (mem.get(i, set()) - sub_set)]
    return {
        "folders": len(subs),
        "images_in_subtree": len(ids),
        "images_only_here": len(doomed),
        "images_elsewhere": len(ids) - len(doomed),
        "delete_ids": doomed,
    }


# ---------- pending_imports（待确认导入列表） ----------
#
# 设计定案（2026-09-20）见 docs/superpowers/specs/2026-09-20-待确认导入列表-design.md：
# - 全局一份列表，没有批次概念
# - name 就是确认导入时要用的名字（统一前缀 2026-09-23 去掉了）
# - 失败的行在列表里占一行，但**不一定**没有 staged_path：暂存成功、只是上一次
#   没进库的行（名字超长、写库失败）暂存文件还在，改完名字点确认就能重试。
#   把这类行连文件一起清掉 = 用户得重新传照片。

def add_pending(conn, name: str, original_filename: str, size: int,
                staged_path: str | None, status: str, reason: str,
                import_source: str, folder_id: int | None = None,
                tags: str = "[]") -> int:
    cur = conn.execute(
        "INSERT INTO pending_imports (name, original_filename, size, staged_path,"
        " status, reason, import_source, folder_id, tags, created_time)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (name, original_filename, size, staged_path, status, reason,
         import_source, folder_id, tags, int(time.time())),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_pending(conn, pid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM pending_imports WHERE id=?", (pid,)).fetchone()


def list_pending(conn) -> list[sqlite3.Row]:
    """按上传顺序。列表从上往下就是用户传进来的顺序。"""
    return conn.execute("SELECT * FROM pending_imports ORDER BY id").fetchall()


def set_pending_name(conn, pid: int, name: str) -> None:
    """改这一行的名字。

    顺手把上一次失败留下的红字清掉：用户改名字正是为了让它能进库，
    而"名字不能超过 100 个字"挂在那里已经跟现状无关了。

    ⚠️ 只清**有暂存文件**的行。从没读出来的行（staged_path 为空）
    名字改得再对也进不了库，清掉红字会让它看着像好了，点确认才发现是被跳过的。
    """
    conn.execute("UPDATE pending_imports SET name=? WHERE id=?", (name, pid))
    conn.execute("UPDATE pending_imports SET status='ok', reason='' "
                 "WHERE id=? AND staged_path IS NOT NULL", (pid,))
    conn.commit()


def set_pending_folder(conn, pid: int, folder_id: int | None) -> None:
    conn.execute("UPDATE pending_imports SET folder_id=? WHERE id=?", (folder_id, pid))
    conn.commit()


def set_all_pending_folders(conn, folder_id: int | None) -> None:
    """把当前待确认列表**所有行**设成同一个文件夹（手机/电脑上那个「这批放到」）。

    ⚠️ 刻意做成一次改全部：它是"这一批放哪儿"，不是每行的独立状态。
    要单独改某一行走 set_pending_folder —— 否则「传了 3 张放 A、又传 2 张放 B」
    时改一下 B 会把前面 3 张也带走。
    """
    conn.execute("UPDATE pending_imports SET folder_id=?", (folder_id,))
    conn.commit()


def set_pending_tags(conn, pid: int, tags: str) -> None:
    conn.execute("UPDATE pending_imports SET tags=? WHERE id=?", (tags, pid))
    conn.commit()


def set_pending_reason(conn, pid: int, reason: str) -> None:
    """把一行标成失败的。确认导入时某一行动不了，就地写上原因让它变红字。"""
    conn.execute("UPDATE pending_imports SET status='failed', reason=? WHERE id=?",
                 (reason, pid))
    conn.commit()


def delete_pending(conn, pid: int) -> None:
    conn.execute("DELETE FROM pending_imports WHERE id=?", (pid,))
    conn.commit()


def clear_pending(conn) -> int:
    n = int(conn.execute("SELECT COUNT(*) c FROM pending_imports").fetchone()["c"])
    conn.execute("DELETE FROM pending_imports")
    conn.commit()
    return n
