"""对比：现有 Hough / 新模型 / 混合策略。

用法：D:/anaconda3/envs/jltx-train/python.exe tools/well_detector/compare.py

混合规则（先按最朴素的来）：
- Hough 没找到孔（fallback）→ 用模型；
- Hough 的半径比模型小 15% 以上（说明它找到的是液滴/小环）→ 用模型；
- 否则用 Hough（完整/清晰的孔上它更准）。

这里只用已经存下来的误差（center_err / radius_ratio），不需要预测坐标：
混合后的误差就是被选中那一方在该图上的误差。
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRAIN = ROOT / "data" / "train_well"


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))] if v else float("nan")


def stats(rows, key_c, key_r):
    ce = [r[key_c] for r in rows]
    rr = [r[key_r] for r in rows]
    return (pct(ce, .5), pct(ce, .9), pct(rr, .5), pct(rr, .9))


def line(name, rows, key_c, key_r):
    if not rows:
        print("%-10s n=0" % name); return
    c50, c90, r50, r90 = stats(rows, key_c, key_r)
    print("%-10s n=%-3d center p50=%.3f p90=%.3f | radius_ratio p50=%.3f p90=%.3f"
          % (name, len(rows), c50, c90, r50, r90))


def main():
    runs = sorted((TRAIN / "runs").glob("*/predictions.json"),
                  key=lambda p: os.path.getmtime(p))
    if not runs:
        raise SystemExit("没有 predictions.json")
    latest = runs[-1]
    model = json.loads(latest.read_text(encoding="utf-8"))
    base = json.loads((TRAIN / "hough_baseline.json").read_text(encoding="utf-8"))
    print("model run:", latest.parent.name, "| model rows:", len(model),
          "| hough rows:", len(base))

    rows = []
    for r in model:
        b = base.get(str(r["id"]))
        if not b:
            continue
        h_ratio, m_ratio = b["radius_ratio"], r["radius_ratio"]
        use_model = bool(b.get("fallback")) or (h_ratio / max(m_ratio, 1e-6) < 0.85)
        rows.append({
            "id": r["id"], "visible_arc": r["visible_arc"],
            "h_c": b["center_err"], "h_r": h_ratio,
            "m_c": r["center_err"], "m_r": m_ratio,
            "x_c": r["center_err"] if use_model else b["center_err"],
            "x_r": m_ratio if use_model else h_ratio,
            "use_model": use_model,
        })

    n = len(rows)
    print("paired rows:", n,
          "| hybrid uses model:", sum(1 for r in rows if r["use_model"]))
    line("hough", rows, "h_c", "h_r")
    line("model", rows, "m_c", "m_r")
    line("hybrid", rows, "x_c", "x_r")

    part = [r for r in rows if r["visible_arc"] < 0.8]
    full = [r for r in rows if r["visible_arc"] >= 0.8]
    print("\n-- partial (<0.8), n=%d --" % len(part))
    line("hough", part, "h_c", "h_r")
    line("model", part, "m_c", "m_r")
    line("hybrid", part, "x_c", "x_r")
    print("\n-- full (>=0.8), n=%d --" % len(full))
    line("hough", full, "h_c", "h_r")
    line("model", full, "m_c", "m_r")
    line("hybrid", full, "x_c", "x_r")

    # Hough 明显失败的图：混合在这里应该体现价值
    bad = [r for r in rows if r["h_c"] > 0.20]
    print("\n-- Hough center > 0.20, n=%d --" % len(bad))
    line("hough", bad, "h_c", "h_r")
    line("model", bad, "m_c", "m_r")
    line("hybrid", bad, "x_c", "x_r")

    # oracle 上限：每张图挑两者中圆心误差更小的
    oracle = [{"c": min(r["h_c"], r["m_c"]),
               "r": r["h_r"] if r["h_c"] <= r["m_c"] else r["m_r"]} for r in rows]
    print("\n-- oracle upper bound --")
    print("oracle     n=%-3d center p50=%.3f p90=%.3f | radius_ratio p50=%.3f p90=%.3f"
          % (n, pct([o["c"] for o in oracle], .5), pct([o["c"] for o in oracle], .9),
             pct([o["r"] for o in oracle], .5), pct([o["r"] for o in oracle], .9)))

    out = TRAIN / "compare.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nwrote", out)


if __name__ == "__main__":
    main()
