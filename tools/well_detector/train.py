"""孔起点圆回归：MobileNetV3-Small + 直接回归 (cx, cy, a)，5 折交叉验证。

用法（在 jltx-train 环境里）：
  D:/anaconda3/envs/jltx-train/python.exe tools/well_detector/train.py

为什么是"直接回归"而不是热图/分割：
- 84 张手工标定里，81 张的圆跨出画面，圆心可能在画面外（16 张）；
  热图表示不了画面外的圆心，分割 mask 只剩半张。
- 模型输入整图（letterbox 到 512 的方形画布），输出归一化到画布边长的
  (cx, cy, log a)，数值不设上限 —— 圆心可以在画面外。

训练增强（对应用户说的"放大倍数和拍摄位置不同"）：
- 随机旋转、随机缩放、随机平移、随机方形裁切（故意制造更多残缺孔）、
  随机翻转、亮度/对比度/模糊/噪声；标签同步变换。
"""
import argparse
import json
import math
import random
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from sklearn.model_selection import KFold
from torch import nn
from torch.utils.data import DataLoader, Dataset
from torchvision.models import MobileNet_V3_Small_Weights, mobilenet_v3_small

ROOT = Path(__file__).resolve().parents[2]
TRAIN = ROOT / "data" / "train_well"
LABELS = TRAIN / "labels.json"
BASELINE = TRAIN / "hough_baseline.json"
CANVAS = 512          # letterbox 画布边长
INPUT = 256           # 模型输入边长
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)


def load_rgb(path: Path) -> np.ndarray:
    # 项目根目录有中文，np.fromfile 在 Windows 上可能读不了；用 Python 的文件 API。
    buf = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"读不出图片: {path}")
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def letterbox(img: np.ndarray, size: int):
    h, w = img.shape[:2]
    s = size / float(max(h, w))
    nw, nh = max(1, int(round(w * s))), max(1, int(round(h * s)))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((size, size, 3), np.uint8)
    ox, oy = (size - nw) // 2, (size - nh) // 2
    canvas[oy:oy + nh, ox:ox + nw] = resized
    return canvas, s, ox, oy


def visible_arc(cx, cy, a, w, h, n=96):
    ok = 0
    for k in range(n):
        t = math.radians(360.0 * k / n)
        x, y = cx + a * math.cos(t), cy + a * math.sin(t)
        if 0 <= x <= w - 1 and 0 <= y <= h - 1:
            ok += 1
    return ok / float(n)


def augment(canvas, cx, cy, a):
    """返回 (crop, cx, cy, a, side)。crop 是方形，保证圆不被拉伸。"""
    H = canvas.shape[0]
    side = H
    for _ in range(4):
        ang = random.uniform(-180.0, 180.0)
        sc = math.exp(random.uniform(math.log(0.7), math.log(1.4)))
        M = cv2.getRotationMatrix2D((H / 2.0, H / 2.0), ang, sc)
        M[0, 2] += random.uniform(-0.15, 0.15) * H
        M[1, 2] += random.uniform(-0.15, 0.15) * H
        img = cv2.warpAffine(canvas, M, (H, H), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
        cx2 = M[0, 0] * cx + M[0, 1] * cy + M[0, 2]
        cy2 = M[1, 0] * cx + M[1, 1] * cy + M[1, 2]
        a2 = a * sc
        side = random.randint(int(H * 0.6), H)
        x0 = random.randint(0, H - side)
        y0 = random.randint(0, H - side)
        crop = img[y0:y0 + side, x0:x0 + side]
        cx2 -= x0
        cy2 -= y0
        if visible_arc(cx2, cy2, a2, side, side) >= 0.2:
            break

    if random.random() < 0.5:
        crop = crop[:, ::-1]
        cx2 = side - 1 - cx2
    if random.random() < 0.5:
        crop = crop[::-1]
        cy2 = side - 1 - cy2

    # 颜色/模糊/噪声：只改像素，不动标签
    crop = crop.astype(np.float32)
    crop = crop * random.uniform(0.8, 1.2) + random.uniform(-20, 20)
    if random.random() < 0.3:
        crop = cv2.GaussianBlur(crop, (3, 3), 0)
    if random.random() < 0.3:
        crop += np.random.normal(0, 5, crop.shape)
    crop = np.clip(crop, 0, 255).astype(np.uint8)
    return crop, cx2, cy2, a2, side


class WellDataset(Dataset):
    def __init__(self, items, train):
        self.items = items
        self.train = train
        self.cache = []
        for it in items:
            img = load_rgb(TRAIN / it["file"])
            canvas, s, ox, oy = letterbox(img, CANVAS)
            self.cache.append({
                "canvas": canvas, "s": s, "ox": ox, "oy": oy,
                "cx": it["cx"] * s + ox, "cy": it["cy"] * s + oy,
                "a": it["a"] * s, "item": it,
            })

    def __len__(self):
        return len(self.cache)

    def __getitem__(self, idx):
        c = self.cache[idx]
        canvas, cx, cy, a = c["canvas"], c["cx"], c["cy"], c["a"]
        side = CANVAS
        if self.train:
            canvas, cx, cy, a, side = augment(canvas, cx, cy, a)
        img = cv2.resize(canvas, (INPUT, INPUT), interpolation=cv2.INTER_AREA)
        x = img.astype(np.float32) / 255.0
        x = (x - MEAN) / STD
        x = np.transpose(x, (2, 0, 1)).copy()
        target = np.array([cx / side, cy / side, math.log(max(a, 1.0) / side)],
                          np.float32)
        return (torch.from_numpy(x), torch.from_numpy(target),
                torch.tensor(c["item"]["id"], dtype=torch.long))


class WellNet(nn.Module):
    """双头直接回归：8×8 语义特征管半径，16×16 空间特征 + 坐标通道管圆心。

    ⚠️ 不能用 MobileNetV3 原来的分类头：全局平均池化会把"孔在画面哪里"丢光。
    第一轮用 8×8 空间图回归，半径学得很好（中位 1.009），但圆心误差 0.229
    —— 8×8 一格约 0.125 个画面，精度被空间分辨率卡死。
    这一版把特征拆成两段：
      stem  = features[:9]  → 16×16×48（空间细，管圆心，拼 2 通道坐标网格）
      later = features[9:]  → 8×8×576（语义强，管"这是孔还是液滴"→ 半径）
    两个头都输出无上限的实数，圆心仍然允许落在画面外。
    """

    def __init__(self):
        super().__init__()
        m = mobilenet_v3_small(weights=MobileNet_V3_Small_Weights.IMAGENET1K_V1)
        self.stem = m.features[:9]
        self.later = m.features[9:]
        grid = torch.stack(torch.meshgrid(
            torch.linspace(-1, 1, 16), torch.linspace(-1, 1, 16),
            indexing="ij")[::-1], dim=0).unsqueeze(0)
        self.register_buffer("grid", grid)          # 1×2×16×16

        self.center_head = nn.Sequential(
            nn.Conv2d(48 + 2, 32, 1), nn.ReLU(inplace=True),
            nn.Flatten(), nn.Dropout(0.2),
            nn.Linear(32 * 16 * 16, 256), nn.ReLU(inplace=True),
            nn.Linear(256, 2))
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.radius_head = nn.Sequential(
            nn.Linear(576, 256), nn.ReLU(inplace=True), nn.Dropout(0.2),
            nn.Linear(256, 1))
        nn.init.normal_(self.center_head[-1].weight, std=0.01)
        nn.init.normal_(self.radius_head[-1].weight, std=0.01)
        with torch.no_grad():
            self.center_head[-1].bias.copy_(torch.tensor([0.5, 0.5]))
            self.radius_head[-1].bias.fill_(math.log(0.4))

    def forward(self, x):
        f16 = self.stem(x)
        f8 = self.later(f16)
        g = self.grid.expand(f16.shape[0], -1, -1, -1)
        center = self.center_head(torch.cat([f16, g], dim=1))
        radius = self.radius_head(torch.flatten(self.pool(f8), 1))
        return torch.cat([center, radius], dim=1)


def build_model():
    return WellNet()


def run_epoch(model, loader, device, criterion, opt=None):
    train = opt is not None
    model.train(train)
    losses, preds = [], []
    with torch.set_grad_enabled(train):
        for x, y, ids in loader:
            x, y = x.to(device), y.to(device)
            out = model(x)
            loss = criterion(out, y)
            if train:
                opt.zero_grad()
                loss.backward()
                opt.step()
            losses.append(float(loss.item()) * len(ids))
            preds.append((out.detach().cpu().numpy(), ids.numpy()))
    total = sum(losses) / max(1, sum(len(p[1]) for p in preds))
    return total, preds


def inverse_to_original(pred, cache_by_id, item):
    """pred=(cx_rel, cy_rel, log_a_rel) → 原图坐标。"""
    # 验证集没有裁切，side == CANVAS
    cx_canvas = float(pred[0]) * CANVAS
    cy_canvas = float(pred[1]) * CANVAS
    a_canvas = math.exp(float(pred[2])) * CANVAS
    c = cache_by_id[item["id"]]
    cx = (cx_canvas - c["ox"]) / c["s"]
    cy = (cy_canvas - c["oy"]) / c["s"]
    a = a_canvas / c["s"]
    err = math.hypot(cx - item["cx"], cy - item["cy"]) / max(item["a"], 1.0)
    ratio = a / max(item["a"], 1.0)
    return err, ratio


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))] if v else float("nan")


def summarize(name, rows):
    ce = [r["center_err"] for r in rows]
    rr = [r["radius_ratio"] for r in rows]
    part = [r for r in rows if r["visible_arc"] < 0.8]
    print("%s n=%d | center p50=%.3f p90=%.3f | radius_ratio p50=%.3f p90=%.3f"
          % (name, len(rows), pct(ce, .5), pct(ce, .9), pct(rr, .5), pct(rr, .9)))
    if part:
        print("   partial n=%d | center p50=%.3f p90=%.3f | radius_ratio p50=%.3f p90=%.3f"
              % (len(part), pct([r["center_err"] for r in part], .5),
                 pct([r["center_err"] for r in part], .9),
                 pct([r["radius_ratio"] for r in part], .5),
                 pct([r["radius_ratio"] for r in part], .9)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--epochs-head", type=int, default=40)
    ap.add_argument("--epochs-ft", type=int, default=40)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--lr-head", type=float, default=1e-3)
    ap.add_argument("--lr-ft", type=float, default=1e-4)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=str, default="")
    args = ap.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print("device:", device, "torch:", torch.__version__)

    data = json.loads(LABELS.read_text(encoding="utf-8"))
    items = data["items"]
    kf = KFold(n_splits=args.folds, shuffle=True, random_state=args.seed)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = Path(args.out) if args.out else TRAIN / "runs" / stamp
    outdir.mkdir(parents=True, exist_ok=True)
    print("out:", outdir, "items:", len(items))

    criterion = nn.SmoothL1Loss(beta=0.05)
    all_rows = []
    for fold, (tr_idx, va_idx) in enumerate(kf.split(items), 1):
        print("\n=== fold %d/%d  train=%d val=%d ===" %
              (fold, args.folds, len(tr_idx), len(va_idx)))
        tr_items = [items[i] for i in tr_idx]
        va_items = [items[i] for i in va_idx]
        tr_ds = WellDataset(tr_items, True)
        va_ds = WellDataset(va_items, False)
        tr_loader = DataLoader(tr_ds, batch_size=args.batch, shuffle=True, num_workers=0)
        va_loader = DataLoader(va_ds, batch_size=args.batch, shuffle=False, num_workers=0)
        cache_by_id = {c["item"]["id"]: c for c in va_ds.cache}

        model = build_model().to(device)
        for p in model.stem.parameters():
            p.requires_grad = False
        for p in model.later.parameters():
            p.requires_grad = False
        opt = torch.optim.AdamW(
            [p for p in model.parameters() if p.requires_grad],
            lr=args.lr_head, weight_decay=1e-4)

        best = {"loss": 1e18, "state": None, "preds": None}
        since = 0
        for phase, epochs, lr in (("head", args.epochs_head, args.lr_head),
                                  ("ft", args.epochs_ft, args.lr_ft)):
            if phase == "ft":
                for p in model.later[-2:].parameters():
                    p.requires_grad = True
                for p in model.stem[-2:].parameters():
                    p.requires_grad = True
                opt = torch.optim.AdamW(
                    [p for p in model.parameters() if p.requires_grad],
                    lr=lr, weight_decay=1e-4)
                since = 0          # 两阶段各自早停，别让上一阶段的计数提前掐死这一阶段
            for ep in range(1, epochs + 1):
                tl, _ = run_epoch(model, tr_loader, device, criterion, opt)
                vl, vp = run_epoch(model, va_loader, device, criterion, None)
                if vl < best["loss"]:
                    best = {
                        "loss": vl,
                        "state": {k: v.detach().cpu().clone()
                                  for k, v in model.state_dict().items()},
                        "preds": vp,
                    }
                    since = 0
                else:
                    since += 1
                    if since >= 12:
                        break
                if ep % 10 == 0 or ep == 1:
                    print("  %s ep%02d train=%.4f val=%.4f" % (phase, ep, tl, vl),
                          flush=True)
        model.load_state_dict(best["state"])
        torch.save(best["state"], outdir / f"fold{fold}.pt")

        rows = []
        for out, ids in best["preds"]:
            for j, iid in enumerate(ids):
                it = next(x for x in va_items if x["id"] == int(iid))
                err, ratio = inverse_to_original(out[j], cache_by_id, it)
                row = {"id": int(iid), "name": it["name"],
                       "center_err": err, "radius_ratio": ratio,
                       "visible_arc": it["visible_arc"]}
                rows.append(row)
        all_rows.extend(rows)
        print("  fold best val=%.4f | center p50=%.3f | radius_ratio p50=%.3f"
              % (best["loss"], pct([r["center_err"] for r in rows], .5),
                 pct([r["radius_ratio"] for r in rows], .5)))

    (outdir / "predictions.json").write_text(
        json.dumps(all_rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n=== FINAL 5-fold ===")
    summarize("model", all_rows)
    summarize("model", [r for r in all_rows if r["visible_arc"] >= 0.8])

    if BASELINE.exists():
        base = json.loads(BASELINE.read_text(encoding="utf-8"))
        base_rows = []
        for r in all_rows:
            b = base.get(str(r["id"]))
            if b:
                base_rows.append({"center_err": b["center_err"],
                                  "radius_ratio": b["radius_ratio"],
                                  "visible_arc": r["visible_arc"]})
        summarize("hough", base_rows)

    print("saved:", outdir)


if __name__ == "__main__":
    main()
