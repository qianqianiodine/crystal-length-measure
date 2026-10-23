"""用**当前**的规则检测器跑 84 张手工标定图，存基线误差。

用法：D:/anaconda3/python.exe -u tools/well_detector/baseline.py

行为完全照 app 现在的 `_rough_circle`：
- 有候选 → 取评分最高的 `cands[0]`；
- 没候选 → 居中的兜底圆（半径 = 0.25 × 短边）。

误差定义（和训练脚本一致）：
- center_err = 预测圆心到手工圆心的距离 / 手工半径；
- radius_ratio = 预测半径 / 手工半径。
"""
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import detect, imageio  # noqa: E402

TRAIN = ROOT / "data" / "train_well"
LABELS = TRAIN / "labels.json"
OUT = TRAIN / "hough_baseline.json"


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))] if v else float("nan")


def main():
    data = json.loads(LABELS.read_text(encoding="utf-8"))
    items = data["items"]
    results = {}
    t_start = time.time()
    for k, it in enumerate(items, 1):
        img = imageio.load_rgb(TRAIN / it["file"])
        t0 = time.time()
        try:
            cands = detect.detect_wells(img)
        except Exception:
            cands = []
        if cands:
            c = cands[0]
            cx, cy, a = float(c.center[0]), float(c.center[1]), float(c.radius_px)
            fallback = False
        else:
            cx, cy, a = it["width"] / 2.0, it["height"] / 2.0, 0.25 * min(it["width"], it["height"])
            fallback = True
        dt = time.time() - t0
        err = math.hypot(cx - it["cx"], cy - it["cy"]) / max(it["a"], 1.0)
        ratio = a / max(it["a"], 1.0)
        results[str(it["id"])] = {
            "center_err": err, "radius_ratio": ratio,
            "fallback": fallback, "seconds": dt,
            "visible_arc": it["visible_arc"],
        }
        print("%2d/%d %-22s center=%.3f ratio=%.3f %.1fs%s" % (
            k, len(items), (it["name"] or "")[:22], err, ratio, dt,
            " FALLBACK" if fallback else ""), flush=True)

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    vals = list(results.values())
    ce = [v["center_err"] for v in vals]
    rr = [v["radius_ratio"] for v in vals]
    part = [v for v in vals if v["visible_arc"] < 0.8]
    pce = [v["center_err"] for v in part]
    prr = [v["radius_ratio"] for v in part]
    print("\nBASELINE n=%d fallback=%d" % (len(vals), sum(1 for v in vals if v["fallback"])))
    print("all: center p50=%.3f p90=%.3f | radius_ratio p50=%.3f p90=%.3f" %
          (pct(ce, .5), pct(ce, .9), pct(rr, .5), pct(rr, .9)))
    print("partial(<0.8): n=%d center p50=%.3f p90=%.3f | radius_ratio p50=%.3f p90=%.3f" %
          (len(part), pct(pce, .5), pct(pce, .9), pct(prr, .5), pct(prr, .9)))
    print("within 0.10 center: %d/%d | radius within 10%%: %d/%d" % (
        sum(1 for v in ce if v <= .10), len(ce),
        sum(1 for v in rr if abs(v - 1) <= .10), len(rr)))
    print("wrote", OUT, "total %.1fs" % (time.time() - t_start))


if __name__ == "__main__":
    main()
