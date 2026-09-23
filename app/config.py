"""全局配置：路径常量与物理常量。

物理常量来自厂商规格（XtalQuest XQ-P-96S-E），不可随意改动：
- 点样孔 Φ3.4mm × H0.6mm
- 孔间距 9.0mm（ANSI/SBS 标准）
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"
IMAGES_DIR = DATA_DIR / "images" / "originals"
CACHE_DIR = DATA_DIR / "images" / "cache"
THUMBS_DIR = DATA_DIR / "thumbs"
# 待确认导入的暂存区。没点「确认导入」之前，照片不进 IMAGES_DIR。
STAGING_DIR = DATA_DIR / "staging"
DB_PATH = DATA_DIR / "app.db"
WEB_DIR = PROJECT_ROOT / "web"
# 图库批量导出的落点。和 data/ 、web/ 并排，用户一眼找得到。
EXPORT_DIR = PROJECT_ROOT / "导出"

# ---- 物理常量（μm）----
# 点样孔直径 Φ3.4mm。**这是出厂默认值** —— 别人手上的板子孔不一样大时，
# 可以在网页右侧「点样孔直径」里改（存在 data/settings.json，见 app/settings.py）。
# 用户改过之后这个常量本身不动，只是被 settings 那边盖住。
WELL_DIAMETER_UM = 3400.0
WELL_PITCH_UM = 9000.0      # 孔间距 9mm

# ---- 支持的图像格式 ----
SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}

# 接口版本。加/改接口就 +1 —— launch.py 靠它认出"端口上跑的是旧版本"。
# 不加这个的话，用户双击 启动.bat 会被老进程的 /api/health 骗过去，
# 页面是新的、接口是旧的，报一堆英文的 "Not Found"。
API_VERSION = 17


def ensure_dirs() -> None:
    """创建运行时数据目录。幂等。

    ⚠️ 一律**在这里现算**，不要在模块级写 `STAGING_THUMBS_DIR = STAGING_DIR / "thumbs"`
    这种派生常量：模块级常量 import 时就求值了，测试 monkeypatch 了 STAGING_DIR
    也带不动它，会往用户真实的 data/ 里写东西。
    """
    for d in (DATA_DIR, IMAGES_DIR, CACHE_DIR, THUMBS_DIR, STAGING_DIR,
              STAGING_DIR / "thumbs", EXPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
