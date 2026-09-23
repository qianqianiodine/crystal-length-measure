"""文件存储：路径解析、图片校验落盘、安全删除、局域网地址、二维码。

为什么校验用 Pillow 而不是 cv2：
- cv2.imdecode 拿到空 buffer 会**抛 cv2.error**（不是返回 None），最可能的坏输入
  （0 字节、传了一半）反而会变成 500；截断的 JPEG 它还会给半张图当成功。
- 同一张 4000×3000 的 JPEG：cv2.imdecode 约 116 ms / 36 MB，Pillow verify() 约 0.1 ms。

本模块不 import cv2 —— 校验只认字节流，不碰路径，所以不涉及中文路径那个坑。
"""
import hashlib
import ipaddress
import secrets
import shutil
import socket
import subprocess
import time
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app import config

# 解压炸弹护栏：一个几百 KB 的 PNG 能让解码器申请上 GB 内存
Image.MAX_IMAGE_PIXELS = 80_000_000

# 只认这几种。扩展名由**内容**决定，不信客户端给的文件名。
# MPO = 多图 JPEG（手机双摄 / 人像 / 部分 HDR 写出来的就是它）。它是**完全正常的
# JPEG**：扩展名 .jpg、MIME image/jpeg、头 12 字节跟普通 JPEG 一模一样，只有往里
# 探一层才知道装了不止一张图 —— 所以手机显示"jpeg"没显示错，是 Pillow 报的 format
# 更细。cv2 读的正是第一张（主图），所以按 .jpg 收下。
_ALLOWED = {"JPEG": ".jpg", "MPO": ".jpg", "PNG": ".png", "TIFF": ".tif", "BMP": ".bmp"}

# iPhone 默认存 HEIC，Pillow 和 OpenCV 都读不了。单独认出来给一句能照做的提示，
# 否则用户看到的是"这不是一张能识别的图片"，不知道该怎么办。
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}

# Windows 文件名里不能出现的字符。main.py 的下载头过滤也用这份，别各写各的。
BAD_NAME_CHARS = '\\/:*?"<>|'


def _is_heic(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS


def sniff(data: bytes) -> tuple[str, int, int]:
    """看清这是张什么图。返回 (扩展名, 宽, 高)。

    坏输入一律抛 ValueError，消息是可以直接给用户看的中文。
    """
    if not data:
        raise ValueError("这个文件是空的")
    if _is_heic(data):
        raise ValueError(
            "这是 iPhone 的 HEIC 格式，工具暂时读不了。"
            "请把手机「设置 → 相机 → 格式」改成「兼容性最佳」再拍，或先用电脑转成 JPG。"
        )
    try:
        with Image.open(BytesIO(data)) as im:
            fmt, size = im.format, im.size
            im.verify()          # 能在 0.1ms 内查出截断/损坏
    except UnidentifiedImageError:
        raise ValueError("这不是一张能识别的图片（支持 JPG / PNG / TIF / BMP）")
    except Image.DecompressionBombError:
        raise ValueError("这张图片太大了，打不开")
    except OSError as e:
        raise ValueError(f"图片损坏或没有传完：{e}")

    if fmt not in _ALLOWED:
        raise ValueError(f"暂不支持 {fmt} 格式（支持 JPG / PNG / TIF / BMP）")
    return _ALLOWED[fmt], int(size[0]), int(size[1])


def resolve_path(stored: str) -> Path:
    """把库里存的路径还原成实际文件位置。

    两种形状都要认：
      - 绝对路径：老记录。照片留在 样品集/ 原地，不拷贝
      - 相对路径：正式导入的图，相对 IMAGES_DIR
    """
    p = Path(stored)
    return p if p.is_absolute() else config.IMAGES_DIR / p


def _store(data: bytes, dest: Path) -> str:
    """先验后写，返回**相对 dest 的文件名**。坏图片抛 ValueError。"""
    ext, _w, _h = sniff(data)
    dest.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time() * 1000)}_{hashlib.sha1(data).hexdigest()[:12]}{ext}"
    (dest / name).write_bytes(data)
    return name


def store_original(data: bytes) -> str:
    """把图存进 IMAGES_DIR，返回**相对 IMAGES_DIR 的文件名**。

    文件名是 时间戳_内容哈希 的纯 ASCII —— 不用原文件名，避免中文落盘的编码问题，
    顺便让同一张图重复上传时能看出是同一份。

    Raises:
        ValueError: 不是有效图片（消息可直接给用户看）
    """
    config.ensure_dirs()
    return _store(data, config.IMAGES_DIR)


def store_staged(data: bytes) -> str:
    """把图存进待确认暂存区，返回**相对 STAGING_DIR 的文件名**。

    和 store_original 共用 _store：校验规则、命名规则完全一致，
    确认导入时再 store_original 一遍就行，不会多出第二种失败模式。
    """
    config.ensure_dirs()
    return _store(data, config.STAGING_DIR)


def _under(p: Path, root: Path) -> bool:
    """这个路径是不是在 root 底下。判不出来一律当"不是"（宁可不动文件）。"""
    try:
        return p.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _under_data(p: Path) -> bool:
    """在 DATA_DIR 底下吗。trash_file 用这个。"""
    return _under(p, config.DATA_DIR)


def trash_file(p: Path) -> bool:
    """把文件挪进 data/trash/，返回是否真的挪了。

    ⚠️ 只处理 DATA_DIR 底下的文件。老记录指向 样品集/ —— 那是用户的原始资料，
    按项目铁律「样品集仅用于测试、不许改原图」，这里绝不碰。

    ⚠️ 另一个坑：Path("data") / "D:/.../样品集/图.jpg" 会返回**右边那个绝对路径**
    （pathlib 遇到绝对路径右操作数会整个丢弃左边），所以判断必须在 resolve 之后做，
    不能写成 DATA_DIR / stored 再删。
    """
    if not p.is_file() or not _under_data(p):
        return False
    trash = config.DATA_DIR / "trash"
    trash.mkdir(parents=True, exist_ok=True)
    shutil.move(str(p), str(trash / p.name))
    return True


def resolve_staged(stored: str) -> Path:
    return config.STAGING_DIR / stored


def discard_staged(stored: str) -> bool:
    """把暂存文件**真删**（不挪 trash —— 它本来就没入库）。返回是否真删了。

    ⚠️ 只删 STAGING_DIR 底下的，理由同 trash_file：Path("data") / "D:/.../图.jpg"
    会返回右边那个绝对路径，不判断就会删掉用户别处的文件。
    """
    p = resolve_staged(stored)
    if not p.is_file() or not _under(p, config.STAGING_DIR):
        return False
    p.unlink()
    return True


# ipconfig 的网卡段落头里带这些字样 = 虚拟网卡。手机连不上它们 —— 那些"默认路由"是电脑自己走
# VPN / 虚拟交换机用的，跟局域网不是一回事。只降权、不过滤：万一用户手上真只有这
# 一张网卡，至少还留着一个地址可选，不至于列表空掉、二维码都出不来。
_VIRTUAL_HINTS = (
    "vethernet", "wsl", "vpn", "vnic", "atrust", "virtual", "vmware", "vbox",
    "virtualbox", "hyper-v", "tap", "tun", "npcap", "loopback", "docker",
    "tailscale", "zerotier", "hamachi", "radmin", "bluetooth",
)


def _ipconfig_adapters() -> list[tuple[str, bool, bool]]:
    """跑一次 ipconfig，返回 [(IPv4 地址, 有没有默认网关, 名字像不像虚拟网卡)]。

    只认 ipconfig 的**结构**，不认中文还是英文界面：
      - 段头 = 顶格、以冒号结尾的那一行（`无线局域网适配器 WLAN:` / `Ethernet adapter Ethernet:`）
      - 段里 IPv4 行往下数第 2 行就是默认网关行；冒号后面是空的 = 这张网卡没有网关
    段头整行拿去匹配 _VIRTUAL_HINTS，**不剥前缀** —— 本地化的"无线局域网适配器"
    里不含那些词，网卡自己的名字（WLAN / aTrustVNIC / vEthernet (WSL)）也不含。
    （试过取最后一个空格分隔的词当网卡名，撞上 `vEthernet (WSL)` 这种带空格的会剥错。）

    为什么要看网关：**只有真连在局域网上的网卡才会被分配到默认网关**。VPN / WSL /
    VMware 这些虚拟网卡的网关字段是空的 —— 2026-09-23 在用户机器上实测，aTrust 的
    2.0.0.1 和 WSL 的 172.22.48.1 都是空的，只有 WLAN 的 10.200.30.175 有网关。

    拿不到就返回空列表，调用方照原样排 —— 功能不受影响，只是少一层排序依据。
    """
    try:
        raw = subprocess.run(["ipconfig"], capture_output=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    # 全程只看 ASCII（"IPv4"、网卡名、数字），解错码也不影响判断
    lines = raw.decode("gbk", "replace").replace("：", ":").splitlines()

    found: list[tuple[str, bool, bool]] = []
    head = ""
    body: list[str] = []

    def flush() -> None:
        virtual = any(h in head.lower() for h in _VIRTUAL_HINTS)
        for i, ln in enumerate(body):
            if "IPv4" not in ln:
                continue
            ip = ln.rsplit(":", 1)[-1].strip()
            try:
                ipaddress.IPv4Address(ip)
            except ValueError:
                continue
            gw = body[i + 2].rsplit(":", 1)[-1].strip() if i + 2 < len(body) else ""
            found.append((ip, bool(gw), virtual))

    for ln in lines:
        if ln[:1].strip() and ln.rstrip().endswith(":"):
            flush()
            head = ln.strip()
            body = []
        else:
            body.append(ln)
    flush()
    return found


def _rank_ips(ips: list[str], adapters: list[tuple[str, bool, bool]]) -> list[str]:
    """按"手机连得上的可能性"重排地址：有网关 +2，名字不像虚拟网卡 +1。

    sorted 是稳定排序，同分保持传入顺序 —— 所以 adapters 为空（拿不到 ipconfig）时
    结果和传入的一模一样，不会比现在更差。
    """
    known = {ip: (has_gw, virtual) for ip, has_gw, virtual in adapters}

    def score(ip: str) -> int:
        has_gw, virtual = known.get(ip, (False, False))
        return (2 if has_gw else 0) + (0 if virtual else 1)

    return sorted(ips, key=score, reverse=True)


def local_ips() -> list[str]:
    """本机在局域网里可能的地址，**手机最可能连得上的排第一**。

    返回列表而不是单个地址：这台机器常带 Hyper-V / WSL / VMware / VPN 虚拟网卡，
    VPN 一开默认路由就指向它，只报一个地址的话二维码会静默编成手机到不了的网址。

    ⚠️ 排序**不能**信"走默认路由的那张" —— 那个探测恰好会被 VPN 骗到。2026-09-23
    实测：它返回的就是 aTrust VPN 的 2.0.0.1，手机真正连得上的 WLAN
    10.200.30.175 被挤到第二，用户看到的就是"第一个网址打不开、选第二个才行"。
    改看两个硬指标：这张网卡有没有默认网关、名字像不像虚拟网卡。
    """
    out: list[str] = []

    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))      # 不会真的发包，只为查路由走哪张网卡
        out.append(s.getsockname()[0])
    except OSError:
        pass
    finally:
        if s is not None:
            s.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            a = ipaddress.ip_address(ip)
            if a.is_private and not a.is_loopback and not a.is_link_local and ip not in out:
                out.append(ip)
    except OSError:
        pass
    return _rank_ips(out, _ipconfig_adapters())


def qr_png_bytes(url: str) -> bytes:
    """生成二维码 PNG。qrcode 没装的话抛 ImportError，由调用方转成中文提示。"""
    import qrcode

    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    buf = BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    return buf.getvalue()


def mobile_token() -> str:
    """手机传图用的令牌。存盘。

    不存盘的话每次重启都换一个，用户昨天存到手机里的书签今天就失效了。
    """
    p = config.DATA_DIR / "mobile_token.txt"
    try:
        t = p.read_text(encoding="ascii").strip()
        if t:
            return t
    except OSError:
        pass

    t = secrets.token_urlsafe(16)
    config.ensure_dirs()
    try:
        p.write_text(t, encoding="ascii")
    except OSError:
        pass          # 写不了也不致命，本次运行用这个令牌
    return t
