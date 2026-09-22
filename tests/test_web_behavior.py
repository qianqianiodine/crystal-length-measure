"""两个页面的**行为**检查（跑 tests/web_behavior_check.mjs）。

`tests/web_check.mjs` 只证明内联 JS 编译得过 —— 它拦不住"失败行的输入框还是灰的"
这种逻辑错误。这里把页面放进一个最小 DOM 里真的跑起来：点按钮、调函数，
再看它吐出来的 DOM。用户要求功能由他人工测、不让 Claude 开浏览器，
这是最接近"真点一下"的验证方式。

手机页不是文件（它是 app/api/mobile.py 里的 Python 字符串 `_PAGE`），
所以先落成临时文件再交给脚本；不给它的话，脚本会跳过手机页那几条检查。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from app.api import mobile

ROOT = Path(__file__).resolve().parent.parent


def test_both_pages_behave(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("这台机器没装 node，跳过页面行为检查")

    page = tmp_path / "mobile_page.html"
    page.write_text(mobile._PAGE, encoding="utf-8")

    r = subprocess.run([node, str(ROOT / "tests" / "web_behavior_check.mjs"), str(page)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, f"页面行为检查没过：\n{r.stdout}\n{r.stderr}"
    # 两块都真跑过才算数：只跑图库页的话，手机页那几条会静默空转
    assert "图库页" in r.stdout, r.stdout
    assert "手机页" in r.stdout, r.stdout
