"""开机自启的开关：装 / 卸 / 看状态。

为什么逻辑写在 Python 里、.bat 只是个空壳：
和 启动.bat 同一个原因 —— cmd.exe 按本地代码页读 .bat 文件，中文写进去必然乱码。
.vbs 那边没这个问题（WScript 直接按文本读，不经过 cmd 解析）。

⚠️ 启动文件夹里那份 JLTX-autostart.vbs 是**现生成**的，不是仓库里的固定文件。
   它必须写死两个本机路径（这个工具装在哪儿、pythonw 在哪儿），固定文件就只能
   硬编码某台机器的路径 —— 换台机器就是坏的，公开出去还会暴露目录结构。
   现在从 config.PROJECT_ROOT 和 sys.executable 现场推，谁装谁对。
   所以：**挪了文件夹要重新 install 一次**（status 会提醒）。

为什么启动项叫 JLTX-autostart.vbs 这个纯英文名：
「取消开机自启.bat」得能删掉它，而 .bat 里一个中文都不能写。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from app import config

STARTUP_DIR = (Path(os.environ.get("APPDATA", ""))
               / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")
ENTRY_NAME = "JLTX-autostart.vbs"


def entry_path() -> Path:
    return STARTUP_DIR / ENTRY_NAME


def _pythonw() -> Path | None:
    """找 pythonw.exe（没有控制台的 Python）。

    用 python.exe 的话开机时会闪一个黑窗口出来 —— 而开机自启的全部意义就是
    安安静静。同一个目录下一般都有 pythonw.exe。
    """
    cand = Path(sys.executable).with_name("pythonw.exe")
    return cand if cand.is_file() else None


def _vbs_text(pythonw: Path) -> str:
    """生成 .vbs 的内容。

    第 2 个参数 0 = 隐藏窗口，第 3 个 False = 不等它结束。
    JLTX_NO_BROWSER=1 让 launch.py 别弹浏览器：开机时用户可能正在做别的事。
    """
    root = config.PROJECT_ROOT
    return (
        "' 开机自启入口 —— 由 app/autostart.py 生成，别手改（要改去改源头）。\n"
        f"' 这个工具装在：{root}\n"
        f"' 用的 Python：{pythonw}\n"
        "Dim sh\n"
        'Set sh = CreateObject("WScript.Shell")\n'
        f'sh.CurrentDirectory = "{root}"\n'
        'sh.Environment("PROCESS")("JLTX_NO_BROWSER") = "1"\n'
        f'sh.Run """{pythonw}"" -m app.launch", 0, False\n'
    )


def install() -> int:
    pythonw = _pythonw()
    if pythonw is None:
        print()
        print("  装不了：没找到 pythonw.exe。")
        print(f"  找的是：{Path(sys.executable).with_name('pythonw.exe')}")
        print("  pythonw 是「没有黑窗口的 Python」，开机自启必须用它。")
        print()
        return 1

    STARTUP_DIR.mkdir(parents=True, exist_ok=True)
    text = _vbs_text(pythonw)
    # 带 BOM 的 UTF-16LE：WScript 读没有 BOM 的文件会当 ANSI，
    # 项目路径里的中文（比如「晶体大小测量」）会当场乱掉。
    entry_path().write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))

    print()
    print("  已开启开机自启。")
    print("  以后开机时它会静静在后台跑 —— 不弹浏览器，也不弹黑窗口。")
    print("  想看测量页面，双击「启动.bat」—— 它发现已经在跑了，")
    print("  就直接把浏览器打开，不会再多起一个。")
    print()
    print(f"  启动项：{entry_path()}")
    print(f"  它指向：{config.PROJECT_ROOT}")
    print()
    # 这里一律不要用 emoji（比如 ⚠）：输出接到真控制台时 Python 走 Windows
    # 控制台 API，什么字符都画得出来；但一旦被重定向成管道或文件，就改用
    # CP936 编码，emoji 编不出来会直接抛 UnicodeEncodeError ——
    # 一句 print 就能把整个命令打崩，退出码还变成 1。
    print("  注意：这个指向是写死的 —— 把工具文件夹挪走或改名之后，")
    print("  要重新双击一次 安装开机自启.bat，不然开机起的还是老路径。")
    print()
    return 0


def uninstall() -> int:
    p = entry_path()
    if not p.exists():
        print()
        print("  本来就没开开机自启，不用关。")
        print()
        return 0
    try:
        p.unlink()
    except OSError as e:
        print()
        print(f"  删不掉，可能被安全软件挡住了（{e}）。")
        print("  可以手动删掉这个文件：")
        print(f"    {p}")
        print()
        return 1
    print()
    print("  已关闭开机自启。")
    print("  工具本身没受影响 —— 需要的时候双击 启动.bat 照样能用。")
    print()
    return 0


def status() -> int:
    p = entry_path()
    print()
    if not p.exists():
        print("  开机自启：未开启")
        print("  想打开的话，双击 安装开机自启.bat。")
        print()
        return 0

    print("  开机自启：已开启")
    print(f"  启动项：{p}")
    # 启动项里写死了路径。文件夹被挪走之后它不会报错，只会静默指向老地方 ——
    # 所以这里主动比一下，别等用户遇到"开机没反应"再来查。
    try:
        text = p.read_bytes().decode("utf-16")
    except (UnicodeDecodeError, OSError):
        text = ""
    if str(config.PROJECT_ROOT) in text:
        print(f"  它指向：{config.PROJECT_ROOT}（就是现在这个文件夹，对）")
    else:
        print("  注意：它指向的不是现在这个文件夹 —— 多半是工具被挪过或改过名。")
        print("  重新双击一次 安装开机自启.bat 就能修正。")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0].lower() if args else "status"
    table = {"install": install, "on": install,
             "uninstall": uninstall, "off": uninstall,
             "status": status}
    if cmd not in table:
        print()
        print("  用法：python -m app.autostart [install | uninstall | status]")
        print()
        return 2
    return table[cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
