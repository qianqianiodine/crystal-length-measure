"""手机扫码传图。

安全模型（配合 app/main.py 里的 lan_guard 中间件）：
- 服务绑 0.0.0.0，但默认**关着** —— 局域网里什么都进不来，包括这里。
- 电脑上点「开启手机传图」才开一个 2 小时的口子。
- 开口子期间也只放行 `/m/<令牌>` 这一个前缀，**任何别的路径一律 403**。
- 手机上能做的只有"传"：这个文件里没有任何列出/查看/删除已有照片的路由。
- 令牌存在 data/mobile_token.txt，重启不换 —— 否则手机书签每天都失效。
"""
import time

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     Request, Response, UploadFile)
from fastapi.responses import HTMLResponse

from app import storage
from app.api.images import MAX_FILES, _TAG_RE, _ingest
from app.db import get_db

router = APIRouter(tags=["mobile"])

# 开一次口子管多久。够拍完一整块板、传完一批照片，又不用记得关。
# 2026-09-23 用户要求从 30 分钟改成 2 小时 —— 30 分钟不够他拍完一块 96 孔板。
# ⚠️ 这个不是安全边界，是省事用的：开口子期间局域网里任何人都能传，只是传进来的
#    照片一律进「待确认导入」列表，要在这台电脑上点「确认导入」才真的入库。
MOBILE_WINDOW_SEC = 2 * 60 * 60


def _urls(request: Request, folder: int | None = None) -> list[str]:
    """手机该访问哪些地址。可能多个 —— 这台机器有虚拟网卡和 VPN。

    folder 是"你在图库点手机传图时正看的那个文件夹"，拼进 URL 的查询串带过去。
    手机上那个「这批照片放到」下拉就默认停在那里。
    ⚠️ 查询串不参与 lan_guard 的路径比对（它比的是 request.url.path），
    所以多带一个参数不影响安全模型。
    """
    # 正常情况下 Host 头里有端口（浏览器访问的是 127.0.0.1:8510）。
    # 没有就退回启动时记下的端口 —— TestClient 就是这种情况。
    port = request.url.port or getattr(request.app.state, "port", None)
    if not port:
        return []
    path = f"/m/{request.app.state.mobile_token}"
    if folder is not None and folder > 0:
        path += f"?folder={folder}"
    return [f"http://{ip}:{port}{path}" for ip in storage.local_ips()]


def _state(request: Request, folder: int | None = None) -> dict:
    st = request.app.state
    left = max(0, int(st.mobile_off_at - time.time())) if st.mobile_on else 0
    return {"urls": _urls(request, folder), "on": left > 0, "expires_in": left}


@router.get("/api/net/info")
def net_info(request: Request, folder: int | None = Query(None, ge=0)) -> dict:
    return _state(request, folder)


@router.post("/api/net/enable")
def net_enable(request: Request) -> dict:
    """开 2 小时。只能从本机调 —— 中间件保证局域网调不到这个路径。

    不收 folder：页面压根不用它的返回值（地址是紧接着从 /api/net/info 拿的）。
    """
    request.app.state.mobile_on = True
    request.app.state.mobile_off_at = time.time() + MOBILE_WINDOW_SEC
    return _state(request)


@router.get("/api/net/qrcode")
def net_qrcode(request: Request, i: int = 0,
               folder: int | None = Query(None, ge=0)) -> Response:
    urls = _urls(request, folder)
    if not urls:
        raise HTTPException(503, "没找到局域网地址 —— 这台电脑可能没连网络")
    try:
        png = storage.qr_png_bytes(urls[i] if 0 <= i < len(urls) else urls[0])
    except ImportError as e:
        raise HTTPException(503, "缺少 qrcode 库，请运行：pip install qrcode[pil]") from e
    return Response(content=png, media_type="image/png")


# ---------------- 手机页面 ----------------

_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>传照片到电脑</title>
<style>
  :root{--ink:#17150f;--panel:#1f1c16;--raise:#2a261d;--rule:#37322a;
        --text:#ede8de;--mute:#9a9284;--ref:#e0a63c;
        /* 下面待确认列表那几块用 var(--ui) 取字体，跟 body 上写的是同一套 */
        --ui:"Microsoft YaHei UI","Microsoft YaHei",sans-serif}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;background:var(--ink);color:var(--text);
       font:16px/1.6 "Microsoft YaHei UI","Microsoft YaHei",sans-serif;
       display:flex;justify-content:center;padding:28px 20px 48px}
  main{width:100%;max-width:460px}
  h1{margin:0 0 6px;font-size:21px;font-weight:600;letter-spacing:.02em}
  .sub{color:var(--mute);font-size:14px;margin:0 0 26px}
  .pick{display:block;text-align:center;background:var(--ref);color:#241c08;
        font-weight:600;font-size:17px;padding:17px;border-radius:4px;cursor:pointer}
  .pick:active{background:#efb452}
  .pick.busy{background:var(--raise);color:var(--mute);pointer-events:none}
  ul{list-style:none;margin:24px 0 0;padding:0}
  li{background:var(--panel);border:1px solid var(--rule);border-radius:3px;
     padding:11px 13px;margin-bottom:8px;font-size:14px;
     display:flex;gap:10px;align-items:baseline}
  li .n{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  li .s{flex:0 0 auto;color:var(--mute);font:13px Consolas,monospace}
  li.ok .s{color:var(--ref)}
  li.bad .s{color:#e8746a}
  li.bad .why{display:block;color:#e8746a;font-size:12.5px;margin-top:4px;
              white-space:normal}
  .tail{color:var(--mute);font-size:13.5px;margin-top:26px;text-align:center}
  #pend[hidden] { display: none; }
  #pend { margin-top: 26px; border-top: 1px solid var(--rule); padding-top: 18px; }
  #pend h2 { font-size: 15px; font-weight: 600; margin: 0 0 12px; color: var(--ref); }
  /* 传送记录。和「待确认导入」同一个样子 —— 两块是上下叠着的，长得不一样会更乱。
     ⚠️ 那条 [hidden] 必须有：下面给了 display 的话，hidden 就压不住它了
     （图库页的 #empty 踩过同一个坑）。 */
  /* ⚠️ 这一块**永远显示**（不像「待确认导入」是空的就藏起来）：一是让用户知道
     记录会出现在哪儿，二是"手机上到底更新了没有"靠它一眼就能看出来 ——
     2026-10-06 用户报过"重启了也没更新"，其实只是这一块当时是藏着的。 */
  #sent { margin-top: 26px; border-top: 1px solid var(--rule); padding-top: 18px; }
  #sent h2 { font-size: 15px; font-weight: 600; margin: 0 0 12px; color: var(--ref); }
  #sent ul { margin-top: 0; }
  #sent .none { color: var(--mute); font-size: 13.5px; margin: 0; }
  .pfxrow { display: block; color: var(--mute); font-size: 13.5px; margin-bottom: 14px; }
  /* 整批那个「这批照片放到」。不写样式的话它是个系统白色的原生下拉，
     在深色页面上像贴上去的一块 —— 用户说这页"有点混乱"就有它一份。 */
  .pfxrow select { display: block; width: 100%; margin-top: 6px; font: 15px var(--ui);
              color: var(--text); background: var(--ink);
              border: 1px solid var(--rule); border-radius: 3px; padding: 10px 9px; }
  .prow { background: var(--panel); border: 1px solid var(--rule); border-radius: 3px;
          padding: 10px 11px; margin-bottom: 8px; }
  .prow.bad { border-color: #5a3630; }
  .prow .ptop { display: flex; gap: 10px; align-items: center; }
  /* contain 而不是 cover：缩略图是拿来认照片的，裁掉一半就没法认了 */
  .prow img { flex: 0 0 54px; width: 54px; height: 54px; object-fit: contain;
              border-radius: 2px; background: var(--raise); }
  .prow input {
    flex: 1 1 auto; min-width: 0; font: 16px var(--ui); color: var(--text);
    background: var(--ink); border: 1px solid var(--rule); border-radius: 3px;
    padding: 9px 10px;
  }
  /* 内边距是给手指留的：这个 ✕ 按下去就把这一行和它的暂存文件真删掉，
     没有撤销、也没有二次确认，误触的代价是重新拍照重传。手机是这个页面的主设备。 */
  .prow .rm { flex: 0 0 auto; background: none; border: 0; color: var(--mute);
              font-size: 19px; padding: 10px 12px; }
  /* 每行第二行：左边填编号、右边三个孔位下拉。
     ⚠️ 手机屏窄，孔位那三个是 `flex: 0 0 auto`（不让缩，缩了「孔1」就剩半个字），
     让编号框去吸收剩下的宽度 —— 反过来整行会横着滚。 */
  .prow .pbot { display: flex; gap: 6px; align-items: center; margin-top: 7px; }
  .prow .pno { flex: 1 1 auto; min-width: 0; font: 14px var(--ui);
              color: var(--text); background: var(--ink);
              border: 1px solid var(--rule); border-radius: 3px; padding: 8px 9px; }
  .prow .pbot select.tg { flex: 0 0 auto; width: auto; font: 14px var(--ui);
              color: var(--text); background: var(--ink);
              border: 1px solid var(--rule); border-radius: 3px; padding: 8px 4px; }
  .prow .why { margin: 7px 0 0; color: #e8746a; font-size: 13px; }
  .prow .pErr { margin: 7px 0 0; color: #e8746a; font-size: 13px; }
  .prow .note { margin: 7px 0 0; color: var(--ref); font-size: 13px; }
  .pfoot { display: flex; gap: 10px; margin-top: 14px; }
  .pfoot button { flex: 1 1 0; font: 600 16px var(--ui); padding: 14px 0;
                  border-radius: 4px; border: 1px solid var(--rule);
                  background: var(--raise); color: var(--text); }
  .pfoot #pok { background: var(--ref); color: #241c08; border-color: var(--ref); }
  /* 待上传（本机队列）。和「待确认导入」长得一样，都是为了少一套视觉语言。 */
  #qsec { margin-top: 26px; border-top: 1px solid var(--rule); padding-top: 18px; }
  #qsec h2 { font-size: 15px; font-weight: 600; margin: 0 0 12px; color: var(--ref); }
  .qrow { background: var(--panel); border: 1px solid var(--rule); border-radius: 3px;
          padding: 10px 11px; margin-bottom: 8px; }
  .qrow.bad { border-color: #5a3630; }
  .qtop { display: flex; gap: 10px; align-items: center; }
  .qtop img { flex: 0 0 54px; width: 54px; height: 54px; object-fit: contain;
              border-radius: 2px; background: var(--raise); }
  .qtop input { flex: 1 1 auto; min-width: 0; font: 16px var(--ui); color: var(--text);
                background: var(--ink); border: 1px solid var(--rule);
                border-radius: 3px; padding: 9px 10px; }
  .qtop .rm { flex: 0 0 auto; background: none; border: 0; color: var(--mute);
              font-size: 19px; padding: 10px 12px; }
  .qbot { display: flex; gap: 6px; align-items: center; margin-top: 7px; }
  .qno { flex: 1 1 auto; min-width: 0; font: 14px var(--ui); color: var(--text);
         background: var(--ink); border: 1px solid var(--rule); border-radius: 3px;
         padding: 8px 9px; }
  .qbot select.tg { flex: 0 0 auto; width: auto; font: 14px var(--ui); color: var(--text);
                    background: var(--ink); border: 1px solid var(--rule);
                    border-radius: 3px; padding: 8px 4px; }
  .qst { margin: 7px 0 0; color: var(--mute); font-size: 13px; }
  .qrow.bad .qst { color: #e8746a; }
  #qall { display: block; width: 100%; margin-top: 4px; font: 600 15px var(--ui);
          padding: 12px 0; border-radius: 4px; border: 1px solid var(--rule);
          background: var(--raise); color: var(--text); }
  .hint { color: var(--mute); font-size: 12.5px; margin-top: 20px; line-height: 1.7; }
  /* 看大图。⚠️ [hidden] 那条不能省：下面给了 display:flex，UA 自带的
     [hidden]{display:none} 压不住它，关不掉（#pend 踩过同一个坑）。 */
  .pv { position: fixed; top: 0; left: 0; right: 0; bottom: 0; z-index: 100;
        background: rgba(0, 0, 0, .93); display: flex; align-items: center;
        justify-content: center; padding: 18px; }
  .pv[hidden] { display: none; }
  .pv img { max-width: 100%; max-height: 100%; object-fit: contain; }
  .pv #pvClose { position: absolute; top: 10px; right: 8px; background: none;
                 border: 0; color: #fff; font-size: 26px; padding: 12px 16px; }
</style>
</head>
<body>
<main>
  <h1>传照片到电脑</h1>
  <p class="sub">选好照片先存在手机里，填好编号和孔位；回到有网的地方，这个页面会自动一张张传到电脑。</p>

  <label class="pick" id="btn">拍照 / 选照片
    <input type="file" accept="image/*" multiple hidden id="f">
  </label>

  <section id="qsec" hidden>
    <h2>待上传（<span id="qn">0</span> 张，约 <span id="qsize">0</span> MB）</h2>
    <div id="qrows"></div>
    <button id="qall" type="button">现在全部传</button>
  </section>

  <section id="pend" hidden>
    <h2>待确认导入（<span id="pn">0</span> 张）</h2>
    <label class="pfxrow">这批照片放到
      <select id="pfolder"></select>
    </label>
    <div id="prows"></div>
    <div class="pfoot">
      <button id="pok" type="button">确认导入</button>
      <button id="pno" type="button">取消</button>
    </div>
  </section>

  <section id="sent">
    <h2>传送记录（<span id="sn">0</span> 张）</h2>
    <p class="none" id="sempty">还没传过 —— 传过的照片会一条条记在这儿，带上你起的名字。</p>
    <ul id="list"></ul>
  </section>
  <p class="tail" id="tail"></p>
  <p class="hint">照片传上去之前只存在这个浏览器里 —— 别清 Safari 数据；传图窗口每 2 小时要重新开一次。<br>
    回到有网的地方，把这个页面放在前台，它会自动传。</p>
</main>

<div class="pv" id="pv" hidden>
  <img id="pvImg" alt="">
  <button type="button" id="pvClose" aria-label="关闭">✕</button>
</div>

<script>
const token = location.pathname.split('/')[2] || '';
const input = document.getElementById('f');
const list = document.getElementById('list');
const tail = document.getElementById('tail');
const sn = document.getElementById('sn');
const sempty = document.getElementById('sempty');

// ---------- 传送记录 ----------
// 这次打开页面（同一个浏览器标签）以来传过的**全部**照片，一条一条往下摞。
// 用户 2026-10-06 要的：批量、拍照、混着来都记在同一份里；每条写它的名字 ——
// 下面填了「编号」+ 孔位，上面这条就变成「编号-行-列-孔」。
// ⚠️ 上一版每传一批就 `list.innerHTML = ''`，传完 3 张再拍一张，前 3 条就没了。
// ⚠️ 存 sessionStorage，不是只放内存：手机锁屏常常让页面自己重载一下，
//    不存的话记录会**悄悄清零**，用户还以为自己没传上去。关掉标签页、或者重新
//    扫码（新开的标签）就重新开始 —— 用户要的"随打开清空"。
// ⚠️ 「确认导入」**不**清空它（用户指定）：记录是"我传过什么"，
//    不是"还差什么没确认"—— 后者是下面那个待确认列表的活。
const SENT_KEY = 'jltx.sent';
let SENT = [];
try {
  SENT = JSON.parse(sessionStorage.getItem(SENT_KEY) || '[]');
  if (!Array.isArray(SENT)) SENT = [];
} catch (e) {
  SENT = [];        // 存坏了、或者浏览器禁了 sessionStorage —— 当没有，不能白屏
}

function saveSent() {
  try { sessionStorage.setItem(SENT_KEY, JSON.stringify(SENT)); } catch (e) {}
}

function paintSent() {
  list.innerHTML = '';
  for (const r of SENT) {
    const li = document.createElement('li');
    if (r.cls) li.classList.add(r.cls);
    const n = document.createElement('span');
    n.className = 'n';
    n.textContent = r.name;        // textContent：名字是用户打的，别走 innerHTML
    const s = document.createElement('span');
    s.className = 's';
    s.textContent = r.st;
    li.append(n, s);
    if (r.why) {
      const w = document.createElement('span');
      w.className = 'why';
      w.textContent = r.why;
      li.append(w);
    }
    list.append(li);
  }
  sempty.hidden = !!SENT.length;      // 有记录了就把"还没传过"那句收起来
  sn.textContent = String(SENT.length);
}

// 名字跟着「待确认列表」走：下面填了编号 + 孔位 → 上面这条跟着变成那个名字。
// 挂在 paintPend() 里 —— 那一页所有重画最后都会走到它（放 edit/patch 各处会漏）。
function syncSentNames() {
  const byId = new Map((P.items || []).map(it => [it.id, it]));
  let changed = false;
  for (const r of SENT) {
    const it = r.pid ? byId.get(r.pid) : null;
    if (it && it.name && it.name !== r.name) { r.name = it.name; changed = true; }
  }
  if (changed) saveSent();
  paintSent();
}

// 这一行离开待确认列表了，记录里那一条换个状态（入库 / 移除 / 取消）。
function markSent(ids, st, cls) {
  const set = new Set(ids);
  for (const r of SENT) {
    if (r.pid !== null && set.has(r.pid)) { r.st = st; r.cls = cls; }
  }
  saveSent();
  paintSent();
}

// ---------- 本机待上传队列（断网也不丢） ----------
// 照片先写进手机浏览器的 IndexedDB，再慢慢往电脑传。晶体房信号差的时候
// fetch 会失败 —— 失败**不删**这一条，回到有网的地方自动接着传。
// ⚠️ 存的是原图 File，不压缩：测量靠原图。
const QDB_NAME = 'jltx_offline';
const QSTORE = 'queue';
const GRACE_MS = 30000;          // 停手 30 秒才自动传，免得名字填一半就被抢走
const RETRY_MS = 15000;          // 有网时每 15 秒试一次
const TIMEOUT_MS = 60000;        // 单张 60 秒没传完就当网络失败
const FORBID_PAUSE_MS = 60000;   // 「手机传图」关了以后，别每 15 秒砸一次

const qsec = document.getElementById('qsec');
const qrows = document.getElementById('qrows');
const qn = document.getElementById('qn');
const qsize = document.getElementById('qsize');
let Q = [];
let syncing = false;
let pausedUntil = 0;
let QDB = null;
const qSaveTimers = new Map();

function onLine() {
  return typeof navigator === 'undefined' || navigator.onLine !== false;
}

function newUid() {
  try {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
  } catch (e) { /* 老浏览器没有 randomUUID，走下面的回退 */ }
  return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2)
         + Math.random().toString(36).slice(2);
}

// 文件名 → 默认名字。和电脑端 _name_from_file 同一个规则：去目录、去扩展名。
// 手机文件名不会有反斜杠，所以这里不处理 Windows 路径。
function defaultName(filename) {
  const base = String(filename || '').split('/').pop();
  const dot = base.lastIndexOf('.');
  return (dot > 0 ? base.slice(0, dot) : base) || '手机照片';
}

function qOpen() {
  if (QDB) return Promise.resolve(QDB);
  return new Promise((resolve, reject) => {
    let req;
    try { req = indexedDB.open(QDB_NAME, 1); }
    catch (e) { reject(e); return; }
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(QSTORE)) {
        db.createObjectStore(QSTORE, { keyPath: 'uid' });
      }
    };
    req.onsuccess = () => { QDB = req.result; resolve(QDB); };
    req.onerror = () => reject(req.error || new Error('手机存储打不开'));
  });
}

function qAll() {
  return qOpen().then(db => new Promise((resolve, reject) => {
    const req = db.transaction(QSTORE, 'readonly').objectStore(QSTORE).getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error || new Error('读不出手机里的照片'));
  }));
}

function qPut(it) {
  return qOpen().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(QSTORE, 'readwrite');
    tx.objectStore(QSTORE).put(it);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error || new Error('存不进手机'));
    tx.onabort = () => reject(tx.error || new Error('存不进手机'));
  }));
}

function qDel(key) {
  return qOpen().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(QSTORE, 'readwrite');
    tx.objectStore(QSTORE).delete(key);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error || new Error('手机存储出错'));
  }));
}

// 只把要留的字段写进去。_url 是缩略图的临时地址，重开页面就失效，
// 没必要存（也不该存）。status=sending 写回 wait：页面被锁屏重载后
// 不能一直卡在"正在传"。
function qSave(it) {
  return qPut({
    uid: it.uid, blob: it.blob, filename: it.filename,
    name: it.name || '', no: it.no || '', tag: it.tag || '',
    folderId: it.folderId === undefined ? null : it.folderId,
    createdAt: it.createdAt,
    touchedAt: it.touchedAt || it.createdAt,
    status: it.status === 'sending' ? 'wait' : (it.status || 'wait'),
    error: it.error || '',
  });
}

function thumbOf(it) {
  if (typeof it._url === 'string') return it._url;
  try { it._url = URL.createObjectURL(it.blob); }
  catch (e) { it._url = ''; }
  return it._url;
}

// 看大图：队列里的缩略图只有 54px，认不出晶体。点一下铺满全屏看原图。
// 用的是手机上那张 Blob 的临时地址 —— 断网也能看，不经过电脑。
function openPreview(src) {
  if (!src) return;
  document.getElementById('pvImg').src = src;
  document.getElementById('pv').hidden = false;
}

// 这一张要被删掉/传走了：如果大图正开着它，先关掉再回收地址，
// 否则屏幕上会剩一块黑底（图被回收了）。
function closePreviewIf(src) {
  const pv = document.getElementById('pv');
  if (!pv.hidden && document.getElementById('pvImg').src === src) {
    pv.hidden = true;
    document.getElementById('pvImg').src = '';
  }
}

document.getElementById('pv').addEventListener('click', () => {
  document.getElementById('pv').hidden = true;
  document.getElementById('pvImg').src = '';
});

function paintQueue() {
  qsec.hidden = !Q.length;
  qn.textContent = String(Q.length);
  let total = 0;
  for (const it of Q) total += (it.blob && it.blob.size) || 0;
  qsize.textContent = (total / 1048576).toFixed(1);
  qrows.innerHTML = '';
  Q.sort((a, b) => (a.createdAt || 0) - (b.createdAt || 0));
  for (const it of Q) qrows.append(queueRow(it));
}

// 一个字都没改，只是用户碰了这一行：更新"停手"计时，并防抖写回手机。
// ⚠️ 这里**不能** paintQueue()：一重画输入框就失焦，手机键盘会弹回去。
function touch(it, row) {
  it.touchedAt = Date.now();
  if (it.status === 'error') {
    it.status = 'wait';
    it.error = '';
    if (row) {
      row.classList.remove('bad');
      const st = row.querySelector('.qst');
      if (st) st.textContent = '等有网自动传';
    }
  }
  clearTimeout(qSaveTimers.get(it.uid));
  qSaveTimers.set(it.uid, setTimeout(() => {
    qSaveTimers.delete(it.uid);
    qSave(it).catch(() => {});
  }, 350));
}

function queueRow(it) {
  const row = document.createElement('div');
  row.className = 'qrow' + (it.status === 'error' ? ' bad' : '');

  const top = document.createElement('div');
  top.className = 'qtop';
  const img = document.createElement('img');
  img.src = thumbOf(it);
  img.alt = '';
  img.title = '点一下看大图';
  // 缩略图背后是几 MB 的原图：不让浏览器一口气把所有行都解码，
  // 96 张全解开会把 iPhone 的内存吃爆。
  img.loading = 'lazy';
  img.decoding = 'async';
  img.addEventListener('click', () => openPreview(img.src));
  top.append(img);

  const inp = document.createElement('input');
  inp.type = 'text';
  inp.value = it.name || '';
  inp.placeholder = '孔位（如 A1-1）或名字，留空用手机文件名';
  inp.addEventListener('input', () => {
    it.name = inp.value;
    touch(it, row);
  });
  top.append(inp);

  const rm = document.createElement('button');
  rm.className = 'rm';
  rm.type = 'button';
  rm.textContent = '✕';
  rm.title = '从手机里删掉这一张';
  rm.addEventListener('click', async () => {
    // 正在传的时候不让删：上传请求已经发出去了，就算本地删掉，电脑那边
    // 也可能已经入库 —— 记录里会凭空多一张"我明明删了"的照片。
    if (it.status === 'sending') {
      tail.textContent = '这张正在传，等它传完再删';
      return;
    }
    // 这张可能只存在手机里（网页拍的照片不进相册）—— 删之前必须问一句。
    if (!confirm('这张还没传上去，删掉就找不回来了。确定吗？')) return;
    clearTimeout(qSaveTimers.get(it.uid));
    qSaveTimers.delete(it.uid);
    try { await qDel(it.uid); } catch (e) { /* 手机存储坏了，下面照样从界面拿掉 */ }
    const i = Q.indexOf(it);
    if (i >= 0) Q.splice(i, 1);
    closePreviewIf(it._url);
    if (it._url) URL.revokeObjectURL(it._url);
    paintQueue();
  });
  top.append(rm);

  const bot = document.createElement('div');
  bot.className = 'qbot';
  const no = document.createElement('input');
  no.type = 'text';
  no.className = 'qno';
  no.placeholder = '编号';
  no.title = '晶体板编号 —— 填了这张就改名成「编号-行-列-孔」';
  no.maxLength = 60;
  no.autocomplete = 'off';
  no.spellcheck = false;
  no.value = it.no || '';

  const m = /^([A-H])([1-9]|1[0-2])-([12])$/.exec(it.tag || '');
  const selA = mkSel(ROWS, m ? m[1] : '');
  const selN = mkSel(COLS, m ? m[2] : '');
  const selH = mkSel(HOLES, m ? (m[3] === '1' ? '孔1' : '孔2') : HOLES[0], false);

  const sync = () => {
    it.no = no.value;
    it.tag = tagOf(selA, selN, selH);
    // 编号留空 = 不动名字（可能用户在上面直接打了自由名字）
    const nm = nameWithPlate(no.value.trim(), it.tag);
    if (nm) { it.name = nm; inp.value = nm; }
    touch(it, row);
  };
  for (const s of [selA, selN, selH]) s.addEventListener('change', sync);
  let nt = null;
  no.addEventListener('input', () => {
    clearTimeout(nt);
    nt = setTimeout(sync, 400);
  });
  bot.append(no, selA, selN, selH);

  const st = document.createElement('p');
  st.className = 'qst';
  if (it.status === 'sending') st.textContent = '正在传…';
  else if (it.status === 'error') st.textContent = '没传上去：' + (it.error || '原因不明');
  else if (it.error) st.textContent = it.error;
  else if (!onLine()) st.textContent = '现在没网，等有网自动传';
  else st.textContent = '等有网自动传';

  row.append(top, bot, st);
  return row;
}

async function qLoad() {
  try {
    const rows = await qAll();
    Q = [];
    let lost = 0;
    for (const r of rows) {
      if (!r || !r.uid || !r.blob) { lost++; continue; }
      if (r.status === 'sending') r.status = 'wait';
      r.touchedAt = r.touchedAt || r.createdAt || Date.now();
      Q.push(r);
    }
    paintQueue();
    if (Q.length) {
      tail.textContent = `手机里还有 ${Q.length} 张没传，正在等网络`
        + (lost ? `；另有 ${lost} 张读不出来了（手机存储可能被清理过）` : '');
      syncQueue(false);
    } else if (lost) {
      // 浏览器在存储紧张时可能清掉某些记录。不能装作没发生。
      tail.textContent = `有 ${lost} 张读不出来了（手机存储可能被清理过）`;
    }
  } catch (e) {
    tail.textContent = '这个浏览器打不开本地存储，离线拍照用不了 —— 请用 Safari 打开';
  }
}

// 传一张。返回 {ok, name} / {stop, why} / {fatal, why} / {why}。
// 「stop」= 网络断了或窗口关了，这一轮别再试后面的；「fatal」= 这一张本身不行。
function uploadOne(it) {
  return new Promise(resolve => {
    const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    const timer = ctl ? setTimeout(() => ctl.abort(), TIMEOUT_MS) : null;
    const fd = new FormData();
    fd.append('files', it.blob, it.filename);
    if (it.name) fd.append('name', it.name);
    if (it.tag) fd.append('tag', it.tag);
    fd.append('uid', it.uid);
    const q = new URLSearchParams({ mode: 'direct' });
    if (it.folderId) q.set('folder', String(it.folderId));

    fetch(`/m/${token}/upload?${q}`, {
      method: 'POST', body: fd, signal: ctl ? ctl.signal : undefined,
    }).then(async r => {
      let d = null;
      try { d = await r.json(); } catch (e) { /* 服务端没给 JSON，用下面的兜底文案 */ }
      if (r.ok && d && d.ok && d.ok.length) {
        resolve({ ok: true, name: d.ok[0].name || it.name || defaultName(it.filename) });
      } else if (r.ok && d && d.failed && d.failed.length) {
        resolve({ fatal: true, why: d.failed[0].reason || '这张传不上去' });
      } else if (r.status === 403) {
        pausedUntil = Date.now() + FORBID_PAUSE_MS;
        resolve({ stop: true, why: '电脑上的「手机传图」关了（每 2 小时要重新开一次），回去点一下就会接着传' });
      } else if (r.status === 400 || r.status === 422) {
        resolve({ fatal: true, why: (d && d.detail) || '电脑说这张不行' });
      } else {
        resolve({ why: '电脑那边出了点问题，等会儿自动重试' });
      }
    }).catch(e => {
      const why = e && e.name === 'AbortError'
        ? '传得太慢，先留着，等网络好点再传'
        : '连不上电脑，等有网会自动重试';
      resolve({ stop: true, why });
    }).finally(() => {
      if (timer) clearTimeout(timer);
    });
  });
}

async function syncQueue(force) {
  if (syncing || !Q.length) return;
  if (Date.now() < pausedUntil) return;
  if (!onLine()) { paintQueue(); return; }
  const now = Date.now();
  const todo = Q.filter(it => force
    || now - (it.touchedAt || it.createdAt || 0) >= GRACE_MS);
  if (!todo.length) return;

  syncing = true;
  try {
    for (const it of todo) {
      if (Q.indexOf(it) < 0) continue;      // 这一轮里被用户 ✕ 删掉了
      it.status = 'sending';
      it.error = '';
      paintQueue();
      const res = await uploadOne(it);

      if (res.ok) {
        try { await qDel(it.uid); } catch (e) { /* 传都传成了，删不掉就下次再删 */ }
        const i = Q.indexOf(it);
        if (i >= 0) Q.splice(i, 1);
        closePreviewIf(it._url);
        if (it._url) URL.revokeObjectURL(it._url);
        // 用服务端返回的最终名字（可能带 " (2)"），传送记录才和电脑里一致。
        SENT.push({ pid: null, name: res.name, st: '已入库', cls: 'ok', why: '' });
        saveSent();
        paintSent();
        paintQueue();
        tail.textContent = '已入库：' + res.name;
      } else if (res.stop) {
        it.status = 'wait';
        it.error = res.why;
        await qSave(it).catch(() => {});
        paintQueue();
        break;                              // 网络/窗口问题，后面的不用白试
      } else if (res.fatal) {
        it.status = 'error';
        it.error = res.why;
        await qSave(it).catch(() => {});
        paintQueue();
      } else {
        it.status = 'wait';
        it.error = res.why;
        await qSave(it).catch(() => {});
        paintQueue();
      }
    }
  } finally {
    syncing = false;
  }
  paintQueue();
}

input.addEventListener('change', async () => {
  const files = [...input.files];
  input.value = '';                       // 允许再选同一张
  if (!files.length) return;

  let added = 0;
  let bad = 0;
  for (const file of files) {
    const it = {
      uid: newUid(), blob: file, filename: file.name || '手机照片.jpg',
      name: '', no: '', tag: '', folderId: currentFolderId(),
      createdAt: Date.now(), touchedAt: Date.now(),
      status: 'wait', error: '',
    };
    try {
      await qSave(it);                    // 先存住 —— 存不住就别假装收了
    } catch (e) {
      bad++;
      tail.textContent = `手机空间不够，这张没存下来：${it.filename}`
        + '（先把已经拍的传上去，再来拍）';
      continue;
    }
    Q.push(it);
    added++;
    paintQueue();
    const rows = qrows.querySelectorAll('.qrow');
    const last = rows[rows.length - 1];
    if (last && last.scrollIntoView) last.scrollIntoView({ block: 'nearest' });
  }

  if (added) {
    tail.textContent = `已先存到手机（${added} 张），有网就会自动传到电脑`;
    syncQueue(false);
  } else if (!bad) {
    tail.textContent = '';
  }
  await loadPend();          // 有网时顺便刷新一下电脑上的待确认列表
});

// ---------- 待确认导入列表 ----------
// 和电脑上是**同一份**（服务端的 pending_imports 表），两边看到的一样。
const pend = document.getElementById('pend');
const prows = document.getElementById('prows');
let P = { items: [] };
// 改名是防抖的（400ms），而服务端是拿这一行的名字给它起名的 ——
// 打完字立刻点「确认导入」的话，名字还堵在定时器里没发出去，整批就按旧名字
// 入库，而提示语只会说「导入 N 张」，用户完全看不出名字不对。
// 所以确认之前必须先把它们冲出去（见 pFlushNames）。
// 每行**自己**一个待发的改名（id -> { timer, run }）
const pSaves = new Map();

// 上一次 pj 失败时服务端说的那句话（成功就清空）
let pjError = '';

async function pj(url, method, body) {
  let r;
  try {
    r = await fetch(url, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    // 晶体房里断网是常态：别让 fetch 的拒绝一路冒出去变成白屏。
    pjError = '连不上电脑，等有网再试';
    return null;
  }
  if (r.ok) { pjError = ''; return r.json(); }
  // ⚠️ 失败不能一声不吭：手机上 `:` `/` `?` 就在符号键盘第一层，打个「A1/B2」很正常，
  // 服务端会 400 掉。那句中文本来就写好了，吞掉它的话输入框里还是用户打的字、
  // 服务端存的还是旧名字，点「确认导入」就按旧名字入库 —— 全程一个字都不说。
  pjError = '没成功，再试一次';
  try {
    const d = await r.json();
    if (d && typeof d.detail === 'string') pjError = d.detail;
  } catch { /* 服务端没给 JSON，用兜底那句 */ }
  return null;
}

// 在这一行下面写一句话（改名没存上时用）。传空串就把那句话撤掉。
function pRowError(row, msg) {
  const old = row.querySelector('.pErr');
  if (!msg) { if (old) old.remove(); return; }
  const p = old || document.createElement('p');
  p.className = 'pErr';
  p.textContent = msg;
  if (!old) row.append(p);
}

// 收下服务端返回的整份列表并重画。
// 服务端每次给的都是全新的 item 对象，前端临时填的「编号」不在里面 ——
// 直接换掉的话输入框里的字就没了，而它还要跟孔位一起拼名字。
// 所以按 id 把编号搬过去。（名字本身早就存到服务端了，丢的只是这个框。）
function pAdopt(d) {
  const old = new Map((P.items || []).map(it => [it.id, it._no]));
  for (const it of d.items || []) {
    const v = old.get(it.id);
    if (v !== undefined) it._no = v;
  }
  P = d;
  paintPend();
}

function paintPend() {
  FOLDERS = P.folders || [];
  // 缓存一份：下次在晶体房里打开页面（断网）也能把文件夹下拉画出来。
  try { localStorage.setItem('jltx.folders', JSON.stringify(FOLDERS)); } catch (e) {}
  paintBatchFolder();
  // 传送记录里的名字跟着这张表走（改名是在这张表上改的）。
  // ⚠️ 必须在这句 return **之前** —— 导完最后一张时 P.items 是空的，
  //    放后面的话最后一次改名就同步不过去了。
  syncSentNames();
  const ok = P.items.filter(i => i.status === 'ok').length;
  pend.hidden = !P.items.length;
  if (!P.items.length) return;
  document.getElementById('pn').textContent = ok;

  prows.innerHTML = '';
  for (const it of P.items) prows.append(pendRow(it));
}

function pendRow(it) {
  const row = document.createElement('div');
  row.className = 'prow' + (it.status === 'failed' ? ' bad' : '');

  const top = document.createElement('div');
  top.className = 'ptop';
  if (it.status === 'ok') {
    const img = document.createElement('img');
    img.src = `/m/${token}/imports/${it.id}/thumb`;
    img.alt = '';
    top.append(img);
  }

  const inp = document.createElement('input');
  inp.type = 'text';
  inp.value = it.name;
  inp.placeholder = '孔位（如 A1-1）或名字，留空用原始文件名';
  // 失败的行**不禁用**：暂存成功、只是上一次没进库的行（名字超长）暂存文件还在，
  // 改短名字再点「确认导入」就能进库。禁掉的话用户改不了、再点还是失败，
  // 唯一出口是 ✕（那会把暂存文件真删掉，照片得重拍）。
  let t = null;
  // 到点了才发，发的是这一刻输入框里的字（不是排队那会儿的）
  const send = async () => {
    pSaves.delete(it.id);
    const d = await pj(`/m/${token}/imports/${it.id}`, 'PATCH', { name: inp.value });
    if (!d) { pRowError(row, pjError); return; }     // 没存上就把原因摆在行下面
    pRowError(row, '');
    // 名字改对了、服务端说这一行能进库了 → 上次那句红字撤掉，它已经不成立了
    if (d.status === 'ok') {
      const why = row.querySelector('.why');
      if (why) why.remove();
    }
    // 只更新冲突提示，不重画 —— 重画会把手机键盘顶掉
    const cur = P.items.find(x => x.id === it.id) || it;
    cur.name = d.name;
    cur.conflict = d.conflict;
    paintNote(row, cur);
    // 传送记录里的名字跟着变。以前只在整表重画时同步，用户打完字不触发重画，
    // 记录就一直挂着手机文件名（2026-10-22 用户报的"看不到手动命名"）。
    syncSentNames();
  };
  inp.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(send, 400);
    pSaves.set(it.id, { timer: t, run: send });   // 记着它，确认导入时要先冲出去
  });
  top.append(inp);

  const rm = document.createElement('button');
  rm.className = 'rm';
  rm.type = 'button';
  rm.textContent = '✕';
  rm.title = '移除这一张';
  rm.addEventListener('click', async () => {
    await pFlushNames();       // 整表重画之前，先把别的行刚打的字发出去
    const d = await pj(`/m/${token}/imports/${it.id}`, 'DELETE');
    if (d) { markSent([it.id], '已移除', ''); pAdopt(d); }
  });
  top.append(rm);

  // 第二行：左边填编号，右边三个孔位下拉。
  // ⚠️ 这个位置以前是「这一张放哪个文件夹」的下拉 —— 和上面「这批照片放到」
  //    是同一件事，用户 2026-09-23 要求换成让他自己填晶体板编号的空白框。
  //    整批放哪个文件夹，现在只由上面那一个下拉决定。
  const bot = document.createElement('div');
  bot.className = 'pbot';

  const no = document.createElement('input');
  no.type = 'text';
  no.className = 'pno';
  no.placeholder = '编号';
  no.title = '晶体板编号 —— 填了这张就改名成「编号-行-列-孔」';
  no.maxLength = 60;          // 再拼上「-行-列-孔」也离服务端那 100 字上限很远
  no.autocomplete = 'off';
  no.spellcheck = false;
  if (it._no !== undefined) no.value = it._no;    // 整表重画前填过的，接着用

  // 注意：这段 JS 是 Python 的普通字符串，正则里不能出现反斜杠转义
  // （写了的话 Python 会抛 SyntaxWarning，严格模式下直接报错）。
  // 所以列号写成 [1-9]|1[0-2]，正好和服务端那条 _TAG_RE 一字不差。
  const m = /^([A-H])([1-9]|1[0-2])-([12])$/.exec((it.tags || [])[0] || '');
  const selA = mkSel(ROWS, m ? m[1] : '');
  const selN = mkSel(COLS, m ? m[2] : '');
  // 孔位没有「不选」这一档：没标过的默认停在孔1（用户 2026-10-06）。
  const selH = mkSel(HOLES, m ? (m[3] === '1' ? '孔1' : '孔2') : HOLES[0], false);
  for (const s of [selA, selN, selH]) {
    s.addEventListener('change', async () => {
      await pFlushNames();     // 同上：重画之前先把排队的名字发出去
      const t = tagOf(selA, selN, selH);
      const body = { tags: t ? [t] : [] };
      // 编号 + 孔位 = 名字，和电脑上图库那个打标签对话框同一个规则。
      // ⚠️ 编号留空时**不发 name**：后端收到空串会真把名字清掉（等于回退成原始文件名）。
      const nm = nameWithPlate(no.value.trim(), t);
      if (nm) body.name = nm;
      const d = await pj(`/m/${token}/imports/${it.id}`, 'PATCH', body);
      if (!d) { pRowError(row, pjError); return; }
      pRowError(row, '');
      it.tags = d.tags;
      it.name = d.name;
      inp.value = d.name;      // 上面那个名字框跟着变，用户看得见改名生效了
      // 只更新冲突提示，不重画 —— 重画会把手机键盘顶掉，也会把别人行里
      // 刚打的字顶回旧值（它们还堵在定时器里）
      paintNote(row, it);
      syncSentNames();
    });
  }

  // 编号框自己改了也要重算名字（孔位已经选好的话，两个一起拼）。
  let nt = null;
  const sendNo = async () => {
    pSaves.delete(`no:${it.id}`);
    const nm = nameWithPlate(no.value.trim(), tagOf(selA, selN, selH));
    if (!nm) return;                    // 编号留空 = 不动名字
    const d = await pj(`/m/${token}/imports/${it.id}`, 'PATCH', { name: nm });
    if (!d) { pRowError(row, pjError); return; }
    pRowError(row, '');
    it.name = d.name;
    inp.value = d.name;
    syncSentNames();
  };
  no.addEventListener('input', () => {
    it._no = no.value;
    clearTimeout(nt);
    nt = setTimeout(sendNo, 400);
    // 和名字框同一个规矩：记进 pSaves，整表重画之前先把它发出去
    pSaves.set(`no:${it.id}`, { timer: nt, run: sendNo });
  });

  bot.append(no, selA, selN, selH);
  row.append(top, bot);

  if (it.status === 'failed') {
    const why = document.createElement('p');
    why.className = 'why';
    // 「没成功」而不是「没读出来」：确认阶段失败的原因（名字太长、写库失败）
    // 套上「没读出来」就成了"没读出来：名字不能超过 100 个字"，驴唇不对马嘴
    why.textContent = '没成功：' + (it.reason || '原因不明');
    row.append(why);
  } else {
    paintNote(row, it);
  }
  return row;
}

function paintNote(row, it) {
  let p = row.querySelector('.note');
  if (!p) {
    p = document.createElement('p');
    p.className = 'note';
    row.append(p);
  }
  // 手机上不给「覆盖」按钮 —— 覆盖会删掉旧照片的测量记录，
  // 在手机上没法把"会毁掉什么"讲清楚。手机会自动加后缀，覆盖去电脑上做。
  p.hidden = !it.conflict;
  if (it.conflict) {
    p.textContent = `图库里已经有「${it.conflict.name}」，导入时会自动加后缀`;
  }
}

async function loadPend() {
  // 断网时这一句会直接抛（fetch 拒绝）。手机的主流程已经改成"先存本地"，
  // 这里失败就静默 —— 不能白屏，也不能弹个红字吓人。
  try {
    const d = await pj(`/m/${token}/imports`, 'GET');
    if (d) { pAdopt(d); }
  } catch (e) { /* 等有网再刷新 */ }
}

// ---------- 文件夹 ----------
// 树是服务端随每次列表一起给的（不单独开 /m/<令牌>/folders）：
// 每个文件夹改动都会跟着列表一起刷，少一个"树是旧的、行是新的"的窗口。
let FOLDERS = [];
// 断网时服务端给不了树，先用上次缓存的那份把下拉画出来。
// 手机锁屏重载很常见；没有这个缓存，晶体房里文件夹下拉会是空的。
try {
  const cached = localStorage.getItem('jltx.folders');
  if (cached) {
    const arr = JSON.parse(cached);
    if (Array.isArray(arr)) FOLDERS = arr;
  }
} catch (e) { FOLDERS = []; }

// 手机页是从电脑上的二维码进来的，地址里带着"你在图库看的那个文件夹"。
// 读不到 / 解析不出 / 不是正整数 → null = 不放进文件夹。
// ⚠️ 服务端还会再校验一次它到底存不存在（可能在扫码之后被删了），
// 这里只负责把字符串变成数字。
const DEFAULT_FOLDER = (() => {
  const v = new URLSearchParams(location.search).get('folder');
  if (!v || !/^[0-9]+$/.test(v)) return null;
  const n = Number(v);
  return n > 0 ? n : null;
})();

// 「这批照片放到」的当前值：扫码 URL 带过来的优先，没有就用上次选的。
// 队列里的每一行在建立时就记下这个值 —— 回有网后据此归文件夹。
let batchFolderId = DEFAULT_FOLDER;
try {
  if (batchFolderId === null) {
    const saved = localStorage.getItem('jltx.folder');
    if (saved !== null && /^[0-9]+$/.test(saved)) {
      const n = Number(saved);
      batchFolderId = n > 0 ? n : null;
    }
  }
} catch (e) { /* 浏览器禁了 localStorage，就按「不放进文件夹」 */ }

function currentFolderId() { return batchFolderId; }

// 「不放进文件夹」永远排第一：它是默认，也是选错时的退路。
function fillFolderSelect(sel, value) {
  sel.innerHTML = '';
  const first = document.createElement('option');
  first.value = '';
  first.textContent = '不放进文件夹';
  sel.append(first);
  for (const f of FOLDERS) {
    const o = document.createElement('option');
    o.value = String(f.id);
    // 缩进靠不换行空格 —— 手机上 <option> 吃不了 CSS 的 padding
    o.textContent = '  '.repeat(f.depth) + f.name;
    sel.append(o);
  }
  // 服务端给的 id 如果已经不在树里了（那个文件夹被删了），赋值会被忽略、
  // 停在第一项 —— 也就是「不放进文件夹」，正是我们要的兜底。
  sel.value = value === null || value === undefined ? '' : String(value);
}

const pfolder = document.getElementById('pfolder');

function folderValue(sel) {
  return sel.value ? Number(sel.value) : null;
}

// ---------- 孔位（行 / 列 / 第几个孔） ----------
// 和图库打标签对话框里那三个下拉是同一套：行 + 列选上就算一个完整孔位
// （孔位那栏不选就当孔1），存成 A1-1 那个形状（`_TAG_RE` 只认这一种）。
// 行或列选「—」= 不标。
const ROWS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
const COLS = Array.from({ length: 12 }, (_, i) => String(i + 1));
const HOLES = ['孔1', '孔2'];

// `withBlank=false`：这一栏没有「不选」这一档（孔位就是，见下面 selH）。
function mkSel(options, cur, withBlank = true) {
  const s = document.createElement('select');
  s.className = 'tg';
  if (withBlank) {
    const blank = document.createElement('option');
    blank.value = ''; blank.textContent = '—';      // 不标
    s.append(blank);
  }
  for (const o of options) {
    const op = document.createElement('option');
    op.value = o; op.textContent = o;
    s.append(op);
  }
  s.value = cur;
  s.title = '孔位：第几行 / 第几列 / 同一个池里的第几个孔';
  return s;
}

// 三个下拉 → "A1-1"。行或列没选就是空串（不标）。
// ⚠️ 孔位那一栏没有「不选」（用户 2026-10-06：不选就当孔1），所以不拿它判空 ——
// 默认写成"不是孔2就是孔1"，真出了空值也只会算成孔1。
function tagOf(a, n, h) {
  if (!a.value || !n.value) return '';
  return `${a.value}${n.value}-${h.value === '孔2' ? 2 : 1}`;
}

// 「编号」+ 孔位 = 名字：编号 20260923-29 + 孔位 B5 孔1 → 20260923-29-B-5-1。
// 和图库打标签对话框里那条 nameWithPlate() 是同一个规则（名字尾巴带孔号，
// 不然同一个池的两个孔会拼出一模一样的名字，在列表里分不出谁是谁）。
// 行或列没选时名字就只剩编号本身 —— 用户填了编号就照他填的来。
function nameWithPlate(no, tag) {
  // ⚠️ 没填编号就返回空串（= 不改名字）。少了这一行，空编号 + 选好的孔位会
  // 拼出一个 "-B-3-1" 发出去 —— 用户没碰编号，名字却被改成了个横杠开头的怪东西。
  if (!no) return '';
  const m = /^([A-H])([1-9]|1[0-2])-([12])$/.exec(tag || '');
  return m ? `${no}-${m[1]}-${m[2]}-${m[3]}` : no;
}

pfolder.addEventListener('change', async () => {
  batchFolderId = folderValue(pfolder);
  try {
    if (batchFolderId === null) localStorage.removeItem('jltx.folder');
    else localStorage.setItem('jltx.folder', String(batchFolderId));
  } catch (e) { /* 存不了就只管这一次 */ }
  // 还没传上去的行跟着改 —— 和电脑上「这批放到」是同一个语义。
  for (const it of Q) {
    it.folderId = batchFolderId;
    qSave(it).catch(() => {});
  }
  paintQueue();

  await pFlushNames();         // 整表重画之前，先把排队的名字发出去
  try {
    const d = await pj(`/m/${token}/imports/folder`, 'PUT', { folder_id: batchFolderId });
    if (d) { pAdopt(d); }
  } catch (e) { /* 断网：队列里已经记下了，有网再看电脑上的列表 */ }
});

// 整批那个下拉显示当前选择。手机上新流程以**本机队列**为准，
// 所以不再拿电脑上那份待确认列表的第一行来覆盖用户的选择。
function paintBatchFolder() {
  fillFolderSelect(pfolder, batchFolderId);
}

// 把还在等那 400ms 的改名立刻都发出去，并等服务端都收下。
async function pFlushNames() {
  const jobs = [...pSaves.values()];
  pSaves.clear();
  await Promise.all(jobs.map(s => { clearTimeout(s.timer); return s.run(); }));
}

document.getElementById('pok').addEventListener('click', async () => {
  const pok = document.getElementById('pok');
  if (pok.disabled) return;                 // 正在导入中，再点也没用
  // 点下去立刻锁住按钮并换文案：下面要先做一次往返（把排队中的改名冲出去），
  // 手机上等半天没反应，用户一定会再点一下 —— 而服务端两次确认是并行的，
  // 同一批照片就入库两遍了。
  pok.disabled = true;
  pok.textContent = '正在导入…';
  try {
    // 先把 400ms 内刚打的名字发出去：确认要带着它。
    // 晚一步的话整批照片按旧名字入库，用户完全看不出来。
    await pFlushNames();
    const before = P.items.map(i => i.id);   // 这批是谁 —— 确认完它们就不在列表里了
    const d = await pj(`/m/${token}/imports/confirm`, 'POST', {});
    if (!d) { tail.textContent = pjError || '没成功，再点一次试试'; return; }
    const msgs = [];
    if (d.imported) msgs.push(`导入 ${d.imported} 张`);
    if (d.unnamed) msgs.push(`有 ${d.unnamed} 张没命名，用了原始文件名`);
    if (d.skipped) msgs.push(`${d.skipped} 张没成功的跳过了`);
    tail.textContent = msgs.join('；') || '没什么要导入的';
    await loadPend();
    // 真离开待确认列表的那些才算入库（没成功的那几张会留下来，不能一起标）
    const still = new Set(P.items.map(i => i.id));
    markSent(before.filter(id => !still.has(id)), '已导入', 'ok');
  } finally {
    pok.disabled = false;
    pok.textContent = '确认导入';
  }
});

document.getElementById('pno').addEventListener('click', async () => {
  if (!confirm(`取消后这 ${P.items.length} 张要重新传一遍，确定吗？`)) return;
  const before = P.items.map(i => i.id);
  const d = await pj(`/m/${token}/imports`, 'DELETE');
  tail.textContent = d ? `已取消 ${d.cleared} 张` : '没成功，再点一次试试';
  await loadPend();
  if (d) markSent(before, '已取消', '');    // 记录留着（传过就是传过），只换个状态
});

paintSent();         // 先把这次的传送记录画出来（存在 sessionStorage 里的）
paintBatchFolder();  // 断网也能把文件夹下拉画出来（用上次缓存的那棵树）
qLoad();             // 读手机里还没传的照片；有网就自动接着传
loadPend();          // 打开页面就先看有没有上次没确认的

// 回到有网 / 回到这个页面，都试着补传一次。
window.addEventListener('online', () => syncQueue(false));
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) syncQueue(false);
});
setInterval(() => {
  if (!document.hidden) syncQueue(false);
}, RETRY_MS);
document.getElementById('qall').addEventListener('click', () => {
  pausedUntil = 0;
  syncQueue(true);
});
</script>
</body>
</html>
"""


@router.get("/m/{token}", response_class=HTMLResponse)
def mobile_page(token: str) -> HTMLResponse:
    # 令牌不对的话中间件已经拦掉了；这里只要不是空就发页面
    if not token:
        raise HTTPException(404, "地址不对")
    # ⚠️ `no-store`：这一页整个存在服务端内存里（`_PAGE`），改完必须重启才变。
    # 而这个响应**没有任何校验标记**（没有 ETag / Last-Modified），手机浏览器拿到
    # 一份就可能一直用自己缓存的那份 —— 用户"重启了，手机上还是老样子"十有八九是它。
    # 桌面那两个页面是 FileResponse（自带 ETag），浏览器每次都会回来问，不用管。
    return HTMLResponse(_PAGE, headers={"Cache-Control": "no-store"})


@router.post("/m/{token}/upload")
async def mobile_upload(token: str, files: list[UploadFile] = File(...),
                        mode: str = "auto", folder: int | None = None,
                        name: str | None = Form(None),
                        tag: str | None = Form(None),
                        uid: str | None = Form(None),
                        conn=Depends(get_db)) -> dict:
    """手机上传。

    mode 由手机页自己传：这个页面一次请求只带一张，服务端数不出"用户这次选了几张"，
    所以"≥2 张才进待确认列表"这条规则只能由知道总数的那一端说了算。
    默认 auto 在单张请求下就等于 direct —— 和以前一样。

    folder 来自扫码时二维码里带的 `?folder=`（图库当时在看的文件夹）。在**查询串**
    上而不是路径上，所以不影响 lan_guard 的安全模型 —— 它比的是 request.url.path。

    name / tag / uid 是离线队列用的（2026-10-22）：
    - name：拍照时填的编号 + 孔位拼出来的名字；空 = 从文件名派生（老行为）。
    - tag：孔位（B5-1）；只认服务端那一份 `_TAG_RE`，别的形状直接 400。
    - uid：这张照片在手机上的唯一编号。同一个 uid 重传只会入库一次 ——
      网络差的时候"电脑收到了、手机没收到回执"是常态。
    """
    if len(files) > MAX_FILES:
        raise HTTPException(400, f"一次最多传 {MAX_FILES} 张")
    name = (name or "").strip() or None
    tag = (tag or "").strip() or None
    if tag and not _TAG_RE.fullmatch(tag):
        raise HTTPException(400, f"标签「{tag}」不对，应该是 A1-1 这样的")
    uid = (uid or "").strip() or None
    if uid and len(uid) > 64:
        raise HTTPException(400, "这张照片的编号太长了")
    return await _ingest(conn, files, mode=mode, source="mobile",
                         default_name="手机照片", folder=folder,
                         name=name, tag=tag, uid=uid)
