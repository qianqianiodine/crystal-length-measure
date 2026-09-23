// 页面**行为**检查：把内联 <script> 放进一个最小 DOM 里真的跑起来，
// 然后调页面自己的函数、点页面自己的按钮，看它吐出来的 DOM。
//
// 为什么必须有它：web/ 下的页面是单个大 HTML，逻辑全在内联 JS 里。
// `web_check.mjs` 只证明"这段 JS 编译得过"，证明不了"失败行的输入框还禁着"
// 这种逻辑错误 —— 而这类错误不点一下看不出来。用户要求功能由他人工测、
// 不让 Claude 开浏览器，所以这里用最小 DOM 顶上：
// 凡是"点一下会怎样"的问题，都该在这里加一条检查。
//
// 用法：node tests/web_behavior_check.mjs [手机页 HTML 路径]
//   手机页不是文件（它是 app/api/mobile.py 里的 Python 字符串 _PAGE），
//   由 tests/test_web_behavior.py 落成临时文件再传进来；不传就跳过手机页那几条。
// 退出码非 0 = 有检查没过。
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

let passed = 0;
const failed = [];
function check(name, ok, extra) {
  if (ok) { passed++; console.log(`  ok   ${name}`); }
  else { failed.push(name); console.error(`  FAIL ${name}${extra ? ' —— ' + extra : ''}`); }
}

/* ---------------- 最小 DOM ---------------- */

class Txt {
  constructor(s) { this.textContent = String(s); this.parentNode = null; }
}

class El {
  constructor(tag, doc) {
    this.tagName = String(tag).toUpperCase();
    this.ownerDocument = doc;
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = {};
    this._listeners = {};
    this._own = '';
    this.dataset = {};
    this.style = {};
    this.className = '';
    this.value = '';
    this.hidden = false;
    this.disabled = false;
    this.type = '';
    this.checked = false;
  }
  get id() { return this.attributes.id || ''; }
  set id(v) { this.attributes.id = String(v); }
  // 真 DOM 里 checked 是布尔属性：赋进去的不管是 undefined 还是别的，读出来一律是 true/false。
  // 页面里 `rb.checked = checked` 的 checked 常常是没传的 undefined —— 假 DOM 原样存下来，
  // 「默认停在哪个档位」的检查就会把「没勾」读成 undefined 而误判。
  get checked() { return this._checked; }
  set checked(v) { this._checked = !!v; }
  get classList() {
    const self = this;
    const has = c => String(self.className).split(/\s+/).includes(c);
    const put = (c, on) => {
      const s = String(self.className).split(/\s+/).filter(Boolean);
      const i = s.indexOf(c);
      if (on && i < 0) s.push(c);
      if (!on && i >= 0) s.splice(i, 1);
      self.className = s.join(' ');
    };
    return {
      contains: has,
      add: c => put(c, true),
      remove: c => put(c, false),
      toggle: (c, on) => put(c, on === undefined ? !has(c) : !!on),
    };
  }
  get textContent() { return this._own + this.childNodes.map(n => n.textContent).join(''); }
  set textContent(v) { this._own = String(v); this.childNodes = []; }
  set innerHTML(v) {
    const s = String(v);
    this._own = ''; this._html = s; this.childNodes = [];
    // 真 DOM 会把这些 markup 变成节点；这里只认**整串**正好是一串不带嵌套、
    // 不带自闭合的 <tag attr="v">文字</tag>（手机页那行「上传中」的 li 就是）。
    // 别的一律不解析 —— 和加这个之前一样，querySelector 找不到里面任何东西。
    const re = /<([a-zA-Z][\w-]*)((?:\s+[\w-]+="[^"]*")*)\s*>([^<]*)<\/\1>/g;
    const got = [];
    let m, end = 0;
    while ((m = re.exec(s))) {
      if (m.index !== end) return;
      end = m.index + m[0].length;
      got.push(m);
    }
    if (end !== s.length) return;
    for (const g of got) {
      const el = new El(g[1], this.ownerDocument);
      for (const a of g[2].matchAll(/([\w-]+)="([^"]*)"/g)) {
        if (a[1] === 'class') el.className = a[2];
        else el.setAttribute(a[1], a[2]);
      }
      el.textContent = g[3];
      this.append(el);
    }
  }
  get innerHTML() { return this._html || ''; }
  get firstChild() {
    if (!this.childNodes.length) { const t = new Txt(''); t.parentNode = this; this.childNodes.push(t); }
    return this.childNodes[0];
  }
  append(...ns) {
    for (const n of ns) {
      const node = (n && n.tagName) || (n && n.parentNode !== undefined) ? n : new Txt(n);
      node.parentNode = this;
      this.childNodes.push(node);
    }
    return this;
  }
  appendChild(n) { return this.append(n); }
  remove() {
    if (!this.parentNode) return;
    const i = this.parentNode.childNodes.indexOf(this);
    if (i >= 0) this.parentNode.childNodes.splice(i, 1);
    this.parentNode = null;
  }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
  addEventListener(t, fn) { (this._listeners[t] ||= []).push(fn); }
  removeEventListener() {}
  // 返回监听器们交回来的东西（异步处理函数会给出 promise），交给调用方决定等不等
  dispatch(type, ev) {
    const out = [];
    for (const fn of this._listeners[type] || []) out.push(fn(ev || { type, target: this }));
    return out;
  }
  closest(sel) {
    for (let n = this; n; n = n.parentNode) if (n.tagName && match(n, sel)) return n;
    return null;
  }
  querySelector(sel) { return this._find(sel, true)[0] || null; }
  querySelectorAll(sel) { return this._find(sel, true); }
  _find(sel, deep) {
    const out = [];
    for (const c of this.childNodes) {
      if (!c.tagName) continue;
      if (match(c, sel)) out.push(c);
      if (deep) out.push(...c._find(sel, true));
    }
    return out;
  }
}

// 只支持本项目用到的几种选择器：tag、.class、tag.class、[attr="v"]、以及组合
function match(el, sel) {
  const m = /^([a-zA-Z]*)((?:\.[\w-]+|\[[^\]]+\])*)$/.exec(sel);
  if (!m) return false;
  if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
  for (const part of m[2].match(/\.[\w-]+|\[[^\]]+\]/g) || []) {
    if (part[0] === '.') { if (!el.classList.contains(part.slice(1))) return false; continue; }
    const am = /^([\w-]+)(?:="([^"]*)")?$/.exec(part.slice(1, -1));
    if (!am) return false;
    // dataset 的键是去掉 data- 之后转驼峰的：dataset.pid ↔ data-pid
    const v = am[1].startsWith('data-') ? el.dataset[am[1].slice(5)] : el.attributes[am[1]];
    if (am[2] === undefined) { if (v === undefined || v === null) return false; }
    else if (String(v) !== am[2]) return false;
  }
  return true;
}

/* ---------------- 把页面跑起来 ---------------- */

function makePage(htmlFile, pathname, search = '') {
  const calls = [];                       // 按顺序记下每次请求：{ url, method, body }
  const state = { pending: { items: [], total_size: 0, ok_count: 0, failed_count: 0 }, reply: null };

  const mkRes = r => ({ ok: r.ok !== false, status: r.status || 200, json: async () => r.body });

  const doc = {
    _els: new Map(), _listeners: {}, body: null, activeElement: null,
    createElement: t => new El(t, doc),
    createTextNode: s => new Txt(s),
    getElementById(id) {
      if (!doc._els.has(id)) { const e = new El('div', doc); e.id = id; doc._els.set(id, e); }
      return doc._els.get(id);
    },
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener(t, fn) { (doc._listeners[t] ||= []).push(fn); },
    removeEventListener() {},
  };
  doc.body = new El('body', doc);

  const store = new Map();
  const listeners = {};
  const win = {
    addEventListener(t, fn) { (listeners[t] ||= []).push(fn); },
    removeEventListener() {},
    dispatch(t, ev) { return (listeners[t] || []).map(fn => fn(ev || { type: t })); },
    location: null,
  };
  const location = { pathname, search, href: '' };
  win.location = location;
  const sandbox = {
    console, setTimeout, clearTimeout, setInterval, clearInterval,
    URLSearchParams, URL, document: doc, window: win, location,
    history: { replaceState() {}, pushState() {}, back() {} },
    localStorage: {
      getItem: k => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: k => store.delete(k),
    },
    // 上传走的是 new FormData()；沙箱里没有它就会在发出请求**之前**抛 ReferenceError，
    // 于是「粘贴有没有被抢」这类检查会以崩溃收场，而不是报 FAIL。
    // fetch 桩只记 {url, method, body}、不解析 body，这个空壳子够用了。
    FormData: class { constructor() { this._parts = []; } append(...a) { this._parts.push(a); } },
    fetch: async (url, opt = {}) => {
      const method = String(opt.method || 'GET').toUpperCase();
      calls.push({ url, method, body: opt.body });
      if (state.reply) {
        const r = await state.reply(url, method);
        if (r) return mkRes(r);
      }
      if (url === '/api/imports' || /\/imports$/.test(url)) {
        return mkRes({ body: state.pending });
      }
      if (url.startsWith('/api/folders')) return mkRes({ body: { items: [] } });
      if (url.startsWith('/api/images')) return mkRes({ body: { total: 0, items: [] } });
      return mkRes({ body: {} });
    },
    confirm: () => true,
  };
  sandbox.globalThis = sandbox;
  const context = vm.createContext(sandbox);

  const html = readFileSync(htmlFile, 'utf8');
  const blocks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)];
  if (!blocks.length) throw new Error(`${htmlFile} 里没有内联 script`);
  return { context, calls, state, doc, win, html,
           code: blocks.map(m => m[1]).join('\n') };
}

function run(page, exportBlock) {
  vm.runInContext(page.code + '\n;' + exportBlock, page.context);
}

/* ---------------- 图库页 ---------------- */

const sleep = ms => new Promise(r => setTimeout(r, ms));

function galleryPage() {
  const page = makePage(join(ROOT, 'web', 'gallery.html'), '/gallery');
  const toasts = [];
  run(page, 'globalThis.__g = { pRowEl, pPatchName, pQueueName, pSaveMap: pSaves, pDropSave,'
    + ' fillFolderSelect,'
    + ' setP: d => { P = d; }, loadPending, paintPending, setPending, pFlushNames,'
    + ' SEL, setSelMode, toggleSel, paintSel, renderGrid, makeCard,'
    + ' readSel, load, batchArchive, batchDelete,'
    + ' setTree: d => { TREE = d; }, setS: d => { Object.assign(S, d); },'
    + ' paintTree, toggleFold, subtreeIds, get foldShut() { return FOLD_SHUT; },'
    + ' get S() { return S; } };');
  page.context.toast = msg => toasts.push(String(msg));   // 函数声明挂 globalThis，能直接换掉
  page.context.ask = async () => true;                    // 弹窗一律点"确定"
  return { ...page, g: page.context.__g, toasts };
}

function pendingWith(items) {
  return {
    items,
    total_size: items.reduce((s, i) => s + (i.size || 0), 0),
    ok_count: items.filter(i => i.status === 'ok').length,
    failed_count: items.filter(i => i.status !== 'ok').length,
  };
}

async function galleryChecks() {
  console.log('图库页');
  const p = galleryPage();
  const g = p.g;
  // 页面启动时自己会 reload() + loadPending()，那些响应的回调是微任务 ——
  // 不等它们落地，下面刚摆好的状态会被"服务端的空列表"冲掉
  await sleep(20);

  // ---- I1：失败的行必须还能改名（暂存成功但这次没进库的行，服务端支持重试）----
  g.setP(pendingWith([]));
  const failedRow = g.pRowEl({
    id: 7, name: 'N'.repeat(50), original_filename: 'IMG_0007.jpg', size: 1024,
    status: 'failed', reason: '名字不能超过 100 个字', import_source: 'upload', conflict: null,
  });
  const failedInp = failedRow.querySelector('input.pname');
  check('I1 失败行的名字输入框没被禁用', failedInp && failedInp.disabled === false,
        `disabled=${failedInp && failedInp.disabled}`);
  failedInp.value = 'A1';
  failedInp.dispatch('input');
  check('I1 失败行打完字会排队保存（改得动才有重试）', g.pSaveMap.has(7));
  g.pDropSave(7);        // 把刚排的那个定时器真清掉，别让它稍后发出去干扰后面的检查

  // ---- M6：失败行的提示语是中性的「没成功」，不是「没读出来」----
  check('M6 失败行写的是「没成功：」', failedRow.textContent.includes('没成功：'),
        failedRow.textContent);
  check('M6 失败行不再写「没读出来：」', !failedRow.textContent.includes('没读出来：'));

  // ---- M2：PATCH 回来后回填输入框，但用户正在打字时不许顶掉 ----
  const it = {
    id: 11, name: '旧名', original_filename: 'IMG_0011.jpg', size: 2048,
    status: 'ok', reason: '', import_source: 'upload', conflict: null,
  };
  g.setP(pendingWith([it]));
  g.paintPending();                    // 行要在真的列表里 —— pPatchName 是从 DOM 里找它的
  const row = p.doc.getElementById('pRows').querySelector('.prow[data-pid="11"]');
  const inp = row.querySelector('input.pname');
  p.doc.activeElement = null;                  // 没在打字
  inp.value = '服务端上的旧名';                 // 模拟"PATCH 在途时重画把框回填成了旧值"
  p.state.reply = () => ({ body: { id: 11, name: '新名', conflict: null } });
  await g.pPatchName(11, '新名');
  check('M2 没在打字时，输入框跟到服务端确认的名字上', inp.value === '新名', `现在的值是「${inp.value}」`);

  p.doc.activeElement = inp;                   // 用户正把光标放在这一行打字
  inp.value = '用户正打着';
  await g.pPatchName(11, '另一个名');
  check('M2 正在打字时不回填（光标不被顶掉）', inp.value === '用户正打着', `现在的值是「${inp.value}」`);
  p.doc.activeElement = null;

  // ---- M4（界面那一半）：失败行改名成功之后，上次那句红字要撤掉 ----
  const fail = {
    id: 13, name: 'N'.repeat(50), original_filename: 'IMG_0013.jpg', size: 512,
    status: 'failed', reason: '名字不能超过 100 个字', import_source: 'upload', conflict: null,
  };
  g.setP(pendingWith([fail]));
  g.paintPending();
  const fRow = p.doc.getElementById('pRows').querySelector('.prow[data-pid="13"]');
  const fInp = fRow.querySelector('input.pname');
  check('M4 失败行原本挂着红字', fRow.textContent.includes('名字不能超过 100 个字'));
  p.state.reply = () => ({ body: { ...fail, name: 'A1', status: 'ok', reason: '', conflict: null } });
  fInp.value = 'A1';
  await g.pPatchName(13, 'A1');
  check('M4 改名成功后红字撤掉了', !fRow.textContent.includes('名字不能超过 100 个字'),
        fRow.textContent);
  check('M4 撤红字没有把输入框也换掉（光标还在原来的框里）',
        fRow.querySelector('input.pname') === fInp && fInp.value === 'A1');
  p.state.reply = null;

  // ---- M9：改名成功后整表重画，旧状态不许被画回来 ----
  // 「收起」再点「去命名」展开就是一次整表重画（paintPending 照着 P.items 重画每一行），
  // 而 PATCH 只更新了内存里那一项的 name/conflict 的话，重画就把改名前的
  // status='failed' / 那句红字又画回来 —— 用户看到的是"改好了，一收起又坏了"，
  // 顺带冲突提示也一起没了（失败行不画冲突框）。
  const fail2 = {
    id: 17, name: 'N'.repeat(50), original_filename: 'IMG_0017.jpg', size: 256,
    status: 'failed', reason: '名字不能超过 100 个字', import_source: 'upload', conflict: null,
  };
  const other = {
    id: 18, name: '乙', original_filename: 'IMG_0018.jpg', size: 256,
    status: 'ok', reason: '', import_source: 'upload', conflict: null,
  };
  g.setP(pendingWith([fail2, other]));       // 两行：证明重画动的是整张表
  g.paintPending();
  const r17a = p.doc.getElementById('pRows').querySelector('.prow[data-pid="17"]');
  check('M9 改名之前这一行挂着红字', r17a.textContent.includes('名字不能超过 100 个字'),
        r17a.textContent);
  const conflict = { image_id: 5, name: 'A1', line_count: 2, suggest: 'A1 (2)' };
  p.state.reply = () => ({ body: { ...fail2, name: 'A1', status: 'ok', reason: '', conflict } });
  await g.pPatchName(17, 'A1');
  g.paintPending();                          // 「收起」→「去命名」展开
  const r17b = p.doc.getElementById('pRows').querySelector('.prow[data-pid="17"]');
  check('M9 重画之后红字没有回来',
        !r17b.textContent.includes('名字不能超过 100 个字'), r17b.textContent);
  check('M9 重画之后冲突提示还在（那一行不再是失败行）',
        r17b.textContent.includes('图库里已经有一张叫「A1」的照片'), r17b.textContent);
  check('M9 重画只动了该动的行（旁边那行没被连累）',
        r17b.textContent.includes('IMG_0017.jpg')
        && p.doc.getElementById('pRows').querySelector('.prow[data-pid="18"]').textContent
             .includes('IMG_0018.jpg'));
  p.state.reply = null;

  // ---- I3：确认之前先重新拉一次列表（列表是全局一份，手机可能刚改过）----
  const owItem = {
    id: 21, name: '甲', original_filename: 'a.jpg', size: 100, status: 'ok', reason: '',
    import_source: 'upload',
    conflict: { image_id: 99, name: '甲', line_count: 3, suggest: '甲 (2)' },
    _ow: true,
  };
  g.setP(pendingWith([owItem]));
  g.paintPending();
  p.calls.length = 0;
  let pendingQ = p.win.dispatch('pageshow', { persisted: true });
  await Promise.all(pendingQ);
  check('I3 pageshow（从测量页返回）会重新拉一次待确认列表',
        p.calls.some(c => c.method === 'GET' && c.url === '/api/imports'),
        JSON.stringify(p.calls));

  p.calls.length = 0;
  p.state.reply = (url, method) => {
    if (url === '/api/imports' && method === 'GET') {
      // 手机上刚把这一行改成了别的名字 → 服务端现在给的是这一份
      return { body: pendingWith([{ ...owItem, name: '甲', conflict: null }]) };
    }
    if (url === '/api/imports/confirm') {
      return { body: { imported: 1, skipped: 0, unnamed: 0, overwritten: ['甲', '乙'] } };
    }
    return null;
  };
  p.toasts.length = 0;
  await Promise.all(p.doc.getElementById('pConfirm').dispatch('click'));
  const firstGet = p.calls.findIndex(c => c.method === 'GET' && c.url === '/api/imports');
  const thePost = p.calls.findIndex(c => c.method === 'POST' && c.url === '/api/imports/confirm');
  check('I3 点「确认导入」时先拉一次列表再算覆盖',
        firstGet >= 0 && thePost >= 0 && firstGet < thePost, JSON.stringify(p.calls));
  check('I3 结果里交代了覆盖掉的是哪几张',
        p.toasts.some(t => t.includes('已覆盖') && t.includes('甲') && t.includes('乙')),
        JSON.stringify(p.toasts));

  // ---- I2（前端）：确认期间按钮禁用 + 换文案，不许再点第二下 ----
  g.setP(pendingWith([{ ...owItem, conflict: null, _ow: false }]));
  p.calls.length = 0;
  let answer;                                  // 确认请求挂在半路上：模拟网络慢
  const gate = new Promise(r => { answer = r; });
  p.state.reply = url => (url === '/api/imports/confirm' ? gate : null);
  const btn = p.doc.getElementById('pConfirm');
  const inflight = btn.dispatch('click');
  check('I2 点下去立刻禁用「确认导入」按钮', btn.disabled === true);
  check('I2 点下去立刻换成看得懂的文案', btn.textContent.includes('正在导入'),
        `现在的文案是「${btn.textContent}」`);
  answer({ body: { imported: 1, skipped: 0, unnamed: 0, overwritten: [] } });
  await Promise.all(inflight);
  check('I2 回来之后按钮恢复可点', btn.disabled === false && btn.textContent.includes('确认导入'));
  p.state.reply = null;

  // ---- M8：列表空着也能点「确认导入」→ 得有一句话 ----
  g.setP(pendingWith([]));
  p.toasts.length = 0;
  await Promise.all(btn.dispatch('click'));
  check('M8 没什么可导入时有一句提示', p.toasts.some(t => t.includes('没什么要导入的')),
        JSON.stringify(p.toasts));

  // ---- M7：取消之前把排队中的改名清掉（否则它们回头 404，盖掉「已取消」）----
  const row2 = { id: 31, name: '乙', original_filename: 'b.jpg', size: 100, status: 'ok',
                 reason: '', import_source: 'upload', conflict: null };
  g.setP(pendingWith([row2]));
  g.paintPending();
  g.pQueueName(31, '乙改');
  p.calls.length = 0;
  p.toasts.length = 0;
  p.state.reply = url => (url === '/api/imports'
    ? { body: { cleared: 1 } }
    : null);
  await Promise.all(p.doc.getElementById('pCancel').dispatch('click'));
  check('M7 取消时把排队中的改名清掉了', g.pSaveMap.size === 0, `还剩 ${g.pSaveMap.size} 个`);
  await sleep(400);
  check('M7 取消之后没有迟到的 PATCH 去撞 404',
        !p.calls.some(c => c.method === 'PATCH'), JSON.stringify(p.calls));
  p.state.reply = null;

  // ---- F5：待确认区每行有自己的文件夹下拉，停在服务端给的值上 ----
  // 整批那个下拉（#pFolder）只是把每行的值一起改掉，真正的落点存在行上 ——
  // 它要是没画出来或者没停在服务端给的值上，用户看到的就是"选了没反应"。
  g.setP({ items: [{ id: 31, name: 'A1-1', original_filename: 'a.jpg', size: 100,
                     status: 'ok', reason: '', import_source: 'upload', conflict: null,
                     folder_id: 2, tags: [] }],
           folders: [{ id: 2, name: '部分2', parent_id: null, depth: 0 }],
           total_size: 100, ok_count: 1, failed_count: 0 });
  g.paintPending();
  const row31 = p.doc.getElementById('pRows').querySelector('.prow[data-pid="31"]');
  const s31 = row31 && row31.querySelector('select.pf');
  check('F5 图库待确认行有文件夹下拉', !!s31);
  check('F5 图库待确认行下拉停在 2 上', s31 && s31.value === '2', `value=${s31 && s31.value}`);
}

/* ---------------- 手机页 ---------------- */

async function mobileChecks(file) {
  console.log('手机页');
  const page = makePage(file, '/m/tok123');
  // `get P()` 而不是 `P: P` —— P 是 let 绑定的，直接存下来会钉住旧对象
  run(page, 'globalThis.__m = { pendRow, loadPend, paintPend, pAdopt, prows,'
    + ' fillFolderSelect,'
    + ' pSaveMap: pSaves, get P() { return P; }, setP: d => { P = d; }, tail };');
  const m = page.context.__m;
  await sleep(20);        // 同上：等页面启动时那次 loadPend() 先落地

  // ---- I1：失败的行也要能改名 ----
  const row = m.pendRow({ id: 3, name: 'IMG_3', original_filename: 'IMG_3.jpg', size: 100,
                          status: 'failed', reason: '名字不能超过 100 个字',
                          import_source: 'mobile', conflict: null });
  const inp = row.querySelector('input');
  check('I1 手机页失败行的输入框没被禁用', inp && inp.disabled === false,
        `disabled=${inp && inp.disabled}`);
  inp.value = 'A1';
  inp.dispatch('input');
  check('I1 手机页失败行打完字会排队保存', m.pSaveMap.has(3));
  m.pSaveMap.clear();

  // ---- M6：失败行同样写「没成功」----
  check('M6 手机页失败行写的是「没成功：」', row.textContent.includes('没成功：'), row.textContent);

  // ---- M4（界面那一半）：改名成功之后红字要撤掉 ----
  page.state.reply = () => ({ body: { id: 3, name: 'A1', status: 'ok', reason: '', conflict: null } });
  inp.value = 'A1';
  inp.dispatch('input');
  await sleep(500);
  check('M4 手机页改名成功后红字撤掉了', !row.textContent.includes('名字不能超过 100 个字'),
        row.textContent);
  page.state.reply = null;

  // ---- I4：改名被服务端拒绝（非法字符 400 / 行没了 404）不能静默吞掉 ----
  const row2 = m.pendRow({ id: 5, name: 'A1', original_filename: 'IMG_5.jpg', size: 100,
                           status: 'ok', reason: '', import_source: 'mobile', conflict: null });
  const inp2 = row2.querySelector('input');
  page.state.reply = url => (String(url).endsWith('/imports/5')
    ? { ok: false, status: 400, body: { detail: '名字里不能有 \\ / : * ? " < > | 这些符号' } }
    : null);
  inp2.value = 'A1/B2';
  inp2.dispatch('input');
  await sleep(500);                       // 改名是 400ms 防抖，等它真的发出去
  check('I4 手机页改名被拒绝时把原因说出来了',
        row2.textContent.includes('名字里不能有'), row2.textContent || '(这一行下面什么都没有)');
  check('I4 手机页不是默不作声地按旧名字入库',
        page.calls.some(c => c.method === 'PATCH' && String(c.url).endsWith('/imports/5')),
        JSON.stringify(page.calls));
  page.state.reply = null;

  // ---- I2（前端）：确认期间按钮禁用 + 换文案 ----
  m.setP(pendingWith([{ id: 6, name: 'A', original_filename: 'a.jpg', size: 100,
                        status: 'ok', reason: '', import_source: 'mobile', conflict: null }]));
  let answer;
  const gate = new Promise(r => { answer = r; });
  page.state.reply = url => (String(url).endsWith('/confirm') ? gate : null);
  const btn = page.doc.getElementById('pok');
  const inflight = btn.dispatch('click');
  check('I2 手机页点下去立刻禁用「确认导入」按钮', btn.disabled === true);
  check('I2 手机页点下去立刻换成看得懂的文案', btn.textContent.includes('正在导入'),
        `现在的文案是「${btn.textContent}」`);
  answer({ body: { imported: 1, skipped: 0, unnamed: 0, overwritten: [] } });
  await Promise.all(inflight);
  check('I2 手机页回来之后按钮恢复可点', btn.disabled === false && btn.textContent.includes('确认导入'),
        `disabled=${btn.disabled} 文案=「${btn.textContent}」`);

  // ---- I6：每行 ✕ 的触屏目标要够大（误触＝这一行的暂存文件没了）----
  const rm = row2.querySelector('.rm');
  const pad = getPad(page);
  check('I6 ✕ 的触屏目标大了（内边距 ≥ 10px）', pad.top >= 10 && pad.side >= 10, JSON.stringify(pad));

  // ---- F1：文件夹下拉 ----
  // ⚠️ FOLDERS 是模块级变量，paintPend() 才会填它。这里直接塞进 P 再重画，
  //    走的就是线上那条路（列表回来 → 填树 → 画两层的下拉）。
  m.setP(pendingWith([{ id: 20, name: 'A1-1', original_filename: 'a.jpg', size: 10,
                        status: 'ok', reason: '', import_source: 'mobile',
                        conflict: null, folder_id: 3, tags: [] }]));
  m.P.folders = [
    { id: 1, name: '样品1', parent_id: null, depth: 0 },
    { id: 2, name: '部分1', parent_id: 1, depth: 1 },
    { id: 3, name: '部分2', parent_id: 1, depth: 1 },
  ];
  m.paintPend();

  const sel = page.doc.createElement('select');
  m.fillFolderSelect(sel, 3);
  const opts = () => sel.childNodes.filter(n => n.tagName === 'OPTION');
  check('F1 下拉第一项是「不放进文件夹」', opts()[0].textContent === '不放进文件夹',
        opts()[0] && opts()[0].textContent);
  check('F1 整棵树都列出来了（3 个文件夹 + 1 个不放进）', opts().length === 4,
        String(opts().length));
  check('F1 子文件夹带缩进', opts()[2].textContent.startsWith(' '),
        JSON.stringify(opts()[2].textContent));
  check('F1 传进来的值被选中', sel.value === '3', `value=${sel.value}`);

  const fRow = m.pendRow({ id: 21, name: 'A1-1', original_filename: 'a.jpg', size: 10,
                           status: 'ok', reason: '', import_source: 'mobile',
                           conflict: null, folder_id: 2, tags: [] });
  // 这个位置以前是「这张放哪个文件夹」的下拉 —— 和上面「这批照片放到」是同一件事，
  // 用户 2026-09-23 要求换成让他自己填晶体板编号的空白框（整批那个说了算）。
  const fno = fRow.querySelector('input.pno');
  check('F2 每行有编号框', !!fno);
  check('F2 编号框默认是空的，等用户自己填', fno ? fno.value === '' : false,
        `value=${fno && fno.value}`);
  check('F2 每行不再有文件夹下拉（整批那个说了算）',
        !fRow.querySelector('select.pf'));

  // ---- F3：单张也进待确认列表 ----
  // 真的触发一次上传（文件选择框的 change），再看它发出去的 URL。
  // 沙箱里有 FormData 的替身（见 makePage），所以这条路能跑通。
  const fin = page.doc.getElementById('f');
  fin.files = [{ name: 'x.jpg' }];
  fin.dispatch('change');
  await sleep(50);
  check('F3 上传时恒为 pending（单张也停下来选）',
        page.calls.some(c => c.method === 'POST' && String(c.url).includes('mode=pending')),
        JSON.stringify(page.calls.map(c => c.url)));

  // ---- F4：文件夹默认值来自地址栏（二维码里带过来的）----
  const withFolder = makePage(file, '/m/tok123', '?folder=3');
  run(withFolder, 'globalThis.__m2 = { DEFAULT_FOLDER };');
  check('F4 地址里的 folder=3 被读成默认文件夹',
        withFolder.context.__m2.DEFAULT_FOLDER === 3,
        String(withFolder.context.__m2.DEFAULT_FOLDER));

  // 地址被人手改坏了（或者从旧二维码进来）不能白屏 —— 当没有就好
  const badFolder = makePage(file, '/m/tok123', '?folder=abc');
  run(badFolder, 'globalThis.__m3 = { DEFAULT_FOLDER };');
  check('F4 地址里的 folder 不是数字时当没有',
        badFolder.context.__m3.DEFAULT_FOLDER === null,
        String(badFolder.context.__m3.DEFAULT_FOLDER));

  // ---- G：每行第二行的三个孔位下拉（2026-09-23 加的）----
  // 手机传完就能直接选孔位，不用先跑到电脑上图库打标签。
  const row7 = m.pendRow({ id: 7, name: 'IMG_7', original_filename: 'IMG_7.jpg',
                           size: 100, status: 'ok', reason: '',
                           import_source: 'mobile', conflict: null,
                           folder_id: null, tags: ['B5-1'] });
  const bot = row7.querySelector('.pbot');
  const sels = bot ? bot.querySelectorAll('select.tg') : [];
  check('G1 每行第二行有编号框 + 三个孔位下拉', !!bot && sels.length === 3,
        `pbot=${!!bot} 孔位下拉=${sels.length}`);
  check('G2 已有的孔位拆回三个下拉',
        sels.length === 3 && sels.map(s => s.value).join('/') === 'B/5/孔1',
        sels.map(s => s.value).join('/'));

  const sent = c => (c && c.body ? JSON.parse(c.body) : null);
  page.calls.length = 0;
  sels[1].value = '9';
  await Promise.all(sels[1].dispatch('change'));
  const g3 = sent(page.calls.find(c => c.method === 'PATCH'));
  check('G3 改一个下拉就发 PATCH tags', !!g3 && String(g3.tags) === 'B9-1',
        JSON.stringify(g3));

  page.calls.length = 0;
  sels[1].value = '';                        // 全选「—」= 不标
  await Promise.all(sels[1].dispatch('change'));
  const g4 = sent(page.calls.find(c => c.method === 'PATCH'));
  check('G4 全选「—」发的是空数组（不是干脆不发这个字段）',
        !!g4 && Array.isArray(g4.tags) && g4.tags.length === 0, JSON.stringify(g4));

  // 这几个处理函数都会让服务端回整份列表、整表重画 —— 重画会把别人行里
  // 还堵在 400ms 定时器里的字顶回旧值。所以每个前面都得先冲一次。
  // 现在是 4 处：孔位下拉、删除这一行、整批文件夹、确认导入。
  // （行里那个编号框和名字框一样是 400ms 防抖 —— 它是**被冲**的一方，不算在内。）
  const flushes = (page.html.match(/await pFlushNames\(\)/g) || []).length;
  check('G5 会让整表重画的地方都先冲名字（孔位/删除/整批/确认）',
        flushes >= 4, `${flushes} 处`);

  // ---- G6~G8：编号框 + 孔位 = 名字 ----
  // 编号是一整块板共用的，孔位是每张各不相同的；两个拼起来才是「编号-行-列-孔」，
  // 和图库打标签对话框同一个规则。用户 2026-09-23 要求那一行改成填编号的空白框。
  // 此刻 G4 刚把孔位清空了，所以正好先测"只有编号"这一半。
  const no7 = bot.querySelector('input.pno');
  check('G6 每行第二行有编号框，默认空的', !!no7 && no7.value === '',
        no7 ? `value=${no7.value}` : '没找到');

  page.calls.length = 0;
  no7.value = '20260923-29';
  await Promise.all(no7.dispatch('input'));
  await sleep(450);                        // 编号框是 400ms 防抖
  const g6 = sent(page.calls.find(c => c.method === 'PATCH'));
  check('G6 只有编号、没选孔位时，名字就是编号本身',
        !!g6 && g6.name === '20260923-29', JSON.stringify(g6));

  page.calls.length = 0;
  sels[1].value = '7';                     // 选上孔位 → 名字该长全
  await Promise.all(sels[1].dispatch('change'));
  const g7 = sent(page.calls.find(c => c.method === 'PATCH'));
  check('G7 编号 + 新选的孔位一起拼进名字',
        !!g7 && g7.name === '20260923-29-B-7-1' && String(g7.tags) === 'B7-1',
        JSON.stringify(g7));

  // ⚠️ 编号留空时改孔位**绝不能**发 name：后端收到空串会真把名字清掉
  // （那是"到确认时回退到原始文件名"的意思），等于把用户起好的名字抹了。
  page.calls.length = 0;
  no7.value = '';
  sels[1].value = '3';
  await Promise.all(sels[1].dispatch('change'));
  const g8 = sent(page.calls.find(c => c.method === 'PATCH'));
  check('G8 编号留空时改孔位不发 name（发了会把名字清掉）',
        !!g8 && g8.name === undefined && String(g8.tags) === 'B3-1',
        JSON.stringify(g8));

  // ---- G9：整表重画之后，框里的编号不能没 ----
  // 服务端每次返回的都是全新的 item，编号只是前端临时填的 —— 不搬过去就没了，
  // 而它还得跟孔位一起拼名字（用户会看到"我填的编号自己消失了"）。
  m.setP({ items: [{ id: 7, name: 'x', tags: [], status: 'ok', _no: '20260923-29' }] });
  m.pAdopt({
    items: [{ id: 7, name: '20260923-29', tags: [], status: 'ok', folder_id: null }],
    folders: [],
  });
  const kept = m.prows.querySelector('input.pno');
  check('G9 整表重画后编号还在框里',
        !!kept && kept.value === '20260923-29', kept ? `value=${kept.value}` : '没找到');
}

// 从手机页的内联 CSS 里读 .prow .rm 的 padding。手指点得中与否没法在
// 这个假 DOM 里量，只能验证"内边距够大"—— 真机上的手感由用户人工确认。
function getPad(page) {
  const rule = /\.prow \.rm\s*\{([^}]*)\}/.exec(page.html);
  const m = /padding:\s*([\d.]+)px\s+([\d.]+)px/.exec(rule ? rule[1] : '');
  return m ? { top: Number(m[1]), side: Number(m[2]) } : { top: 0, side: 0 };
}

/* ---------------- 图库页：多选模式 ---------------- */

async function selModeChecks() {
  console.log('图库页 —— 多选模式');
  const p = galleryPage();
  const g = p.g;
  await sleep(20);

  const el = id => p.doc.getElementById(id);

  // ---- 进入 / 退出 ----
  g.setSelMode(true);
  check('S1 进入选择模式：body 上有 selmode', p.doc.body.classList.contains('selmode'));
  check('S1 操作栏出现了', el('selBar').hidden === false);
  g.setSelMode(false);
  check('S1 退出：selmode 去掉、操作栏收起',
        !p.doc.body.classList.contains('selmode') && el('selBar').hidden === true);

  // ---- 空选择时两个动作禁着（不禁用会弹出「删掉 0 张照片」的确认框）----
  g.setSelMode(true);
  g.paintSel();
  check('S2 一张没勾时「归档」「删除」禁用',
        el('selArchive').disabled === true && el('selDelete').disabled === true);

  // ---- 点卡片 = 勾选，不跳去测量页 ----
  // 卡片要真的挂在 grid 里：toggleSel 是从 grid 里按 data-id 找卡片来描边的
  g.setS({ folder: '', total: 9 });
  const card = g.makeCard({ id: 12, name: '甲', import_time: 0, line_count: 0, folders: [] });
  el('grid').append(card);
  const a = card.querySelector('a');
  let jumped = false;
  a.dispatch('click', { type: 'click', target: a, preventDefault: () => { jumped = true; } });
  check('S3 选择模式下点卡片是勾选，不跳走', jumped === true && g.SEL.ids.has(12));
  check('S3 卡片描上了边', card.classList.contains('on'));
  check('S3 「已选」跟着变', el('selCount').textContent === '已选 1 张',
        el('selCount').textContent);

  a.dispatch('click', { type: 'click', target: a, preventDefault: () => {} });
  check('S3 再点一下取消', !g.SEL.ids.has(12) && !card.classList.contains('on'));

  // ---- 不在选择模式时点卡片照样跳走 ----
  g.setSelMode(false);
  const card2 = g.makeCard({ id: 13, name: '乙', import_time: 0, line_count: 0, folders: [] });
  const a2 = card2.querySelector('a');
  let jumped2 = false;
  a2.dispatch('click', { type: 'click', target: a2, preventDefault: () => { jumped2 = true; } });
  check('S4 不在选择模式时点卡片不拦（照常跳去测量页）', jumped2 === false);

  // ---- 卡片链接带着当前文件夹范围 ----
  g.setS({ folder: '3', total: 9 });
  const card3 = g.makeCard({ id: 14, name: '丙', import_time: 0, line_count: 0, folders: [] });
  check('S5 在「实验A」里点卡片，链接带上 folder=3',
        card3.querySelector('a').href === '/?id=14&folder=3',
        card3.querySelector('a').href);
  g.setS({ folder: '' });
  const card4 = g.makeCard({ id: 15, name: '丁', import_time: 0, line_count: 0, folders: [] });
  check('S5 在「全部照片」里点卡片，链接不带 folder',
        card4.querySelector('a').href === '/?id=15', card4.querySelector('a').href);

  // ---- 全选本页 → 才长出「选中全部 N 张」----
  g.setSelMode(true);
  g.setS({ folder: '', total: 9 });      // 全库 9 张，本页只会放 3 张
  g.SEL.ids.clear();
  g.renderGrid([{ id: 1, name: 'A', import_time: 0, line_count: 0, folders: [] },
                { id: 2, name: 'B', import_time: 0, line_count: 0, folders: [] },
                { id: 3, name: 'C', import_time: 0, line_count: 0, folders: [] }]);
  g.paintSel();
  check('S6 没全勾时没有「选中全部」', el('selAll').hidden === true);
  el('selPage').dispatch('click');
  check('S6 全选本页把当前页都勾上',
        [1, 2, 3].every(id => g.SEL.ids.has(id)) && g.SEL.ids.size === 3,
        `勾了 ${g.SEL.ids.size} 张`);
  check('S6 这时才出现「选中全部 9 张」',
        el('selAll').hidden === false && el('selAll').textContent === '选中全部 9 张',
        el('selAll').textContent);
  check('S6 两个动作放开了', el('selArchive').disabled === false);

  // ---- 全勾上之后「选中全部」自己收起来 ----
  g.setS({ total: 3 });
  g.paintSel();
  check('S6 全库都勾上了，「选中全部」就收起来', el('selAll').hidden === true);

  // ---- 刷新后勾还在（localStorage）----
  // ⚠️ 页面哪天不写 jltx.sel 了，getItem 给的是 null —— 直接 JSON.parse(null)
  // 会一路抛到顶层，把后面 S8~S13 和亮度曲线全带崩，还没有具名 FAIL。
  // 兜一层空对象：坏掉时报 FAIL，不报崩溃。
  check('S7 选择模式存进了 localStorage',
        (JSON.parse(p.context.localStorage.getItem('jltx.sel') || 'null') || {}).on === true);

  // ---- 退出时清空 ----
  g.setSelMode(false);
  check('S8 退出选择模式会清空已勾的', g.SEL.ids.size === 0);

  // ---- S9 拖拽监听必须挂在 document 上 ----
  // 挂在某个区域上的话，拖偏一点松手，浏览器会**直接导航到那个文件**，
  // 当前的筛选、页码、滚动位置全丢 —— 这条只靠肉眼读代码守不住。
  check('S9 拖拽的 dragenter 挂在 document 上',
        (p.doc._listeners.dragenter || []).length === 1);

  // ---- S10 光标在输入框里时，粘贴不抢 ----
  const q = el('q');
  q.tagName = 'INPUT';               // 沙箱里 getElementById 造出来的是 div，手动改成输入框
  p.doc.activeElement = q;
  let swallowed = false;
  const callsBefore = p.calls.length;
  p.win.dispatch('paste', {
    clipboardData: { items: [{ kind: 'file', type: 'image/png',
                               getAsFile: () => ({ name: '粘贴.png' }) }] },
    preventDefault() { swallowed = true; },
  });
  await sleep(10);                   // 真抢的话会发一个上传请求出来
  check('S10 光标在输入框里时，粘贴既不抢也不拦',
        swallowed === false && p.calls.length === callsBefore);
  p.doc.activeElement = null;

  // ---- S11 刷新后：选择模式的**界面**真的画出来了，不是只有内部状态 ----
  // 这里钉的是一个真发生过的缺陷：启动时只调了 readSel() 没调 paintSel()，
  // 于是 SEL.on 是 true、卡片点击也被拦了，但 body 上没有 selmode、操作栏还 hidden
  // —— 用户视角是「刷新之后图片点不开了」，屏幕上却没有一个字解释为什么。
  // 光靠「点一下按钮」的检查抓不到它，必须走一遍「从 localStorage 恢复」这条路。
  p.context.localStorage.setItem('jltx.sel', JSON.stringify({ on: true, ids: [12] }));
  p.context.readSel();
  // 只让 /api/images 这个请求回答有数据 —— 别的（比如文件夹树）照旧走默认桩，
  // 否则会把树也喂成照片列表
  p.state.reply = url => String(url).startsWith('/api/images?')
    ? { body: { total: 1, items: [
        { id: 12, name: '甲', import_time: 0, line_count: 0, folders: [] }] } }
    : null;
  await p.context.load();            // load() 是「格子画完了」的那个点，它必须收尾画选择态
  check('S11 刷新后操作栏和 selmode 都画出来了',
        p.doc.body.classList.contains('selmode') && el('selBar').hidden === false);
  check('S11 刷新后「已选」是读回来的那个数',
        el('selCount').textContent === '已选 1 张', el('selCount').textContent);
  const back = el('grid').querySelector('.card[data-id="12"]');
  check('S11 刷新后那张已勾的卡片还带着勾（数字类型不能变成字符串）',
        !!back && back.classList.contains('on'));
  p.state.reply = null;

  // ---- S12 批量归档：默认停在「也放进」（安全的那边）----
  // 默认档位是安全设计：用户不看就点保存，最坏也只是"没改分类"，不会把归档弄丢
  let archBody = null;
  p.context.ask = async (title, hint, content) => { archBody = content; return null; };
  g.setTree({ items: [{ id: 3, name: '实验A', depth: 0 }], total: 1, uncategorized: 0 });
  g.SEL.ids.add(12);
  await p.context.batchArchive();
  const ins = archBody ? archBody.querySelectorAll('input') : [];
  check('S12 归档弹窗默认停在「也放进」',
        ins.length >= 2 && ins[0].value === 'add' && ins[0].checked === true
        && ins[1].value === 'replace' && ins[1].checked === false,
        ins.map(i => `${i.value}:${i.checked}`).join(' '));
  g.SEL.ids.clear();

  // ---- S12b 「也放进」+ 一个文件夹都不勾 = 一张都没动，界面不许谎报 ----
  // 服务端此时算的是「原有 ∪ ∅ = 原有」，回 updated=0。界面却说的是别的：
  // 提示语说「不勾就是未分类」、结果 toast 报「已归档 N 张」。两条都钉住。
  // （同一处的第三句话只在文件夹树为空时才出现，那个场景这里不摆。）
  let archHint = '';
  p.context.ask = async (title, hint) => { archHint = hint; return null; };
  g.setTree({ items: [{ id: 3, name: '实验A', depth: 0 }], total: 1, uncategorized: 0 });
  g.SEL.ids.add(12);
  await p.context.batchArchive();
  check('S12b 默认档位的提示语不再承诺「未分类」', !String(archHint).includes('未分类'),
        archHint);
  g.SEL.ids.clear();

  // 再走一遍，这回真点弹窗上那颗「保存」：档位是默认的「也放进」，一个文件夹都没勾。
  // ⚠️ 必须调页面自己那个 pick()。桩直接 return 一个 {folder_ids: []} 的话，
  // 这条检查就退化成「桩返回什么就断言什么」—— 永远绿，什么都没钉住。
  p.calls.length = 0;
  p.toasts.length = 0;
  p.context.ask = async (title, hint, content, buttons) => buttons[1].pick();   // 真「保存」
  p.state.reply = url => (String(url).includes('batch-folders') ? { body: { updated: 0 } } : null);
  g.setTree({ items: [{ id: 3, name: '实验A', depth: 0 }], total: 1, uncategorized: 0 });
  g.SEL.ids.add(12);
  await p.context.batchArchive();
  const archCall = p.calls.find(c => String(c.url).includes('batch-folders'));
  const sent = archCall ? JSON.parse(archCall.body) : null;
  check('S12b 没勾文件夹时发出去的 folder_ids 是空数组（没勾的不许被带上）',
        !!sent && Array.isArray(sent.folder_ids) && sent.folder_ids.length === 0
        && sent.mode === 'add',
        archCall ? archCall.body : '根本没发出请求');
  check('S12b 一张都没动时不报「已归档 N 张」',
        p.toasts.some(t => t.includes('原来的归档没变'))
        && !p.toasts.some(t => t.includes('已归档')),
        JSON.stringify(p.toasts));
  p.state.reply = null;
  g.SEL.ids.clear();

  // ---- S13 批量删除：确认框里的张数是**服务端**给的，不是前端自己数的 ----
  // 这条钉的是整个设计里最要紧的那个决定：跨页全选时前端手里只有当前页那几行，
  // 所以「会删几张、其中几张量过线」必须问服务端（dry_run），不能在前端算。
  // 前端手里只有 1 张、服务端说 3 张 —— 框里就必须是 3 张，写成 1 张就是退化了。
  let askTitle = '';
  p.context.ask = async title => { askTitle = title; return null; };
  p.state.reply = url => String(url).includes('batch-delete')
    ? { body: { count: 3, measured: 1 } } : null;
  g.SEL.ids.add(12);
  await p.context.batchDelete();
  check('S13 删除确认框用的是服务端算的张数',
        askTitle === '删掉 3 张照片' && g.SEL.ids.size === 1, askTitle);
  p.state.reply = null;
  g.SEL.ids.clear();
}

/* ---------------- 测量页：亮度曲线 ---------------- */

// measure.html 整页在这个假 DOM 里跑不起来（它一开机就要 2D 画布），
// 但决定"亮度调完长什么样"的 toneLut 是个纯函数，从源码里整段抠出来单独跑就够。
// 抠的是文件里的真源码 —— 哪天有人把它改回"乘一个倍数"，这里立刻红。
function pickFunc(src, name) {
  const at = src.indexOf(`function ${name}(`);
  if (at < 0) throw new Error(`没找到 function ${name}()`);
  let depth = 0;
  for (let j = src.indexOf('{', at); j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(at, j + 1);
  }
  throw new Error(`function ${name}() 的花括号不配对`);
}

function toneChecks() {
  console.log('测量页 · 亮度曲线');
  const html = readFileSync(join(ROOT, 'web', 'measure.html'), 'utf8');
  const code = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)]
    .map(m => m[1]).join('\n');
  const ctx = vm.createContext({});
  vm.runInContext(pickFunc(code, 'toneLut'), ctx);
  const toneLut = ctx.toneLut;
  if (typeof toneLut !== 'function') throw new Error('toneLut 跑起来不是函数');

  // —— 亮度 = 1.00 必须是原样。bakeTone 在 b===1 时直接不烘、拿原图去画，
  //    曲线本身若在 1 处改了哪怕一个数，这两条路就对不上了。
  const one = toneLut(1);
  let same = true, bad = '';
  for (let i = 0; i < 256; i++) if (one[i] !== i) { same = false; bad = `L[${i}]=${one[i]}`; }
  check('B1 亮度=1.00 时曲线是原样（改 0 个像素）', same, bad);

  const up = toneLut(2);
  check('B2 提亮时黑还是黑、白还是白', up[0] === 0 && up[255] === 255, `L[0]=${up[0]} L[255]=${up[255]}`);

  // —— 用户要的"均匀一点儿变亮"就是这一条：暗处涨幅 ≥ 亮处涨幅。
  //    旧的 brightness(2) 恰好相反（30→60 涨 30，220→255 只涨 35 但已经顶死）。
  check('B3 提亮时暗的地方涨得不比亮的地方少',
        (up[30] - 30) >= (up[220] - 220), `30 涨 ${up[30] - 30}，220 涨 ${up[220] - 220}`);

  // —— 两头锚死，所以怎么拖都不会顶到 255 糊成一片，也不会把黑压成灰。
  //    这条正是换掉 CSS brightness() 的理由。
  check('B4 拖到最大也不会有地方糊成死白', up[220] < 255, `L[220]=${up[220]}（brightness(2) 这里会顶到 255）`);

  // —— 整张偏暗的照片（平均灰度 40）最需要被救。
  //    旧的 brightness(2) 只能把它抬到 80；同样的倍率下伽马曲线能抬到 100 以上。
  check('B5 最暗的照片也救得回来：40 灰在 b=2 时升到 100 以上',
        up[40] >= 100, `L[40]=${up[40]}（brightness(2) 只到 80）`);

  const dn = toneLut(0.5);
  check('B6 压暗时中间调确实变暗，两头仍然不动',
        dn[128] < 128 && dn[0] === 0 && dn[255] === 255, `L[128]=${dn[128]}`);

  // —— 曲线不许回头：回头就意味着画面里某一段的亮暗被颠倒了。
  let mono = true, at = '';
  for (const b of [0.5, 0.7, 1, 1.5, 2]) {
    const L = toneLut(b);
    for (let i = 1; i < 256; i++) {
      if (L[i] < L[i - 1]) { mono = false; at ||= `b=${b} 在灰度 ${i} 处掉头`; }
    }
  }
  check('B7 滑块两端之间任何倍率都不掉头', mono, at);

  // —— 下面三条是**源码级防退化断言**，不是行为检查：这个假 DOM 跑不起测量页整页
  //    （它一开机就要 2D 画布），只能照 pickFunc / getPad 那两条先例，在源码文本上
  //    钉住最容易回归的三点。它们只证明「这几句还在」，证明不了「跑起来对」。
  check('R1 地址栏带着文件夹范围（`folder=${S.scope}` 还在）',
        code.includes('folder=${S.scope}'));
  check('R2 下拉框上限还是 1000（一次拉齐，不靠翻页）',
        code.includes("limit: '1000'"));
  check('R3 只有真选了范围才给请求加 folder 参数',
        /if\s*\(\s*S\.scope\s*!==\s*''\s*\)\s*u\.set\('folder'/.test(code));

  // —— 裁剪那三条同理：这个假 DOM 跑不起测量页整页，只能在源码文本上
  //    钉住最容易回归的三点。它们只证明「这几句还在」，证明不了「跑起来对」。
  //    ⚠️ R4 的谓词按当前源码调过（文案一字未动）：brief 写的是
  //    `drawImage(photoSrc(), r.x0, r.y0, ...)`，而 7c44ec2 把这里改成了
  //    「源矩形换算到源图像素空间」（kx/ky），照抄会当场红。守的没变：
  //    drawImage 得带着源矩形画，否则裁剪不出来。
  check('R4 画图时用了源矩形（裁剪靠它生效）',
        /const src = photoSrc\(\);/.test(code)
        && /ctx\.drawImage\(src,\s*r\.x0 \* kx,\s*r\.y0 \* ky,/.test(code));
  check('R5 调裁剪框时显示的是整张图（viewRegion 里认 cropMode）',
        /function viewRegion\(\)\s*\{\s*if \(cropMode \|\| !S\.crop\)/.test(code));
  check('R6 裁剪不再进 localStorage（它进库了）',
        !/crop: S\.crop, cropChoice/.test(code) && !code.includes('let cropChoice'));
  // —— R7 是 Task 5 那条 Important（框拖到照片外 → 画布空白且刷新也回不来）
  //    唯一的仓库内保护：修复只活在源码里，实施者的探针在临时目录、会随系统清理消失。
  check('R7 点「裁好了」之前把框夹回照片内（越界框不会进库、也不会把画面弄空白）',
        /function clampToPhoto\(/.test(code) && /const box = clampToPhoto\(S\.crop\)/.test(code)
        && /S\.crop = box;/.test(code) && /JSON\.stringify\(box\)/.test(code));

  // —— 图上那行字（用户 2026-09-23：先是反馈"看不清"调大了字号，
  //    然后要一个能同时管长度数字和标尺字的滑块）——
  //    守的是**只有一份字号**：导出不是另画一套，而是把屏幕上这套绘制用 uz()
  //    原样放大重跑。哪天有人给导出单开一个字号，两边就又对不上了（修过四轮）。
  //    真正的行为验证在 tests/web_geom_check.mjs（那边能真的跑一遍导出）。
  const fontSets = code.match(/ctx\.font\s*=\s*[^;]+/g) || [];
  check('C1 每一处 ctx.font 都来自 labelFont()（没有第二份字号）',
        fontSets.length > 0 && fontSets.every(x => /labelFont\(\)/.test(x)),
        fontSets.join(' | '));
  check('C1b 字号只在声明和 applyFontPx 里出现（导出不会偷偷改它）',
        (code.match(/FONT_PX\s*=[^=]/g) || []).length === 2,
        String((code.match(/FONT_PX\s*=[^=]/g) || []).length));

  // —— 「图上字号」滑块：管的是长度数字 + 标尺字，独立存、关照片也不清 ——
  check('C4 导出面板里有「图上字号」滑块，接在 applyFontPx 上',
        /id="exFont"/.test(html)
        && /\$\('exFont'\)\.addEventListener\('input'/.test(code)
        && /function applyFontPx\(/.test(code));
  // 那两个勾选框是用户点名要撤掉的；重新加回来就得同时把渲染代码也加回去，
  // 否则勾了没反应（这次撤的时候把画编号/备注那两段一起删了）。
  check('C5 已撤掉的「显示测量编号 / 显示备注文字」没有半途回来',
        !/exSeq|exNotes/.test(html) && !/exSeq|exNotes/.test(code));

  // —— 「关掉这张」：只把照片从屏幕上卸下来 ——
  //    这几条只证明"按钮还在、还接着那个函数"，证明不了跑起来对
  //    （整页在假 DOM 里跑不起来，理由见上面 toneLut 那段）。
  // ⚠️ 按钮的 id 在 HTML 里、不在 <script> 里 —— 两条得分别对着 html 和 code 查
  check('C2 侧栏的「关掉这张」还接在 closePhoto 上',
        /id="btnClose"/.test(html)
        && /\$\('btnClose'\)\.addEventListener\('click',/.test(code)
        && /function closePhoto\(\)/.test(code));
  // 删除的收尾必须和它共用一份 —— 各写一遍的话，往 S 里加字段时总有一边会漏
  check('C3 删除照片走的是同一个 closePhoto',
        /closePhoto\(\);\s*\n\s*await loadPhotos\(\);/.test(code));
}

/* ---------------- 测量页：下拉框里写什么 ---------------- */

// 和 toneLut 同一套路：整页跑不起来，就把纯函数从源码里抠出来单独跑。
function labelChecks() {
  console.log('测量页 · 下拉框文案');
  const html = readFileSync(join(ROOT, 'web', 'measure.html'), 'utf8');
  const code = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)]
    .map(m => m[1]).join('\n');
  const ctx = vm.createContext({});
  vm.runInContext(pickFunc(code, 'photoLabel'), ctx);
  const f = ctx.photoLabel;
  if (typeof f !== 'function') throw new Error('photoLabel 跑起来不是函数');

  // FOLDER_NAMES 只有叶子名（loadFolderNames 装的），所以 nameOf 就是它
  const nameOf = id => ({ 2: '部分2' }[id] || '');
  const label = (o) => f(o, nameOf);

  check('N1 有文件夹有标签 → 部分2-A1-1',
        label({ name: 'A1-1', folders: [2], tags: ['A1-1'], line_count: 0 }) === '部分2-A1-1');
  check('N2 只有文件夹 → 部分2-名字',
        label({ name: 'IMG_1', folders: [2], tags: [], line_count: 0 }) === '部分2-IMG_1');
  check('N3 只有标签 → A1-1',
        label({ name: '随便', folders: [], tags: ['A1-1'], line_count: 0 }) === 'A1-1');
  check('N4 都没有 → 就是名字',
        label({ name: '随便', folders: [], tags: [], line_count: 0 }) === '随便');
  check('N5 量过的带尾巴',
        label({ name: 'A1-1', folders: [2], tags: ['A1-1'], line_count: 3 })
          === '部分2-A1-1（已量 3 条）');
  // 文件夹被删了 / FOLDER_NAMES 还没加载回来 —— 不能显示成 "undefined-名字"
  check('N6 文件夹名字查不到 → 退回名字',
        label({ name: 'IMG_1', folders: [99], tags: [], line_count: 0 }) === 'IMG_1');
  check('N7 一张图在好几个文件夹里时只取第一个',
        label({ name: 'x', folders: [2, 7], tags: [], line_count: 0 }) === '部分2-x');
  check('N8 老数据没有 folders/tags 字段也不炸',
        label({ name: 'x', line_count: 0 }) === 'x');
}

/* ---------------- 图库页：文件夹树的展开 / 收起 ---------------- */

// 用户原话：「文件夹左边可以给我一个收起和展开子文件夹的功能，对每个层级有效，
// 点一个只展开当前下一级的文件夹，不是所有子文件夹」。这几条就钉这句话。
async function treeChecks() {
  console.log('图库页 · 文件夹树展开收起');
  const p = galleryPage();
  const g = p.g;
  await sleep(20);

  const nodes = () => [...p.doc.getElementById('treeList').querySelectorAll('.tnode')];
  const labels = () => nodes().map(b => b.querySelector('.tname').textContent);
  const byName = n => nodes().find(b => b.querySelector('.tname').textContent === n);
  const caret = n => byName(n).querySelector('.tcaret');
  // 假 DOM 不会冒泡，也没有真事件对象；补一个空的 stopPropagation 让它跑得下去
  const click = n => caret(n).dispatch('click', { stopPropagation() {} });
  const setTree = items => {
    g.setTree({ items, total: 9, uncategorized: 1 });
    g.foldShut.clear();
    g.paintTree();
  };

  // 乙(2) 套 丙(3) 套 戊(5)；丁(4) 在最外层且没有子文件夹
  setTree([
    { id: 2, name: '乙', parent_id: null, depth: 0, count: 5 },
    { id: 3, name: '丙', parent_id: 2, depth: 1, count: 2 },
    { id: 5, name: '戊', parent_id: 3, depth: 2, count: 1 },
    { id: 4, name: '丁', parent_id: null, depth: 0, count: 1 },
  ]);
  check('T1 默认全展开（和以前一样，不会一上来就把看惯的列表变样）',
        labels().join() === '全部照片,未分类,乙,丙,戊,丁', labels().join());

  check('T2 有子文件夹的才有三角，没有的留空位（不然同层的名字会左右错开）',
        caret('乙').textContent === '▾' && caret('丁').textContent === '',
        `乙=${caret('乙').textContent} 丁=${caret('丁').textContent}`);

  click('乙');
  check('T3 收起乙 → 它下面两级一起看不见，别的行照常在',
        labels().join() === '全部照片,未分类,乙,丁', labels().join());
  check('T3 收起后三角朝右', caret('乙').textContent === '▸', caret('乙').textContent);

  click('乙');
  check('T4 再展开 → 只回到下一级（丙），孙子戊还藏着',
        labels().join() === '全部照片,未分类,乙,丙,丁', labels().join());
  check('T4 收起状态存进了 localStorage（⭐ 刷新保持现状）',
        String(p.context.localStorage.getItem('jltx.foldShut')).includes('5'),
        String(p.context.localStorage.getItem('jltx.foldShut')));

  click('丙');
  check('T5 再点丙 → 到第三层', labels().join() === '全部照片,未分类,乙,丙,戊,丁',
        labels().join());

  // ⚠️ 这条是最容易做错的：只把「收起」标记清掉的话，刚展开过的丙会跟着冒出来，
  // 看着还是一下摊开整棵树 —— 用户明确说了不要那样。
  click('乙');          // 收起
  click('乙');          // 再展开
  check('T6 里面开过的情况下重开乙 → 仍然只展开下一级',
        labels().join() === '全部照片,未分类,乙,丙,丁', labels().join());

  // 点三角只收展开开，不能顺手把这一行选成当前文件夹
  g.setS({ folder: '' });
  click('乙');
  check('T7 点三角不会把那一行选成当前文件夹', g.S.folder === '', String(g.S.folder));
}

/* ---------------- 图库打标签：「编号」+ 孔位 → 名字 ---------------- */

// 打标签对话框那段 JS 在这个假 DOM 里跑不起来（要弹窗、要缩略图），
// 但「编号 + 孔位拼成什么名字」是个纯函数，照 pickFunc 那条先例抠出来单独跑就够。
// 抠的是 gallery.html 里的真源码 —— 规则一改这里立刻红。
function plateChecks() {
  console.log('图库 · 编号拼名字');
  const html = readFileSync(join(ROOT, 'web', 'gallery.html'), 'utf8');
  const code = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)]
    .map(m => m[1]).join('\n');
  const ctx = vm.createContext({});
  vm.runInContext(pickFunc(code, 'nameWithPlate'), ctx);
  const np = ctx.nameWithPlate;

  // 用户给的例子 + 结尾那个孔号：编号 + '-' + 行 + '-' + 列 + '-' + 孔号
  check('P1 编号 + 孔位拼成 20260923-29-B-5-1',
        np('20260923-29', 'B5-1') === '20260923-29-B-5-1', np('20260923-29', 'B5-1'));
  // 结尾那个孔号是 2026-09-23 用户要求补的：不写的话同一个池的两个孔
  // 会拼出一模一样的名字，在列表里分不出谁是谁。
  check('P2 同一个池的两个孔拼出两个不同的名字',
        np('P1', 'B5-1') === 'P1-B-5-1' && np('P1', 'B5-2') === 'P1-B-5-2');
  check('P3 两位数的列号照样拼（A10-1）', np('X', 'A10-1') === 'X-A-10-1');
  // 三个下拉没选全时 tag 是空串：只剩编号本身。用户填了编号就照他填的来，
  // 不能一声不吭丢掉 —— 他会以为名字已经改好了。
  check('P4 孔位没选全时名字只剩编号', np('P1', '') === 'P1');
  // 「编号留空 = 不改名字」是在 collect 里分叉的，这条钉住那个分叉
  check('P5 编号留空时发的是空串（服务端一个字都不改）',
        /name:\s*no\s*\?\s*nameWithPlate\(no,\s*t\)\s*:\s*''/.test(code));
  check('P6 行里有编号框，整批那个一改覆盖所有行',
        /className = 'tagNo'/.test(code) && /className = 'tagBatch'/.test(code)
        && /for \(const r of rows\) r\._no\.value = bno\.value/.test(code));
  // 整批框必须在滚动区外面：塞进 .tagList 的话列表一长它就跟着滚没了
  check('P7 整批框在滚动区外面',
        /wrap\.append\(bb, list\)/.test(code) && /list\.append\(row\)/.test(code)
        && !/wrap\.className = 'tagList'/.test(code));
}

/* ---------------- 跑 ---------------- */

await galleryChecks();
await selModeChecks();
await treeChecks();
toneChecks();
labelChecks();
plateChecks();

const mobileFile = process.argv[2];
if (mobileFile) await mobileChecks(mobileFile);
else console.log('（没给手机页文件，跳过手机页检查）');

console.log(`\n${passed} 项通过，${failed.length} 项失败`);
if (failed.length) { for (const f of failed) console.error('  - ' + f); process.exit(1); }
