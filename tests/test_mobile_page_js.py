"""手机页的内联 JS 也要过语法检查。

手机页**不是文件** —— 它是 app/api/mobile.py 里的 Python 字符串 `_PAGE`，
不在 web/ 底下，tests/web_check.mjs 默认那趟扫描根本看不见它。也就是说
往手机页插的 JS 一行都得不到保护，而语法错了浏览器只会**白屏**：
用户看不懂控制台，只会以为工具坏了。所以这里把 `_PAGE` 落成临时文件，
交给同一个脚本查。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from app.api import mobile

# 仓库根：按本测试文件的位置定位，不依赖当前工作目录
ROOT = Path(__file__).resolve().parent.parent


def test_mobile_page_inline_js_parses(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("这台机器没装 node，跳过手机页的 JS 语法检查")

    page = tmp_path / "mobile_page.html"
    page.write_text(mobile._PAGE, encoding="utf-8")

    r = subprocess.run([node, str(ROOT / "tests" / "web_check.mjs"), str(page)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    assert r.returncode == 0, f"手机页的 JS 有语法错误：\n{r.stdout}\n{r.stderr}"
    # ⚠️ 光看 returncode 会空转：脚本对"没有内联 script"的文件是打印一句"跳过"
    # 然后返回 0；参数解析哪天退化了也是 0。这两种情况下这条测试全绿，却什么都没查。
    assert "mobile_page" in r.stdout, f"没真查到这个文件：\n{r.stdout}"
