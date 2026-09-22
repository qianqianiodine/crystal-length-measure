"""用户可改的设置。

出厂默认写在 `config.py`（那是厂商规格，是源码里的常量）；这里存的只是
**用户改过的值**，落盘到 `data/settings.json`。删掉那个文件 = 恢复出厂。

为什么用一个文件而不是数据库表：`data/` 本来就是用户产物、已经在 .gitignore 里，
而这个值在**建库之前**就可能要用到，塞进 schema 反而要多写一次迁移。

⚠️ 路径一律**现算**（写成 `_path()` 而不是模块级常量）：测试会 monkeypatch
`config.DATA_DIR`，模块级常量 import 那一刻就定型了、带不动，会去读写用户真实的
`data/`。这条和 `config.ensure_dirs()` 里那句警告是同一个坑。
"""
import json

from app import config


def _path():
    return config.DATA_DIR / "settings.json"


def well_diameter_um() -> float:
    """点样孔直径（μm）。

    读不出来（文件不在 / 内容坏了 / 值不合理）一律回**出厂默认** ——
    一个设置文件坏掉不该让整个工具没法测量。
    """
    try:
        v = float(json.loads(_path().read_text(encoding="utf-8"))
                  ["well_diameter_um"])
    except (OSError, ValueError, TypeError, KeyError):
        # OSError=文件不在/读不了 · ValueError=JSON 坏了 · TypeError=顶层不是对象
        # · KeyError=缺这个键 · 值不是数字也是 ValueError
        return config.WELL_DIAMETER_UM
    return v if v > 0 else config.WELL_DIAMETER_UM


def set_well_diameter_um(v: float) -> float:
    """改孔直径，落盘，返回存进去的值。

    ⚠️ 只写设置，**不回头改任何已有照片**。每张照片的换算系数
    （calibrations.um_per_px）在它定圆的那一刻就单独存下来了，所以改这里
    只影响"以后新定圆的照片"。想更新某张老照片，把那张图的圆重新拖一下就
    会按新直径重存、并自动重算它所有的线。
    """
    v = float(v)
    if not (v > 0):
        raise ValueError("孔直径必须大于 0")

    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再改名：避免半个文件留在那儿（下次读就是坏 JSON）。
    tmp = p.with_name(p.name + ".new")
    tmp.write_text(json.dumps({"well_diameter_um": v}, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(p)
    return v
