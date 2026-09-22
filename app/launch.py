"""双击「启动.bat」之后真正跑起来的东西。

为什么中文提示写在这里、而不是写在 .bat 里：
cmd.exe 是按**本地代码页**（中文 Windows 是 CP936/GBK）去读 .bat 文件的，
而文件本身是 UTF-8 —— 两套编码对不上，中文必然乱码。`chcp 65001` 也救不了，
cmd 读批处理文件本身就有 bug，还可能让脚本提前退出。
放在 Python 里就没这个问题：Python 会按控制台代码页正确编码再输出。

浏览器也在这里开，不用 .bat 里那套 `start /min cmd /c "timeout & start"`
—— 嵌套引号太容易出错，而且服务没起来就开浏览器会白屏。

端口会**自动找空位**：这台机器上 8501 被另一个 Streamlit 项目长期占着，
写死端口就会起不来。
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from app import config

LAN_HOST = "0.0.0.0"      # 绑所有网卡 —— 手机传图要连得进来。
                          # 别担心：app/main.py 里的守卫默认拒掉一切局域网请求，
                          # 只有你在界面上点「开启手机传图」才开一个 30 分钟的口子。
PROBE_HOST = "127.0.0.1"  # ⚠️ 探测一律用这个，不要改成 LAN_HOST。
                          # Windows 上连 0.0.0.0 会失败（WinError 10049），
                          # 用它会让「端口自动找空位」和「工具已经在跑了」两个分支静默失效。
PREFERRED_PORT = 8510     # 避开 8501（Streamlit 默认端口，本机已被占用）
PORT_TRIES = 10


def _url(port: int) -> str:
    return f"http://{PROBE_HOST}:{port}"


def _port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex((PROBE_HOST, port)) == 0


def _running_version(port: int) -> int | None:
    """该端口上跑的是不是我们这个工具？是的话返回它的**接口版本**，否则 None。

    不能只判"活着没有"：老进程会让页面是新的、接口是旧的，用户看到的是一堆
    英文 "Not Found"，根本猜不到该重启。
    """
    try:
        with urllib.request.urlopen(f"{_url(port)}/api/health", timeout=1.5) as r:
            d = json.loads(r.read())
        return int(d.get("api", 0)) if d.get("status") == "ok" else None
    except Exception:
        return None


def _open_browser_later(port: int, delay: float = 2.5) -> None:
    time.sleep(delay)
    webbrowser.open(_url(port))


def _no_browser() -> bool:
    """开机自启时设 JLTX_NO_BROWSER=1 —— 静静在后台跑，别弹浏览器。

    自启那份是由启动文件夹里的 .vbs 拉起来的（隐藏窗口），开机时用户可能正
    在用电脑做别的事，突然弹一个网页出来很烦。要看得自己点桌面上的「打开测量」。
    """
    return os.environ.get("JLTX_NO_BROWSER") == "1"


def _fix_std_streams() -> None:
    """没控制台时给 stdout/stderr 接个黑洞。

    pythonw.exe（开机自启用的就是它，为的是完全没有黑窗口）是 GUI 子系统的程序，
    被 WScript 拉起来时 **sys.stdout 和 sys.stderr 都是 None** —— 这时任何一句
    print() 都会抛 AttributeError，工具当场崩掉。

    而且是最难查的那种崩：连报错信息都写不出去（stderr 也是 None），用户只会看到
    「双击了但工具没起来」，什么线索都没有。

    双击 启动.bat 时走不到这个分支（控制台在，stdout 有效），所以只影响自启。
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def main() -> None:
    _fix_std_streams()
    running = _running_version(PREFERRED_PORT)
    if running is not None:
        print()
        if running < config.API_VERSION:
            # 最坑的一种情况：老进程还活着，于是页面（每次现读磁盘）是新的、
            # 接口是旧的。用户只会看到英文报错，猜不到该重启。
            # 别用 emoji：stdout 接到真控制台时 Python 走 Windows 控制台 API，
            # 什么都画得出；一旦被重定向成管道/文件就改用 CP936，emoji 编不出来
            # 会抛 UnicodeEncodeError —— 这段警告本身就是为了让人看懂，崩在这儿
            # 用户只会看到一条 traceback。
            print("  注意：有个【旧版本】的工具还在跑，新加的功能在它上面用不了。")
            print("     请先关掉那个黑窗口，再双击一次 启动.bat。")
            print(f"     （旧版本正占着 {PREFERRED_PORT} 端口）")
            print()
            # 退出码 1 —— 让 .bat 停下来，用户来得及看清这段警告
            raise SystemExit(1)
        print(f"  工具已经在跑了：{_url(PREFERRED_PORT)}")
        print("  这次就不重复启动了，直接给你打开浏览器。")
        print("  想重启的话，先关掉之前那个黑窗口。")
        print()
        if not _no_browser():
            webbrowser.open(_url(PREFERRED_PORT))
        # 退出码 0 —— 一切正常，.bat 不必停下来等按键
        raise SystemExit(0)

    port = next((p for p in range(PREFERRED_PORT, PREFERRED_PORT + PORT_TRIES)
                 if not _port_busy(p)), None)
    if port is None:
        print()
        print(f"  端口 {PREFERRED_PORT}~{PREFERRED_PORT + PORT_TRIES - 1} 都被占用了，起不来。")
        print("  先关掉占用它们的程序再试。")
        print()
        raise SystemExit(1)

    if port != PREFERRED_PORT:
        print()
        print(f"  注意：{PREFERRED_PORT} 被别的程序占着，改用 {port}。")

    print()
    print("  晶体长度测量工具")
    print(f"  地址：{_url(port)}")
    if _no_browser():
        print("  正在后台运行（开机自启）。想打开页面，点桌面上的「打开测量」。")
    else:
        print("  浏览器会自己打开。关掉这个黑窗口就是关闭工具。")
    print()
    print("  如果 Windows 弹出防火墙提示：只勾「专用网络」，不要勾「公用网络」。")
    print("  手机传图默认关着，在网页右侧点「开启手机传图」才会打开。")
    print()

    if not _no_browser():
        threading.Thread(target=_open_browser_later, args=(port,), daemon=True).start()
    # 端口是我们挑的，app 里算手机地址和二维码要用 —— 在 uvicorn 导入 app 之前告诉它
    os.environ["JLTX_PORT"] = str(port)
    try:
        # proxy_headers=False：本机没有反向代理，关掉 X-Forwarded-For 解析，
        # 免得来源地址能被伪造。
        uvicorn.run("app.main:app", host=LAN_HOST, port=port, proxy_headers=False)
    except KeyboardInterrupt:
        pass
    print("\n  已停止。\n")


if __name__ == "__main__":
    main()
