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
# 17 → 18（2026-09-23）：接口形状没变，但手机地址的**排序**变了。老进程还开着的话
#   用户会看到跟修之前一模一样的第一个网址，以为白修了 —— 靠这一位让 launch.py
#   把"先关掉那个黑窗口再开"喊出来。
# 18 → 19（2026-10-06）：接口形状还是没变，但改名不再"改成自己现在的名字也加 (2)"了。
#   页面是现读磁盘的（改完不用重启就是新的），接口在内存里是旧的 —— 正好是最坑的
#   那种搭配：新页面每次点确定都重发预填的名字，老接口会每点一次加一个 " (2)"。
#   同一天还改了手机页（孔位默认孔1、新增「传送记录」）—— 那一页整个存在服务端
#   内存里（`mobile._PAGE`），不重启根本不会变，用户在手机上会以为没改。
# 19 → 20（2026-10-06）：单张上传（direct）以前把 `folder` 丢了，照片落进「未分类」；
#   现在两条路都放。页面本来就在传 folder，所以**不 +1 的话一句提示都不会有** ——
#   新页面对着老接口，用户只会看到"我明明在图库里选着文件夹，照片还是没归进去"。
# 20 → 21（2026-10-17）：新增 PUT /api/settings/style（测量页把字号/线宽/画布尺寸
#   告诉服务端，图库缩略图和批量导出按它画）。老进程没有这个接口：页面会静默 404，
#   图库继续用写死的旧粗细（线 3、字 13）—— 正是用户报的"没同步"，
#   看起来就像这次没修。
# 21 → 22（2026-10-22）：手机上传接口新增 name/tag/uid（离线队列：照片先存手机、
#   有网自动补传、uid 防重复）。手机页整个存在服务端内存里（`mobile._PAGE`）——
#   老进程发的是新页面、打的是老接口，`name/tag/uid` 会被静默忽略：照片能进库，
#   但**名字全变成手机文件名、孔位也丢了**，用户只会看到"我填了半天白填"。
# 22 → 23（2026-10-22）：后台预识别孔（`images.rough_circle` 起点圆缓存）。
#   没有新增 HTTP 接口，但老进程没有后台线程 —— 不 +1 的话 启动.bat 会以为
#   "工具已经在跑了"，用户永远等不到"打开文件夹里的图变快"。
# 23 → 24（2026-10-23）：孔的起点圆改成**视觉模型优先**（ONNX，约 10ms），
#   模型文件缺失 / onnxruntime 不可用 / 输出离谱时才回退旧 Hough（约 5s）。
#   没有新增 HTTP 接口，但老进程没有 `app/detect_dl.py` 和模型文件，
#   开图仍然要等 5 秒 —— 必须重启才能用上新模型。
# 24 → 25（2026-10-23）：模型推理补两遍"围着预测裁切"（完整孔用户反馈不准）。
#   接口没变，但老进程还是旧的推理逻辑，开图拿到的起点圆偏得厉害 —— +1 让
#   launch.py 把"先关掉那个黑窗口再开"喊出来。
API_VERSION = 25


def ensure_dirs() -> None:
    """创建运行时数据目录。幂等。

    ⚠️ 一律**在这里现算**，不要在模块级写 `STAGING_THUMBS_DIR = STAGING_DIR / "thumbs"`
    这种派生常量：模块级常量 import 时就求值了，测试 monkeypatch 了 STAGING_DIR
    也带不动它，会往用户真实的 data/ 里写东西。
    """
    for d in (DATA_DIR, IMAGES_DIR, CACHE_DIR, THUMBS_DIR, STAGING_DIR,
              STAGING_DIR / "thumbs", EXPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
