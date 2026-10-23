"""用全部 84 张训练最终模型，导出 ONNX 给 app 用。

用法：
  D:/anaconda3/envs/jltx-train/python.exe tools/well_detector/train_final.py

- 5 折 CV 已经给出诚实的性能估计（混合圆心 0.134 / 半径 0.979）；
  这里是在全部数据上重训一个模型，给线上用。
- 导出后立刻用 onnxruntime 跑一遍，和 PyTorch 输出对比，确认导出没错。
"""
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from train import (LABELS, TRAIN, WellDataset, build_model,  # noqa: E402
                   run_epoch)

OUT_MODEL = ROOT / "app" / "models" / "well_regressor.onnx"


def main():
    random_seed = 42
    torch.manual_seed(random_seed)
    np.random.seed(random_seed)
    import random
    random.seed(random_seed)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print("device:", device)
    items = json.loads(LABELS.read_text(encoding="utf-8"))["items"]
    ds = WellDataset(items, True)
    loader = DataLoader(ds, batch_size=16, shuffle=True, num_workers=0)
    model = build_model().to(device)

    for p in model.stem.parameters():
        p.requires_grad = False
    for p in model.later.parameters():
        p.requires_grad = False
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                            lr=1e-3, weight_decay=1e-4)
    crit = nn.SmoothL1Loss(beta=0.05)

    t0 = time.time()
    print("stage 1: head only")
    for ep in range(1, 41):
        tl, _ = run_epoch(model, loader, device, crit, opt)
        if ep % 10 == 0 or ep == 1:
            print("  ep%02d loss=%.4f" % (ep, tl), flush=True)

    print("stage 2: fine-tune last blocks")
    for p in model.later[-2:].parameters():
        p.requires_grad = True
    for p in model.stem[-2:].parameters():
        p.requires_grad = True
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                            lr=1e-4, weight_decay=1e-4)
    for ep in range(1, 41):
        tl, _ = run_epoch(model, loader, device, crit, opt)
        if ep % 10 == 0 or ep == 1:
            print("  ep%02d loss=%.4f" % (ep, tl), flush=True)

    torch.save(model.state_dict(), TRAIN / "final.pt")
    print("trained in %.1fs" % (time.time() - t0))

    model.cpu().eval()
    OUT_MODEL.parent.mkdir(parents=True, exist_ok=True)
    dummy = torch.zeros(1, 3, 256, 256)
    torch.onnx.export(
        model, dummy, str(OUT_MODEL),
        input_names=["input"], output_names=["output"],
        opset_version=17, do_constant_folding=True)
    print("onnx:", OUT_MODEL, "%.1f MB" % (OUT_MODEL.stat().st_size / 1048576))

    # onnxruntime 和 PyTorch 对比
    import onnxruntime as ort
    sess = ort.InferenceSession(str(OUT_MODEL), providers=["CPUExecutionProvider"])
    iname = sess.get_inputs()[0].name
    max_diff = 0.0
    for it in items[:5]:
        one = WellDataset([it], False)
        x, _y, _i = one[0]
        with torch.no_grad():
            t_out = model(x[None])[0].numpy()
        o_out = sess.run(None, {iname: x[None].numpy()})[0][0]
        max_diff = max(max_diff, float(np.max(np.abs(t_out - o_out))))
    print("onnx vs torch max abs diff: %.2e" % max_diff)


if __name__ == "__main__":
    main()
