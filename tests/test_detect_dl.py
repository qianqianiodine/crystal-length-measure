"""模型推理的裁切精修：坐标换算和越界补黑。"""
import math
from types import SimpleNamespace

import numpy as np
import pytest

from app import detect_dl


class _FakeSession:
    """按顺序吐出预设的 (cx, cy, log a)，单位是 512 画布。"""

    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = 0

    def get_inputs(self):
        return [SimpleNamespace(name="input")]

    def run(self, _names, _feed):
        out = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        return [np.array([out], np.float32)]


def test_predict_refines_around_the_first_circle(monkeypatch):
    monkeypatch.delenv("JLTX_NO_DL", raising=False)
    # 第一次：圆在 (150, 100)、半径 75；后两次：裁切里半径占 0.4 画布 → 仍 75。
    sess = _FakeSession([
        (0.5, 0.5, math.log(0.25)),
        (0.5, 0.5, math.log(0.40)),
        (0.5, 0.5, math.log(0.40)),
    ])
    monkeypatch.setattr(detect_dl, "_session", lambda: sess)

    img = np.zeros((200, 300, 3), np.uint8)
    c = detect_dl.predict(img)

    assert sess.calls == 3                 # 整图一次 + 围着裁两次
    assert c["cx"] == pytest.approx(150.0, abs=1.0)
    assert c["cy"] == pytest.approx(100.0, abs=1.0)
    assert c["a"] == pytest.approx(75.0, abs=1.5)
    assert c["b"] == c["a"] and c["th"] == 0.0


def test_crop_to_canvas_pads_outside_the_image():
    img = np.full((100, 100, 3), 200, np.uint8)
    canvas, x0, y0, side = detect_dl._crop_to_canvas(img, 50, 50, 200)
    assert (x0, y0, side) == (-50, -50, 200)
    assert canvas[0, 0].tolist() == [0, 0, 0]      # 图外补黑
    assert canvas[256, 256].tolist() == [200, 200, 200]
