"""待确认导入列表（需求文档 §3.1.3）。

上传的照片先进 `data/staging/`，用户在列表里起好名字再「确认导入」正式入库；
「取消」则一张不留。

设计定案（2026-09-20，见 docs/superpowers/specs/2026-09-20-待确认导入列表-design.md）：
- **全局一份列表，没有批次 id** —— 手机端一个请求传一张，服务端看不出"这 5 张
  是同一批"。与其让前端编一个批次号传进来，不如共用一份。
- 每行的 `name` 只存**不含前缀**的那半截；前缀在 pending_state 表里。
  这样清掉前缀时每一行自动退回自己的 base，不需要撤销任何东西。
- 统一前缀存服务端而不是浏览器：手机和电脑看的是同一份列表，前缀放本地存储
  的话两边各记一个，确认出来的名字会不一样。

⚠️ 同一批处理函数挂了**两条路径**：`/api/imports`（电脑）和
`/m/{token}/imports`（手机）。手机必须走后者 —— main.py 的 lan_guard 对非本机
请求只放行 `/m/<当前令牌>` 开头的路径，直接打 /api/imports 会被自己的守卫 403 掉。
令牌不用在这里再验一遍（中间件已经在路由之前卡死了），所以处理函数里
**不声明** token 参数 —— 声明了它就会变成桌面路由上一个必填的 query 参数。
"""
import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import db, storage, thumbs
from app.api.images import (_BAD_CHARS, _name_from_file, _pending_summary,
                            _reason, _unique_name, _validate_name)
from app.db import get_db

router = APIRouter(prefix="/api/imports", tags=["imports"])
phone = APIRouter(prefix="/m/{token}/imports", tags=["mobile"])

# 前缀不能太长：它要套在每一行的名字前面，而名字总共只有 100 字。
PREFIX_MAX_LEN = 60

# confirm_pending 是同步函数，Starlette 会把它丢进线程池 —— 两个请求是**真的并行**。
# 列表又是电脑和手机共用一份：两边同时点「确认导入」（或者手快点两下），
# 两边都会读到同一批行、各入库一遍。用一把锁把整个确认过程串起来。
_CONFIRM_LOCK = threading.Lock()


def both(method: str, path: str):
    """同一个处理函数同时挂在电脑和手机两条路径上。

    手机那份不进 OpenAPI 文档：它是给手机页用的镜像，不是对外接口，
    顺带也免了两条路由撞 operation id。
    """
    def deco(fn):
        getattr(router, method)(path)(fn)
        getattr(phone, method)(path, include_in_schema=False)(fn)
        return fn
    return deco


def _pending_list(conn) -> dict:
    """列表的完整形状。**每个改动列表的接口都返回它** —— 前端一次重画，
    省掉一整类"两边各记一份状态、慢慢对不上"的毛病。"""
    items = [_pending_summary(conn, r) for r in db.list_pending(conn)]
    ok = [i for i in items if i["status"] == "ok"]
    return {
        "items": items,
        "prefix": db.get_prefix(conn),
        "total_size": sum(i["size"] for i in ok),
        "ok_count": len(ok),
        "failed_count": len(items) - len(ok),
    }


def _drop(conn, row) -> None:
    """把一行从待确认列表里去掉：暂存文件、它的小图、数据库记录。"""
    if row["staged_path"]:
        storage.discard_staged(row["staged_path"])
    thumbs.drop_staged(row["id"])
    db.delete_pending(conn, row["id"])


@both("get", "")
def list_pending(conn=Depends(get_db)) -> dict:
    return _pending_list(conn)


@both("get", "/{pid}/thumb")
def import_thumb(pid: int, conn=Depends(get_db)):
    """待确认行的小图。第一次请求时现做，之后走磁盘缓存。"""
    row = db.get_pending(conn, pid)
    if row is None:
        raise HTTPException(404, "这一行已经不在了")
    if not row["staged_path"]:
        raise HTTPException(404, "这张没传成功，没有图可以看")

    p = thumbs.ensure_at(storage.resolve_staged(row["staged_path"]),
                         thumbs.path_for_staged(pid))
    if p is None:
        raise HTTPException(404, "这张照片读不出来")
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400",
                                 "X-Content-Type-Options": "nosniff"})


class NameIn(BaseModel):
    name: str


@both("patch", "/{pid}")
def patch_pending(pid: int, body: NameIn, conn=Depends(get_db)) -> dict:
    """改这一行的名字（只有不含前缀的那半截）。

    **允许空字符串** —— 用户清空重打的中间态就是空的，那时候弹 400 只会让人困惑。
    空串表示"到确认时回退到原始文件名"（需求 §3.1.3 的"未命名"那一条）。
    前缀拼上去之后的总长度这里管不了（前缀随时会变），留到确认时兜底。
    """
    if db.get_pending(conn, pid) is None:
        raise HTTPException(404, "这一行已经不在了")

    name = (body.name or "").strip()
    if name:
        _validate_name(name)
    db.set_pending_name(conn, pid, name)
    return _pending_summary(conn, db.get_pending(conn, pid))


@both("delete", "/{pid}")
def remove_pending(pid: int, conn=Depends(get_db)) -> dict:
    """移除这一行：暂存文件、它的小图、数据库记录一起清掉。"""
    row = db.get_pending(conn, pid)
    if row is None:
        raise HTTPException(404, "这一行已经不在了")
    _drop(conn, row)
    return _pending_list(conn)


class PrefixIn(BaseModel):
    prefix: str


@both("put", "/prefix")
def set_prefix(body: PrefixIn, conn=Depends(get_db)) -> dict:
    """设统一前缀。传空串 = 清掉。

    改前缀会同时改掉**每一行**的冲突状态，所以整份列表一起返回。
    """
    prefix = (body.prefix or "").strip()
    if prefix:
        if len(prefix) > PREFIX_MAX_LEN:
            raise HTTPException(400, f"前缀不能超过 {PREFIX_MAX_LEN} 个字")
        if _BAD_CHARS.search(prefix):
            raise HTTPException(400, f"前缀里不能有 {storage.BAD_NAME_CHARS} 这些符号")
    db.set_prefix(conn, prefix)
    return _pending_list(conn)


@both("delete", "")
def cancel_pending(conn=Depends(get_db)) -> dict:
    """取消导入：列表清空、暂存文件和小图删掉、前缀清空，一张都不入库。"""
    n = 0
    for row in db.list_pending(conn):
        _drop(conn, row)
        n += 1
    db.set_prefix(conn, "")
    return {"cleared": n}


class ConfirmIn(BaseModel):
    overwrite: list[int] = []


@both("post", "/confirm")
def confirm_pending(request: Request, body: ConfirmIn,
                    conn=Depends(get_db)) -> dict:
    """把列表里正常的照片正式入库。

    逐行处理、**成功一行就从列表里去掉**：中途出错的话前面成功的已经进库、
    剩下的还留在列表里带红字，用户可以改完接着点。这比"全有或全无"好用。

    留不住的行有三类：
    ① 从没读出来的（"这不是图片"）压根没有暂存文件，留在列表里也改不好，
       这里顺手清掉并计进 skipped；
    ② 暂存下来了、只是这一次没进去的（名字太长、写库偶发失败）—— 暂存文件还在，
       下次点「确认导入」会重试，所以**必须留着**；
    ③ 暂存文件在磁盘上没了（有人手动清过 staging/）—— 它进不来也退不掉，
       标上红字等用户自己移除这一行。

    overwrite 里列出的行，如果库里真有同名的，会先删掉它再用原名导入 ——
    这是唯一会毁数据的路径，界面上必须已经弹过一次写清条数的警告。

    ⚠️ 顺序上**先落盘再删旧的**：万一新文件存不下来，旧的那张还完好无损。

    ⚠️ 覆盖的「旧照片」**只认这次之前就在库里的**：同一批里前一行刚落成的那张
    不算旧的（手机相机文件名会重复，两行同名 + 两行都点覆盖很常见）——
    删它就是把用户自己刚传的照片丢进 trash、连测量一起删掉，而确认前的
    警告从没提过它。名字撞上了就自动加后缀，两张都留住。

    ⚠️ 整个过程在 `_CONFIRM_LOCK` 里跑：它跨请求，也跨电脑和手机两条路径。
    """
    # 手机上不许覆盖：覆盖会连旧照片量过的线一起删（不可恢复），手机屏幕讲不清。
    # 只靠手机页不画那个按钮挡不住构造出来的请求，所以在手机这条路径上真拦住它。
    if body.overwrite and request.url.path.startswith("/m/"):
        raise HTTPException(400, "在手机上不能覆盖已有的照片，请到电脑上操作")

    with _CONFIRM_LOCK:
        prefix = db.get_prefix(conn)
        overwrite = set(body.overwrite)
        imported = skipped = unnamed = 0
        overwritten: list[str] = []
        created: set[int] = set()       # 本次 confirm 已经建过的图，不能当"旧的"删

        for row in db.list_pending(conn):
            if row["status"] != "ok" and not row["staged_path"]:
                _drop(conn, row)                # 从没读出来的行，没有文件可留
                skipped += 1
                continue

            if not row["staged_path"]:
                # 今天到不了的一行（status='ok' 却没有暂存文件）。真出现了也不能
                # 掉进 resolve_staged(None) —— 那会抛 TypeError 冲出循环，
                # 客户端拿到 500、后面的行一行都不处理、前缀也不清。
                continue

            src = storage.resolve_staged(row["staged_path"])
            if not src.is_file():
                db.set_pending_reason(conn, row["id"],
                                      "暂存的文件找不到了，移除这一行重新传")
                continue

            base = row["name"].strip()
            used_fallback = not base
            if used_fallback:
                base = _name_from_file(row["original_filename"])

            try:
                final = _validate_name(prefix + base)
                data = src.read_bytes()
                stored = storage.store_original(data)   # 先落盘 —— 下一步是删东西
            except Exception as e:                      # noqa: BLE001
                db.set_pending_reason(conn, row["id"], _reason(e))
                continue

            try:
                if row["id"] in overwrite:
                    old = conn.execute("SELECT * FROM images WHERE name=? LIMIT 1",
                                       (final,)).fetchone()
                    if old is not None and old["id"] not in created:
                        storage.trash_file(storage.resolve_path(old["path"]))
                        db.delete_image(conn, old["id"])  # 标定和测量级联删除，不可恢复
                        overwritten.append(final)
                    else:
                        # 库里本来就没有（old 是 None），或者那张是同一批里前一行刚建的。
                        # 后者绝不能删 —— 当成撞名，两张都留住。
                        final = _unique_name(conn, final)
                else:
                    final = _unique_name(conn, final)

                new_id = db.create_image(conn, final, row["original_filename"], stored,
                                         row["import_source"])
            except Exception as e:                      # noqa: BLE001
                # 删旧的和建新的之间没有事务可依：create_image 炸了的话旧的可能已经没了。
                # 但异常绝不能冲出循环 —— 那会让客户端 500、剩下的行不再处理、前缀也不清。
                # 刚落盘的这份原图现在没人引用：留在原图目录里就是个永久孤儿
                #（界面上看不见、也删不掉，重试还会再写一份），挪进 trash 收拾掉。
                try:
                    storage.trash_file(storage.resolve_path(stored))
                except Exception:                       # noqa: BLE001
                    # 这里必须吞掉：trash_file 内部是 shutil.move，在 Windows 上对
                    # 一个刚写完的文件它是可能抛的。让它冒出去的话，这个本意是善后的调用
                    # 反而会触发上面那句注释里要防的后果 —— 客户端 500、剩下的行不处理、
                    # 前缀不清。清不掉孤儿文件只是多占点磁盘，比比整批导入中断轻得多。
                    pass
                db.set_pending_reason(conn, row["id"], _reason(e))
                continue

            created.add(new_id)
            if used_fallback:                             # 真的进库了才算「用了原始文件名」
                unnamed += 1
            _drop(conn, row)
            imported += 1

        db.set_prefix(conn, "")
        return {"imported": imported, "skipped": skipped,
                "unnamed": unnamed, "overwritten": overwritten}
