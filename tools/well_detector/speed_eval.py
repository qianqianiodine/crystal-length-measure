"""速度实验：模型单独 vs 模型 + 快速霍夫局部精修。

5 秒来自现在 detect_wells() 的全范围粗搜 + _best_circle 多次评估。
模型只要约 30ms；再用模型预测的半径把霍夫搜索收窄（不再全范围扫），
看能不能在 ~0.1s 内拿到接近混合策略的精度。

用法：
  D:/anaconda3/envs/jltx-train/python.exe tools/well_detector/speed_eval.py
"""
import json
import math
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from sklearn.model_selection import KFold
from torch.utils.data import DataLoader

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from train import CANVAS, LABELS, TRAIN, WellDataset, build_model, load_rgb  # noqa: E402
from app import detect as hough  # noqa: E402

SIDE = 300


def latest_run() -> Path:
    runs = sorted((TRAIN / "runs").glob("*/fold1.pt"), key=lambda p: p.parent.name)
    if not runs:
        raise SystemExit("找不到训练好的 fold1.pt")
    return runs[-1].parent


def model_coords(items, run: Path):
    """用 5 折各自的 fold 模型，预测它没见过的那些图（和训练时的 CV 一致）。"""
    kf = KFold(n_splits=5, shuffle=True, random_state=42)
    out = {}
    for fold, (_tr, va) in enumerate(kf.split(items), 1):
        va_items = [items[i] for i in va]
        ds = WellDataset(va_items, False)
        by_id = {c["item"]["id"]: c for c in ds.cache}
        model = build_model()
        model.load_state_dict(torch.load(run / f"fold{fold}.pt", map_location="cpu"))
        model.eval()
        loader = DataLoader(ds, batch_size=16, shuffle=False, num_workers=0)
        with torch.no_grad():
            for x, _y, ids in loader:
                pred = model(x).numpy()
                for j, iid in enumerate(ids.numpy()):
                    c = by_id[int(iid)]
                    cx = (pred[j, 0] * CANVAS - c["ox"]) / c["s"]
                    cy = (pred[j, 1] * CANVAS - c["oy"]) / c["s"]
                    a = math.exp(pred[j, 2]) * CANVAS / c["s"]
                    out[int(iid)] = (float(cx), float(cy), float(a))
    return out


def fast_hough(img, cx0, cy0, a0):
    """先缩到 640 再找视场圆 —— 24MP 原图上做 Otsu/连通域是新的瓶颈。"""
    h0, w0 = img.shape[:2]
    s0 = min(1.0, 640.0 / max(h0, w0))
    if s0 < 1.0:
        small = cv2.resize(img, (max(1, int(round(w0 * s0))),
                                 max(1, int(round(h0 * s0)))),
                           interpolation=cv2.INTER_AREA)
    else:
        small = img
    cropped, info = hough.crop_field_circle(small)
    if info is not None:
        ox = max(0, info[0] - info[2])
        oy = max(0, info[1] - info[2])
    else:
        ox = oy = 0
    h, w = cropped.shape[:2]
    scale = min(1.0, float(SIDE) / max(h, w))
    if scale < 1.0:
        work = cv2.resize(cropped, (max(1, int(round(w * scale))),
                                    max(1, int(round(h * scale)))),
                          interpolation=cv2.INTER_AREA)
    else:
        work = cropped
    gray = cv2.cvtColor(work, cv2.COLOR_RGB2GRAY)
    g = cv2.GaussianBlur(cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8)).apply(gray),
                         (5, 5), 0)
    a_work = a0 * s0 * scale
    r_lo = max(4, int(0.60 * a_work))
    r_hi = max(r_lo + 2, int(1.50 * a_work))
    found = cv2.HoughCircles(
        g, cv2.HOUGH_GRADIENT, dp=1.2,
        minDist=max(8, int(0.8 * a_work)), param1=100, param2=30,
        minRadius=r_lo, maxRadius=r_hi)
    if found is None:
        return None, 0
    best = None
    for c in found[0]:
        cx = (float(c[0]) / scale + ox) / s0
        cy = (float(c[1]) / scale + oy) / s0
        a = (float(c[2]) / scale) / s0
        rr = a / max(a0, 1.0)
        d = math.hypot(cx - cx0, cy - cy0) / max(a0, 1.0)
        if 0.65 <= rr <= 1.45 and d <= 0.35:
            score = abs(rr - 1.0) + d
            if best is None or score < best[0]:
                best = (score, cx, cy, a)
    if best is None:
        return None, len(found[0])
    return (best[1], best[2], best[3]), len(found[0])


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))] if v else float("nan")


def stats(name, rows):
    ce = [r["c"] for r in rows]
    rr = [r["ra"] for r in rows]
    print("%-18s n=%-3d center p50=%.3f p90=%.3f | radius p50=%.3f p90=%.3f"
          % (name, len(rows), pct(ce, .5), pct(ce, .9), pct(rr, .5), pct(rr, .9)))


def main():
    run = latest_run()
    items = json.loads(LABELS.read_text(encoding="utf-8"))["items"]
    print("run:", run.name, "items:", len(items))
    t0 = time.time()
    pred = model_coords(items, run)
    print("model coords: %.1fs (incl. image decode/cache)" % (time.time() - t0))

    # 纯模型推理时间
    model = build_model()
    model.eval()
    dummy = torch.zeros(1, 3, 256, 256)
    with torch.no_grad():
        model(dummy)
        t = time.time()
        for _ in range(20):
            model(dummy)
        infer_ms = (time.time() - t) / 20 * 1000
    print("model forward: %.1f ms" % infer_ms)

    base = json.loads((TRAIN / "hough_baseline.json").read_text(encoding="utf-8"))
    slow = {r["id"]: r for r in json.loads(
        (TRAIN / "compare.json").read_text(encoding="utf-8"))}

    rows_model, rows_fast, rows_slow, rows_hough = [], [], [], []
    fast_times, used_fast = [], 0
    for it in items:
        iid = it["id"]
        cx0, cy0, a0 = pred[iid]
        e = math.hypot(cx0 - it["cx"], cy0 - it["cy"]) / max(it["a"], 1.0)
        r = a0 / max(it["a"], 1.0)
        rows_model.append({"c": e, "ra": r, "arc": it["visible_arc"]})
        b = base.get(str(iid))
        if b:
            rows_hough.append({"c": b["center_err"], "ra": b["radius_ratio"],
                               "arc": it["visible_arc"]})
        if iid in slow:
            rows_slow.append({"c": slow[iid]["x_c"], "ra": slow[iid]["x_r"],
                              "arc": it["visible_arc"]})

        img = load_rgb(TRAIN / it["file"])
        t = time.time()
        got, nfound = fast_hough(img, cx0, cy0, a0)
        dt = time.time() - t
        fast_times.append(dt)
        if got is not None:
            cx, cy, a = got
            used_fast += 1
        else:
            cx, cy, a = cx0, cy0, a0
        e = math.hypot(cx - it["cx"], cy - it["cy"]) / max(it["a"], 1.0)
        r = a / max(it["a"], 1.0)
        rows_fast.append({"c": e, "ra": r, "arc": it["visible_arc"]})

    print("\n=== accuracy (out-of-fold) ===")
    stats("hough (5s)", rows_hough)
    stats("model only", rows_model)
    stats("model+fast hough", rows_fast)
    stats("slow hybrid 0.78", rows_slow)
    print("fast hough used: %d/%d | avg %.1f ms | p90 %.1f ms" %
          (used_fast, len(items), float(np.mean(fast_times)),
           float(np.percentile(fast_times, 90))))

    print("\n=== partial (<0.8) ===")
    stats("model only", [x for x in rows_model if x["arc"] < 0.8])
    stats("model+fast hough", [x for x in rows_fast if x["arc"] < 0.8])
    stats("slow hybrid 0.78", [x for x in rows_slow if x["arc"] < 0.8])


if __name__ == "__main__":
    main()
