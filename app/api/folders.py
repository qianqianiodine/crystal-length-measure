"""文件夹 API。

定案（2026-09-20 用户拍板，详见 app/db.py 的 folders 段和 .claude/memory/memory.md）：
多对多、可套层、删除有条件连坐、复制不复制照片文件。

删除的规则最容易踩坑，单独说一遍：
**只有「删完之后不属于任何文件夹」的照片才真删。** 还留在别的文件夹里的，
只是从被删的那个文件夹里移出来，照片和测量记录都完好。
删之前前端先调 /delete-preview 拿数字，弹窗里写清楚「会删 N 张、另有 M 张保留」。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app import db, storage
from app.api.images import _validate_name      # 同一份名字校验，别再写第二份
from app.db import get_db

router = APIRouter(prefix="/api", tags=["folders"])


def _require_folder(conn, folder_id: int | None, what: str = "文件夹") -> None:
    """folder_id 为 None 表示「最外层」，是合法的，直接放行。"""
    if folder_id is not None and db.get_folder(conn, folder_id) is None:
        raise HTTPException(404, f"找不到这个{what}，可能已经被删了")


def _require_image(conn, image_id: int) -> None:
    if db.get_image(conn, image_id) is None:
        raise HTTPException(404, "找不到这张图，可能记录被删了")


# ---------------- 树 ----------------

@router.get("/folders")
def list_folders(conn=Depends(get_db)) -> dict:
    """整棵树 + 每个文件夹直接装了几张 + 全部/未分类的总数。

    items 是**深度优先展开的一维数组**，每项带 depth（0 是最外层）。
    界面靠「后代紧跟在自己后面、depth 更大」这个顺序算子树 —— 改排序会连带改前端。
    """
    counts = db.folder_counts(conn)
    items = [{**f, "count": counts.get(f["id"], 0)} for f in db.list_folders(conn)]
    return {
        "items": items,
        "total": db.count_images(conn),
        "uncategorized": db.uncategorized_count(conn),
    }


class FolderIn(BaseModel):
    name: str
    parent_id: int | None = None


@router.post("/folders")
def create_folder(body: FolderIn, conn=Depends(get_db)) -> dict:
    """新建。parent_id 省略或给 null 就是建在最外层。"""
    _require_folder(conn, body.parent_id, "上级文件夹")
    name = db.unique_folder_name(conn, _validate_name(body.name), body.parent_id)
    fid = db.create_folder(conn, name, body.parent_id)
    return {"id": fid, "name": name, "parent_id": body.parent_id}


class FolderPatch(BaseModel):
    name: str | None = None
    parent_id: int | None = None


@router.patch("/folders/{folder_id}")
def patch_folder(folder_id: int, body: FolderPatch, conn=Depends(get_db)) -> dict:
    """改名和/或移动。parent_id 用 model_fields_set 区分「没传」和「传了 null」。"""
    if db.get_folder(conn, folder_id) is None:
        raise HTTPException(404, "找不到这个文件夹，可能已经被删了")

    if "parent_id" in body.model_fields_set:
        _require_folder(conn, body.parent_id, "目标文件夹")
        try:
            db.move_folder(conn, folder_id, body.parent_id)
        except ValueError as e:                  # 移到自己里面 / 自己的子文件夹里
            raise HTTPException(400, str(e)) from e

    if body.name is not None:
        cur = db.get_folder(conn, folder_id)     # 移动后重读：名字要按新父级查重
        name = db.unique_folder_name(conn, _validate_name(body.name),
                                     cur["parent_id"], exclude=folder_id)
        db.update_folder(conn, folder_id, name=name)

    row = db.get_folder(conn, folder_id)
    return {"id": row["id"], "name": row["name"], "parent_id": row["parent_id"]}


class CopyIn(BaseModel):
    parent_id: int | None = None


@router.post("/folders/{folder_id}/copy")
def copy_folder(folder_id: int, body: CopyIn, conn=Depends(get_db)) -> dict:
    """复制到指定位置。只复制文件夹结构和归档关系，照片文件不复制、不占双份磁盘。"""
    src = db.get_folder(conn, folder_id)
    if src is None:
        raise HTTPException(404, "找不到这个文件夹，可能已经被删了")
    _require_folder(conn, body.parent_id, "目标文件夹")

    name = db.unique_folder_name(conn, src["name"], body.parent_id)
    new_id = db.copy_folder(conn, folder_id, body.parent_id, name)
    return {"id": new_id, "name": name, "parent_id": body.parent_id}


# ---------------- 删除 ----------------

@router.get("/folders/{folder_id}/delete-preview")
def delete_preview(folder_id: int, conn=Depends(get_db)) -> dict:
    """删之前先看看会牵连到什么。不修改任何东西。"""
    _require_folder(conn, folder_id)
    plan = db.folder_delete_plan(conn, folder_id)
    plan.pop("delete_ids")                       # 只是一串 id，界面不需要
    return plan


@router.delete("/folders/{folder_id}")
def delete_folder(folder_id: int, with_photos: bool = Query(False),
                  conn=Depends(get_db)) -> dict:
    """删文件夹。

    with_photos=False（默认）：文件夹没了，照片全部变成「未分类」。
    with_photos=True：只在这个文件夹里的照片跟着删掉（原图进 data/trash/，
    测量记录级联删，不可恢复）；还属于别的文件夹的照片**保留**，只是解除关联。
    """
    _require_folder(conn, folder_id)
    plan = db.folder_delete_plan(conn, folder_id)

    deleted = 0
    if with_photos:
        for iid in plan["delete_ids"]:
            row = db.get_image(conn, iid)
            if row is None:
                continue
            storage.trash_file(storage.resolve_path(row["path"]))
            db.delete_image(conn, iid)
            deleted += 1

    db.delete_folder(conn, folder_id)            # 子文件夹和成员关系由外键级联
    return {
        "folders_deleted": plan["folders"],
        "images_deleted": deleted,
        "images_kept": plan["images_elsewhere"] if with_photos
                       else plan["images_in_subtree"],
    }


# ---------------- 照片归档 ----------------

class ImageFoldersIn(BaseModel):
    folder_ids: list[int] = []


@router.put("/images/{image_id}/folders")
def set_image_folders(image_id: int, body: ImageFoldersIn,
                      conn=Depends(get_db)) -> dict:
    """设置这张照片放进哪些文件夹（覆盖式）。空列表 = 移出全部，回到「未分类」。"""
    _require_image(conn, image_id)
    ids = sorted(set(body.folder_ids))
    for fid in ids:
        _require_folder(conn, fid)
    db.set_image_folders(conn, image_id, ids)
    return {"image_id": image_id, "folders": ids}
