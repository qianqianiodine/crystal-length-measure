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
  set innerHTML(v) { this._own = ''; this.childNodes = []; this._html = String(v); }
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

function makePage(htmlFile, pathname) {
  const calls = [];                       // 按顺序记下每次请求：{ url, method, body }
  const state = { pending: { items: [], prefix: '', total_size: 0, ok_count: 0, failed_count: 0 }, reply: null };

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
  const location = { pathname, search: '', href: '' };
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
      if (url.startsWith('/api/folders')) return mkRes({ body: { items: [], prefix: '' } });
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
    + ' setP: d => { P = d; }, loadPending, paintPending, setPending, pFlushNames,'
    + ' SEL, setSelMode, toggleSel, paintSel, renderGrid, makeCard,'
    + ' readSel, load, batchArchive, batchDelete,'
    + ' setTree: d => { TREE = d; }, setS: d => { Object.assign(S, d); } };');
  page.context.toast = msg => toasts.push(String(msg));   // 函数声明挂 globalThis，能直接换掉
  page.context.ask = async () => true;                    // 弹窗一律点"确定"
  return { ...page, g: page.context.__g, toasts };
}

function pendingWith(items, prefix = '') {
  return {
    items, prefix,
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
      return { body: pendingWith([{ ...owItem, name: '甲', conflict: null }], '') };
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
}

/* ---------------- 手机页 ---------------- */

async function mobileChecks(file) {
  console.log('手机页');
  const page = makePage(file, '/m/tok123');
  run(page, 'globalThis.__m = { pendRow, loadPend, paintPend, pSaveMap: pSaves,'
    + ' setP: d => { P = d; }, tail };');
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
}

/* ---------------- 跑 ---------------- */

await galleryChecks();
await selModeChecks();
toneChecks();
const mobileFile = process.argv[2];
if (mobileFile) await mobileChecks(mobileFile);
else console.log('（没给手机页文件，跳过手机页检查）');

console.log(`\n${passed} 项通过，${failed.length} 项失败`);
if (failed.length) { for (const f of failed) console.error('  - ' + f); process.exit(1); }
