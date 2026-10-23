"""自动化测试默认禁用 ONNX 模型。

线上 `bgdetect` 是"模型优先"；测试图大多是 80×60 的合成小图，不该让模型参与，
否则测试结果会依赖模型文件、不可重复。需要测模型选择的用例自己 monkeypatch
`bgdetect.detect_dl.predict`。
"""
import os

os.environ.setdefault("JLTX_NO_DL", "1")
