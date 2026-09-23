"""手机扫码传图。

安全模型（配合 app/main.py 里的 lan_guard 中间件）：
- 服务绑 0.0.0.0，但默认**关着** —— 局域网里什么都进不来，包括这里。
- 电脑上点「开启手机传图」才开一个 2 小时的口子。
- 开口子期间也只放行 `/m/<令牌>` 这一个前缀，**任何别的路径一律 403**。
- 手机上能做的只有"传"：这个文件里没有任何列出/查看/删除已有照片的路由。
- 令牌存在 data/mobile_token.txt，重启不换 —— 否则手机书签每天都失效。
"""
import time

from fastapi import (APIRouter, Depends, File, HTTPException, Query, Request,
                     Response, UploadFile)
from fastapi.responses import HTMLResponse

from app import storage
from app.api.images import MAX_FILES, _ingest
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
    <label class="pfxrow">这批照片放到
      <select id="pfolder"></select>
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

  // 手机上**恒为 pending**：单张也要停下来选文件夹、填孔位。
  // （电脑端不一样 —— 那边一个请求带多个文件，≥2 张才进列表。）
  const mode = 'pending';

  // 一张一张传 —— 手机上看得见进度，也不用把整批堆在内存里
  for (const file of files) {
    const li = document.createElement('li');
    li.innerHTML = '<span class="n"></span><span class="s">上传中</span>';
    li.querySelector('.n').textContent = file.name;
    list.append(li);

    try {
      const fd = new FormData();
      fd.append('files', file, file.name);
      const q = new URLSearchParams({ mode });
      if (DEFAULT_FOLDER !== null) q.set('folder', String(DEFAULT_FOLDER));
      const r = await fetch(`/m/${token}/upload?${q}`, { method: 'POST', body: fd });
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
    : `传完 ${done} 张 —— 在下面起好名字，点「确认导入」`;
  await loadPend();          // 传完把待确认列表刷新出来
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
  paintBatchFolder();
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
    if (d) { pAdopt(d); }
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
  const selH = mkSel(HOLES, m ? (m[3] === '1' ? '孔1' : '孔2') : '');
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
  const d = await pj(`/m/${token}/imports`, 'GET');
  if (d) { pAdopt(d); }
}

// ---------- 文件夹 ----------
// 树是服务端随每次列表一起给的（不单独开 /m/<令牌>/folders）：
// 每个文件夹改动都会跟着列表一起刷，少一个"树是旧的、行是新的"的窗口。
let FOLDERS = [];

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
// 和图库打标签对话框里那三个下拉是同一套：三个都选了才算一个完整孔位，
// 存成 A1-1 那个形状（`_TAG_RE` 只认这一种）。全选「—」= 不标。
const ROWS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
const COLS = Array.from({ length: 12 }, (_, i) => String(i + 1));
const HOLES = ['孔1', '孔2'];

function mkSel(options, cur) {
  const s = document.createElement('select');
  s.className = 'tg';
  const blank = document.createElement('option');
  blank.value = ''; blank.textContent = '—';      // 不标
  s.append(blank);
  for (const o of options) {
    const op = document.createElement('option');
    op.value = o; op.textContent = o;
    s.append(op);
  }
  s.value = cur;
  s.title = '孔位：第几行 / 第几列 / 同一个池里的第几个孔';
  return s;
}

// 三个下拉 → "A1-1"。没选全就是空串（不标）。
function tagOf(a, n, h) {
  if (!a.value || !n.value || !h.value) return '';
  return `${a.value}${n.value}-${h.value === '孔1' ? 1 : 2}`;
}

// 「编号」+ 孔位 = 名字：编号 20260923-29 + 孔位 B5 孔1 → 20260923-29-B-5-1。
// 和图库打标签对话框里那条 nameWithPlate() 是同一个规则（名字尾巴带孔号，
// 不然同一个池的两个孔会拼出一模一样的名字，在列表里分不出谁是谁）。
// 三个下拉没选全时名字就只剩编号本身 —— 用户填了编号就照他填的来。
function nameWithPlate(no, tag) {
  // ⚠️ 没填编号就返回空串（= 不改名字）。少了这一行，空编号 + 选好的孔位会
  // 拼出一个 "-B-3-1" 发出去 —— 用户没碰编号，名字却被改成了个横杠开头的怪东西。
  if (!no) return '';
  const m = /^([A-H])([1-9]|1[0-2])-([12])$/.exec(tag || '');
  return m ? `${no}-${m[1]}-${m[2]}-${m[3]}` : no;
}

pfolder.addEventListener('change', async () => {
  await pFlushNames();         // 整表重画之前，先把排队的名字发出去
  const d = await pj(`/m/${token}/imports/folder`, 'PUT', { folder_id: folderValue(pfolder) });
  if (d) { pAdopt(d); }
  else tail.textContent = pjError || '没存上，再试一次';
});

// 整批那个下拉停在"第一行的值"上 —— 行与行不一致时它显示的是第一行，
// 改它 = 下面所有行都变（用户看得见每一行跟着动，不会误以为只改了第一行）。
function paintBatchFolder() {
  const v = P.items.find(it => it.status === 'ok' || it.status === 'failed');
  fillFolderSelect(pfolder, v ? v.folder_id : DEFAULT_FOLDER);
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
                        mode: str = "auto", folder: int | None = None,
                        conn=Depends(get_db)) -> dict:
    """手机上传。

    mode 由手机页自己传：这个页面一次请求只带一张，服务端数不出"用户这次选了几张"，
    所以"≥2 张才进待确认列表"这条规则只能由知道总数的那一端说了算。
    默认 auto 在单张请求下就等于 direct —— 和以前一样。

    folder 来自扫码时二维码里带的 `?folder=`（图库当时在看的文件夹）。在**查询串**
    上而不是路径上，所以不影响 lan_guard 的安全模型 —— 它比的是 request.url.path。
    """
    if len(files) > MAX_FILES:
        raise HTTPException(400, f"一次最多传 {MAX_FILES} 张")
    return await _ingest(conn, files, mode=mode, source="mobile",
                         default_name="手机照片", folder=folder)
