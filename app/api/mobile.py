"""手机扫码传图。

安全模型（配合 app/main.py 里的 lan_guard 中间件）：
- 服务绑 0.0.0.0，但默认**关着** —— 局域网里什么都进不来，包括这里。
- 电脑上点「开启手机传图」才开一个 30 分钟的口子。
- 开口子期间也只放行 `/m/<令牌>` 这一个前缀，**任何别的路径一律 403**。
- 手机上能做的只有"传"：这个文件里没有任何列出/查看/删除已有照片的路由。
- 令牌存在 data/mobile_token.txt，重启不换 —— 否则手机书签每天都失效。
"""
import time

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse

from app import storage
from app.api.images import MAX_FILES, _ingest
from app.db import get_db

router = APIRouter(tags=["mobile"])

# 开一次口子管多久。够传完一批照片，又不用记得关。
MOBILE_WINDOW_SEC = 30 * 60


def _urls(request: Request) -> list[str]:
    """手机该访问哪些地址。可能多个 —— 这台机器有虚拟网卡和 VPN。"""
    # 正常情况下 Host 头里有端口（浏览器访问的是 127.0.0.1:8510）。
    # 没有就退回启动时记下的端口 —— TestClient 就是这种情况。
    port = request.url.port or getattr(request.app.state, "port", None)
    if not port:
        return []
    path = f"/m/{request.app.state.mobile_token}"
    return [f"http://{ip}:{port}{path}" for ip in storage.local_ips()]


def _state(request: Request) -> dict:
    st = request.app.state
    left = max(0, int(st.mobile_off_at - time.time())) if st.mobile_on else 0
    return {"urls": _urls(request), "on": left > 0, "expires_in": left}


@router.get("/api/net/info")
def net_info(request: Request) -> dict:
    return _state(request)


@router.post("/api/net/enable")
def net_enable(request: Request) -> dict:
    """开 30 分钟。只能从本机调 —— 中间件保证局域网调不到这个路径。"""
    request.app.state.mobile_on = True
    request.app.state.mobile_off_at = time.time() + MOBILE_WINDOW_SEC
    return _state(request)


@router.get("/api/net/qrcode")
def net_qrcode(request: Request, i: int = 0) -> Response:
    urls = _urls(request)
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
  .pfxrow { display: block; color: var(--mute); font-size: 13.5px; margin-bottom: 14px; }
  .pfxrow input {
    display: block; width: 100%; margin-top: 6px; font: 16px var(--ui);
    color: var(--text); background: var(--ink); border: 1px solid var(--rule);
    border-radius: 3px; padding: 10px 11px;
  }
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
  .prow .why { margin: 7px 0 0; color: #e8746a; font-size: 13px; }
  .prow .pErr { margin: 7px 0 0; color: #e8746a; font-size: 13px; }
  .prow .note { margin: 7px 0 0; color: var(--ref); font-size: 13px; }
  .pfoot { display: flex; gap: 10px; margin-top: 14px; }
  .pfoot button { flex: 1 1 0; font: 600 16px var(--ui); padding: 14px 0;
                  border-radius: 4px; border: 1px solid var(--rule);
                  background: var(--raise); color: var(--text); }
  .pfoot #pok { background: var(--ref); color: #241c08; border-color: var(--ref); }
</style>
</head>
<body>
<main>
  <h1>传照片到电脑</h1>
  <p class="sub">选好之后自动开始传，一张一张来，传完这里会告诉你结果。</p>

  <label class="pick" id="btn">拍照 / 选照片
    <input type="file" accept="image/*" multiple hidden id="f">
  </label>

  <section id="pend" hidden>
    <h2>待确认导入（<span id="pn">0</span> 张）</h2>
    <label class="pfxrow">统一前缀
      <input id="ppfx" type="text" maxlength="60" autocomplete="off"
             spellcheck="false" placeholder="如 EXP0615_">
    </label>
    <div id="prows"></div>
    <div class="pfoot">
      <button id="pok" type="button">确认导入</button>
      <button id="pno" type="button">取消</button>
    </div>
  </section>

  <ul id="list"></ul>
  <p class="tail" id="tail"></p>
</main>

<script>
const token = location.pathname.split('/')[2] || '';
const btn = document.getElementById('btn');
const input = document.getElementById('f');
const list = document.getElementById('list');
const tail = document.getElementById('tail');

input.addEventListener('change', async () => {
  const files = [...input.files];
  input.value = '';                       // 允许再选同一张
  if (!files.length) return;

  btn.classList.add('busy');
  btn.firstChild.textContent = '正在传…';
  list.innerHTML = '';
  let done = 0, bad = 0;

  // 一次选多张才进待确认列表 —— 这个页面一个请求只带一张，
  // 服务端数不出总数，所以由这里说了算
  const mode = files.length >= 2 ? 'pending' : 'direct';

  // 一张一张传 —— 手机上看得见进度，也不用把整批堆在内存里
  for (const file of files) {
    const li = document.createElement('li');
    li.innerHTML = '<span class="n"></span><span class="s">上传中</span>';
    li.querySelector('.n').textContent = file.name;
    list.append(li);

    try {
      const fd = new FormData();
      fd.append('files', file, file.name);
      const r = await fetch(`/m/${token}/upload?mode=${mode}`, { method: 'POST', body: fd });
      const d = await r.json();
      if (r.ok && d.ok && d.ok.length) {
        li.classList.add('ok');
        li.querySelector('.s').textContent = '已传到电脑';
        done++;
      } else if (r.ok && d.staged && d.staged.length) {
        li.classList.add('ok');
        li.querySelector('.s').textContent = '进了待确认列表';
        done++;
      } else {
        throw new Error((d.failed && d.failed[0] && d.failed[0].reason)
                        || d.detail || '没传上去');
      }
    } catch (err) {
      li.classList.add('bad');
      li.querySelector('.s').textContent = '失败';
      const why = document.createElement('span');
      why.className = 'why';
      why.textContent = err.message;
      li.append(why);
      bad++;
    }
  }

  btn.classList.remove('busy');
  btn.firstChild.textContent = '再传几张';
  tail.textContent = bad
    ? `传完 ${done} 张，${bad} 张没成功`
    : (mode === 'pending'
       ? `传完 ${done} 张 —— 在下面起好名字，点「确认导入」`
       : `传完 ${done} 张 —— 回到电脑上刷新照片列表就能看到`);
  await loadPend();          // 传完把待确认列表刷新出来
});

// ---------- 待确认导入列表 ----------
// 和电脑上是**同一份**（服务端的 pending_imports 表），两边看到的一样。
const pend = document.getElementById('pend');
const prows = document.getElementById('prows');
const ppfx = document.getElementById('ppfx');
let P = { items: [], prefix: '' };
// 前缀和改名都是防抖的（400ms），而服务端是拿「前缀 + 名字」给整批照片起名的 ——
// 打完字立刻点「确认导入」的话，名字还堵在定时器里没发出去，整批就按旧名字
// （甚至无前缀）入库，而提示语只会说「导入 N 张」，用户完全看不出名字不对。
// 所以确认之前必须先把它们冲出去（见 pFlushNames / pFlushPrefix）。
let pfxTimer = null;
let pfxFlight = null;     // 已经发出去、还没回来的那次前缀 PUT
let pfxKnown = null;      // 服务端确认过的前缀：同一个值不重复发
let pfxFlushing = false;  // 正在把"还没发出去的那个前缀"冲出去：这期间别动输入框
// 每行**自己**一个待发的改名（id -> { timer, run }）
const pSaves = new Map();

// 上一次 pj 失败时服务端说的那句话（成功就清空）
let pjError = '';

async function pj(url, method, body) {
  const r = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
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

function paintPend() {
  pfxKnown = P.prefix;      // 服务端刚说了它的前缀是这个 → 同一个值不用再发一遍
  const ok = P.items.filter(i => i.status === 'ok').length;
  pend.hidden = !P.items.length;
  if (!P.items.length) return;
  document.getElementById('pn').textContent = ok;
  // 还有没送出去的前缀（用户在等这次请求的路上又打了字，或者正冲着呢）→
  // 输入框里那个新值说了算，别拿刚回来的旧值把它冲掉；
  // 它自己有定时器（或正被 flush 送出去），发出去之后服务端就是它了
  if (ppfx.value !== P.prefix && !pfxTimer && !pfxFlushing) ppfx.value = P.prefix;

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
  inp.placeholder = '留空就用原始文件名';
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
    const d = await pj(`/m/${token}/imports/${it.id}`, 'DELETE');
    if (d) { P = d; paintPend(); }
  });
  top.append(rm);
  row.append(top);

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
  const d = await pj(`/m/${token}/imports`, 'GET');
  if (d) { P = d; paintPend(); }
}

// 把前缀发到服务端。串行 + 同一个值不重复发：两个 PUT 并发时回来的顺序不定，
// 服务端可能停在旧值上；而防抖刚发出去的那次，flush 等它落地就够了。
function pSendPrefix(value) {
  const prev = pfxFlight;
  const promise = (async () => {
    if (prev) await prev.promise;
    const d = await pj(`/m/${token}/imports/prefix`, 'PUT', { prefix: value });
    if (d) { P = d; paintPend(); }
    else {
      pfxKnown = null;         // 没成功 → 不知道服务端现在是什么，逼下一次真的发
      tail.textContent = pjError || '前缀没存上，再试一次';
    }
    return d;
  })();
  pfxFlight = promise;
  const settled = () => {      // 落地后把自己摘掉
    if (pfxFlight === promise) pfxFlight = null;
  };
  promise.then(settled, settled);
  return promise;
}

// 把还没发出去的前缀立刻送出去，并等服务端收下 —— 确认导入之前必须跑到这一步。
// 返回之后服务端存的前缀就是输入框里的字。
async function pFlushPrefix() {
  if (pfxTimer) { clearTimeout(pfxTimer); pfxTimer = null; }
  // ⚠️ 要发的字**等之前先记下来**：等在飞的那次时它的响应会回来重画，
  // 那会儿定时器已经被清掉了，输入框可能被服务端的旧值顶掉 ——
  // 照着被顶掉的值发就等于什么都没修。
  const v = ppfx.value;
  pfxFlushing = true;                        // 这期间 paintPend 不许动输入框
  try {
    if (pfxFlight) await pfxFlight;          // 上路的先落地，别和它抢
    if (pfxKnown !== v) await pSendPrefix(v);   // 服务端已经是这个值就不补第二枪
  } finally {
    pfxFlushing = false;
  }
}

// 把还在等那 400ms 的改名立刻都发出去，并等服务端都收下。
async function pFlushNames() {
  const jobs = [...pSaves.values()];
  pSaves.clear();
  await Promise.all(jobs.map(s => { clearTimeout(s.timer); return s.run(); }));
}

ppfx.addEventListener('input', () => {
  clearTimeout(pfxTimer);
  // 防抖：每敲一个字就往服务端跑一趟太浪费
  pfxTimer = setTimeout(() => {
    pfxTimer = null;                // 到点了：待发的没了，发出去的这次记在 pfxFlight 上
    pSendPrefix(ppfx.value);        // 发的是这一刻输入框里的字
  }, 400);
});

document.getElementById('pok').addEventListener('click', async () => {
  const pok = document.getElementById('pok');
  if (pok.disabled) return;                 // 正在导入中，再点也没用
  // 点下去立刻锁住按钮并换文案：下面要先做两次往返（冲名字、冲前缀），
  // 手机上等半天没反应，用户一定会再点一下 —— 而服务端两次确认是并行的，
  // 同一批照片就入库两遍了。
  pok.disabled = true;
  pok.textContent = '正在导入…';
  try {
    // 先把 400ms 内刚打的名字和前缀发出去：确认要带着它们。
    // 晚一步的话整批照片按旧名字入库，用户完全看不出来。
    await pFlushNames();
    await pFlushPrefix();
    const d = await pj(`/m/${token}/imports/confirm`, 'POST', {});
    if (!d) { tail.textContent = pjError || '没成功，再点一次试试'; return; }
    const msgs = [];
    if (d.imported) msgs.push(`导入 ${d.imported} 张`);
    if (d.unnamed) msgs.push(`有 ${d.unnamed} 张没命名，用了原始文件名`);
    if (d.skipped) msgs.push(`${d.skipped} 张没成功的跳过了`);
    tail.textContent = msgs.join('；') || '没什么要导入的';
    await loadPend();
  } finally {
    pok.disabled = false;
    pok.textContent = '确认导入';
  }
});

document.getElementById('pno').addEventListener('click', async () => {
  if (!confirm(`取消后这 ${P.items.length} 张要重新传一遍，确定吗？`)) return;
  const d = await pj(`/m/${token}/imports`, 'DELETE');
  tail.textContent = d ? `已取消 ${d.cleared} 张` : '没成功，再点一次试试';
  await loadPend();
});

loadPend();          // 打开页面就先看有没有上次没确认的
</script>
</body>
</html>
"""


@router.get("/m/{token}", response_class=HTMLResponse)
def mobile_page(token: str) -> HTMLResponse:
    # 令牌不对的话中间件已经拦掉了；这里只要不是空就发页面
    if not token:
        raise HTTPException(404, "地址不对")
    return HTMLResponse(_PAGE)


@router.post("/m/{token}/upload")
async def mobile_upload(token: str, files: list[UploadFile] = File(...),
                        mode: str = "auto", conn=Depends(get_db)) -> dict:
    """手机上传。

    mode 由手机页自己传：这个页面一次请求只带一张，服务端数不出"用户这次选了几张"，
    所以"≥2 张才进待确认列表"这条规则只能由知道总数的那一端说了算。
    默认 auto 在单张请求下就等于 direct —— 和以前一样。
    """
    if len(files) > MAX_FILES:
        raise HTTPException(400, f"一次最多传 {MAX_FILES} 张")
    return await _ingest(conn, files, mode=mode, source="mobile",
                         default_name="手机照片")
