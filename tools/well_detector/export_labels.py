"""把手工标定过的图导出成训练快照（只读原图，拷贝一份到 data/train_well/）。

用法：D:/anaconda3/python.exe tools/well_detector/export_labels.py

- 只取 `calibrations.circle.auto == false` 的行 —— 自动放的圆不是标签。
- 原图拷贝到 `data/train_well/images/`，训练脚本只读这份快照，不再碰数据库。
- `labels.json` 里的坐标都是**原图**坐标；训练脚本自己做 letterbox 转换。
- `visible_arc`：圆周上落在画面内的比例。用户的孔大多是残缺的（中位约 57%），
  这个字段用来分桶看模型在"孔拍不全"时表现如何。
"""
import json
import math
import shutil
import sqlite3
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402
from app import storage  # noqa: E402

DB = ROOT / "data" / "app.db"
OUT = ROOT / "data" / "train_well"


def visible_arc(cx, cy, a, w, h, n=360):
    ok = 0
    for k in range(n):
        t = math.radians(360.0 * k / n)
        x, y = cx + a * math.cos(t), cy + a * math.sin(t)
        if 0 <= x <= w - 1 and 0 <= y <= h - 1:
            ok += 1
    return ok / float(n)


def main():
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT i.id, i.name, i.original_filename, i.import_source, i.path, c.circle"
        " FROM images i JOIN calibrations c ON c.image_id = i.id"
        " ORDER BY i.id").fetchall()
    conn.close()

    img_dir = OUT / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    items, skipped = [], []
    for r in rows:
        try:
            c = json.loads(r["circle"])
        except Exception:
            continue
        if c.get("auto"):
            continue
        src = storage.resolve_path(r["path"])
        if not src.is_file():
            skipped.append((r["id"], r["name"], "原图不在了"))
            continue
        try:
            w, h = Image.open(src).size
        except Exception as e:
            skipped.append((r["id"], r["name"], f"读不出尺寸: {e}"))
            continue
        dst = img_dir / (str(r["id"]) + src.suffix.lower())
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dst)
        items.append({
            "id": int(r["id"]),
            "name": r["name"] or "",
            "original_filename": r["original_filename"] or "",
            "source": r["import_source"] or "",
            "file": str(dst.relative_to(OUT)).replace("\\", "/"),
            "width": int(w),
            "height": int(h),
            "cx": float(c["cx"]),
            "cy": float(c["cy"]),
            "a": float(c["a"]),
            "b": float(c["b"]),
            "th": float(c.get("th", 0.0)),
            "visible_arc": round(visible_arc(float(c["cx"]), float(c["cy"]),
                                            float(c["a"]), w, h), 3),
        })

    payload = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(items),
        "items": items,
    }
    (OUT / "labels.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    arcs = sorted(x["visible_arc"] for x in items)
    p = lambda q: arcs[min(len(arcs) - 1, int(round(q * (len(arcs) - 1))))] if arcs else 0
    print(f"exported={len(items)} skipped={len(skipped)}")
    print(f"visible_arc p10={p(.1):.2f} p50={p(.5):.2f} p90={p(.9):.2f} "
          f"full(>=0.8)={sum(1 for v in arcs if v >= 0.8)}")
    for sid, name, why in skipped:
        print(f"  skipped {sid} {name}: {why}")
    print("out:", OUT)


if __name__ == "__main__":
    main()
