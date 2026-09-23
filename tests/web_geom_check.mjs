// 页面脚本的几何自检：用桩把 measure.html 里的 JS 跑起来，验导出时的坐标换算。
//
// **不需要浏览器**，跑法：  node tests/web_geom_check.mjs
// 它只验算术（画布尺寸、视口变换、放大倍数、导出后画布有没有恢复原状），
// **不验画面好不好看** —— 那是用户自己上手看的事。
//
// 为什么需要它：导出的图现在是「把画布上的绘制代码原样放大重跑」，
// 所以这套换算一旦错了，导出的整张图就全错，而且浏览器里不一定看得出来。
import fs from 'node:fs';
import path from 'node:path';

const html = fs.readFileSync(
  'D:/vs_claude_test/晶体大小测量/web/measure.html', 'utf8');
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];

const calls = [];
function makeCtx(tag) {
  const rec = (name, ...a) => calls.push([tag, name, ...a]);
  const noop = name => (...a) => rec(name, ...a);
  return {
    save: noop('save'), restore: noop('restore'),
    setTransform: noop('setTransform'), translate: noop('translate'),
    scale: noop('scale'), clearRect: noop('clearRect'),
    drawImage: noop('drawImage'), beginPath: noop('beginPath'),
    moveTo: noop('moveTo'), lineTo: noop('lineTo'), arc: noop('arc'),
    rect: noop('rect'), fill: noop('fill'), stroke: noop('stroke'),
    fillRect: noop('fillRect'), strokeRect: noop('strokeRect'),
    setLineDash: noop('setLineDash'), fillText: noop('fillText'),
    strokeText: noop('strokeText'), ellipse: noop('ellipse'),
    measureText: t => ({ width: String(t).length * 7 }),
    _props: {}, set font(v) { rec('font', v); }, get font() { return ''; },
    set lineWidth(v) { rec('lineWidth', v); }, get lineWidth() { return 1; },
    set strokeStyle(v) { rec('strokeStyle', v); }, get strokeStyle() { return ''; },
    set fillStyle(v) { rec('fillStyle', v); }, get fillStyle() { return ''; },
    set lineCap(v) { rec('lineCap', v); }, get lineCap() { return ''; },
    set lineJoin(v) { rec('lineJoin', v); }, get lineJoin() { return ''; },
    set textAlign(v) { rec('textAlign', v); }, get textAlign() { return ''; },
    set textBaseline(v) { rec('textBaseline', v); }, get textBaseline() { return ''; },
  };
}

const els = {};
function el(id) {
  if (!els[id]) {
    els[id] = {
      id, value: '', checked: false, hidden: false, disabled: false,
      textContent: '', innerHTML: '', title: '', dataset: {},
      style: {}, options: [], width: 0, height: 0,
      addEventListener() {}, append() {}, remove() {},
      setAttribute() {}, getContext: () => makeCtx('off'),
      getBoundingClientRect: () => ({ left: 0, top: 0, width: 940, height: 854 }),
    };
  }
  return els[id];
}
globalThis.document = {
  getElementById: el,
  createElement: () => el('tmp' + Math.random()),
  body: { append() {}, classList: { add() {}, remove() {} } },
  activeElement: null,
  addEventListener() {},          // 拖拽监听挂在 document 上
};
globalThis.localStorage = {     // 刷新回到原样用的
  _v: {},
  getItem(k) { return this._v[k] ?? null; },
  setItem(k, v) { this._v[k] = String(v); },
  removeItem(k) { delete this._v[k]; },
};
globalThis.window = { addEventListener() {}, devicePixelRatio: 1 };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => 'Consolas' });
globalThis.Image = class { set src(v) {} };
globalThis.URL = { createObjectURL: () => 'blob:x', revokeObjectURL() {} };
globalThis.fetch = async () => ({ ok: true, json: async () => ({}), headers: { get: () => null } });

const factory = new Function(js + `
  return { S, render, renderExport, exportRegion, scaleBarInfo, scaleBarGeom,
           uz: n => n * UIZ, labelScreen, focusCropForLine,
           setFontPx: n => { FONT_PX = n; },
           setCW: (w, h) => { CW = w; CH = h; SW = w; SH = h; },
           fitView, fitFullScale, fitScaleFor, exportSup };
`);
const app = factory();

// 造一张 960×1280 的图，模拟用户开到某个缩放
app.S.imageId = 1; app.S.name = '样品2_1'; app.S.W = 960; app.S.H = 1280;
app.S.circle = { cx: 480, cy: 640, a: 200, b: 200, th: 0 };
app.S.umpp = 7.768;
app.S.lines = [{ id: 1, seq: 1, x1: 400, y1: 600, x2: 460, y2: 620,
                 measured_um: 1000, color: '#FF2D55', note: '', label_dx: 0, label_dy: 0 }];
app.S.view = { s: 0.5, tx: 100, ty: 50 };
// 导出图里的字号/线宽按"**整张照片**铺满屏幕"那个比例算，不按当前缩放、也不按裁剪区
// —— 见下面「导出的大小不跟着缩放变」那几段。它由 fitFullScale(CW, CH) 当场算出来。
// 屏幕画布设成 940×668：整张照片 960×1280 铺满它正好是 0.5，
// 于是下面断言里的数字（64px / 120px / 4 倍线宽…）还是原来那些。
app.setCW(940, 668);

let fails = 0;
const check = (name, ok, detail = '') => {
  console.log((ok ? '  OK   ' : '  FAIL ') + name + (detail ? '   ' + detail : ''));
  if (!ok) fails++;
};

// ---------- 导出：区域 → 离屏像素的换算 ----------
const region = { x0: 300, y0: 400, x1: 700, y1: 700 };   // 原图坐标
for (const sup of [1, 2, 4]) {
  calls.length = 0;
  const before = JSON.stringify(app.S.view);
  const cv = app.renderExport(region, sup);
  const w = Math.round((region.x1 - region.x0) * sup);
  const h = Math.round((region.y1 - region.y0) * sup);
  check(`${sup}× 离屏画布尺寸 = 框选区域 × 倍率`,
        cv.width === w && cv.height === h, `${cv.width}×${cv.height} 期望 ${w}×${h}`);

  // 清屏之后第一次 translate/scale 就是图像的视口变换
  const tr = calls.find(c => c[1] === 'translate');
  const sc = calls.find(c => c[1] === 'scale');
  check(`${sup}× 图像平移量 = -框选左上角 × 倍率`,
        tr && Math.abs(tr[2] - (-region.x0 * sup)) < 1e-6
          && Math.abs(tr[3] - (-region.y0 * sup)) < 1e-6,
        tr ? `(${tr[2]}, ${tr[3]}) 期望 (${-region.x0 * sup}, ${-region.y0 * sup})` : '没找到');
  check(`${sup}× 图像缩放 = 倍率`, sc && Math.abs(sc[2] - sup) < 1e-6);

  // 屏幕上 1px 在导出图里应该占 sup / 当前缩放 个像素
  const wantUiz = sup / 0.5;
  const lineWidths = calls.filter(c => c[0] === 'off' && c[1] === 'lineWidth').map(c => c[2]);
  check(`${sup}× 线宽按 ${wantUiz} 倍放大`,
        lineWidths.includes(3 * wantUiz),
        `画过的线宽 ${[...new Set(lineWidths)].join(',')}`);
  check(`${sup}× 用完之后画布恢复原状`, JSON.stringify(app.S.view) === before,
        app.S.view.s + ' vs ' + JSON.parse(before).s);
}

// ---------- 导出区域的选择 ----------
app.S.crop = { x0: 10, y0: 20, x1: 300, y1: 400 };
check('选了手动框选就用框选的区域',
      JSON.stringify(app.exportRegion()) === JSON.stringify(app.S.crop));

// ---------- 标尺长度：页面和导出必须是同一个物理值 ----------
const pageInfo = app.scaleBarInfo();
const pageTarget = app.S.W * 0.175 * app.S.umpp;
check('页面挑标尺用的目标长度 = 照片宽 × 0.175 × 比例尺',
      true, `${pageTarget.toFixed(0)} μm → 选中 ${pageInfo.um} μm`);

// ---------- 聚焦某条线 ----------
const fc = app.focusCropForLine(app.S.lines[0]);
check('聚焦裁剪框包含整条线且有边距',
      fc.x0 <= 400 && fc.x1 >= 460 && fc.y0 <= 600 && fc.y1 >= 620 &&
      fc.x1 - fc.x0 > 60);

// ---------- 字号：屏幕、导出、以及"缩放不该影响它" ----------
const REGION = { x0: 300, y0: 400, x1: 700, y1: 700 };
const drawn = (tag, prop) =>
  [...new Set(calls.filter(c => c[0] === tag && c[1] === prop).map(c => c[2]))];

// —— 屏幕上的字和线**跟着照片一起缩放**（它们是"照片的一部分"）——
// 用户 2026-09-23 的原话：「应该成为我图片的一部分」。放大照片时字也变大、
// 缩小也变小，字相对**照片**的大小恒定不变。
// ⚠️ 以前这里是反的（钉成固定屏幕像素，uz() 在屏幕上恒等于 1）—— 那样照片一缩小，
// 字相对照片就显得又大又糙，用户说"画质变差了"。别改回去。
{
  const px = [];
  for (const zoom of [0.25, 1, 5]) {
    app.S.view = { s: zoom, tx: 0, ty: 0 };
    calls.length = 0;
    app.render();
    const m = String(drawn('off', 'font')[0]).match(/([\d.]+)px/);
    px.push(m ? parseFloat(m[1]) : NaN);
  }
  // 基准 fitFullScale(940, 668) = 0.5 → UIZ = zoom/0.5 = 2·zoom → 字号 16×2·zoom
  check('屏幕上的字跟着照片一起缩放', px.join('/') === '8/32/160', px.join(' / '));
  check('字号和缩放严格成正比（字相对照片恒定）',
        Math.abs(px[0] / 0.25 - px[1]) < 1e-9 && Math.abs(px[1] - px[2] / 5) < 1e-9,
        px.join(' / '));
}

// —— 导出图 = 屏幕上那个样子的放大版（所见即所得）——
// 这是整套换算唯一的目的：屏幕上字相对照片多大，导出图里就得多大。
// 屏幕和导出共用 render() 里那一行 UIZ = S.view.s / fitFullScale()，
// 区别只是导出时 S.view.s 被换成了 sup。
{
  const WHOLE = { x0: 0, y0: 0, x1: 960, y1: 1280 };
  const zoom = 0.31;
  app.S.view = { s: zoom, tx: 0, ty: 0 };
  calls.length = 0; app.render();
  const onScreen = parseFloat(String(drawn('off', 'font')[0]).match(/([\d.]+)px/)[1]);
  calls.length = 0; app.renderExport(WHOLE, 2);
  const inExport = parseFloat(String(drawn('off', 'font')[0]).match(/([\d.]+)px/)[1]);
  // "字相对照片" = 字号 ÷ 照片在这张画布上占的宽度
  const a = onScreen / (960 * zoom), b = inExport / (960 * 2);
  check('导出和屏幕上看到的一样（字相对照片的比例一致）',
        Math.abs(a - b) < 1e-9, `${a} vs ${b}`);
}

// —— 导出图里的大小也**不随缩放变** ——
// 用户 2026-09-23 报的：放大着看晶体、调完线再导出，图上的字和线就变小了。
// 因为导出当时是按"屏幕上 1px 在导出图里占 sup/当前缩放 个像素"算的 ——
// 当前缩放一变，整张图的字号线宽全跟着变，同一张图导两次都不一样。
// 改成只认 S.fit（整张照片铺满屏幕那个比例）：跟你在哪个缩放上按的导出无关。
{
  const shots = [];
  for (const zoom of [0.25, 1, 5]) {
    app.S.view = { s: zoom, tx: 0, ty: 0 };
    calls.length = 0;
    app.renderExport(REGION, 2);
    // ⚠️ 只比**导出那一次**（排在最前）：renderExport 收尾会把屏幕重画一遍，
    // 而屏幕上的字现在是跟着缩放走的，混进来三次就一定不一样。
    shots.push(JSON.stringify({ f: drawn('off', 'font')[0],
                                lw: drawn('off', 'lineWidth')[0] }));
  }
  check('导出：缩放到哪一档，导出的字号和线宽都一样',
        shots.every(x => x === shots[0]),
        shots[0] === shots[1] && shots[1] === shots[2] ? '' : shots.join(' | '));
}

// —— 那个字号滑块真的同时管着长度数字和标尺字 ——
// 用户要的是"比例尺字和标注字体共用一个字号"。两处都走 labelFont()，
// 所以改 FONT_PX 两边一起变（这里比的是导出图，两个值都得在）。
{
  const at = n => {
    app.setFontPx(n);
    app.S.view = { s: 0.5, tx: 0, ty: 0 };
    calls.length = 0;
    app.renderExport(REGION, 2);
    return drawn('off', 'font');
  };
  const f16 = at(16), f30 = at(30);
  // ⚠️ 每次导出列表里都有**两个**值：导出那一次画的，加上 renderExport 收尾时
  //    把屏幕重画一遍用的那个（屏幕上是原始 FONT_PX，没有乘 UIZ）。导出在前。
  //    UIZ = 2 / 0.5 = 4 → 16px 的字在导出图里是 64px，30px 的是 120px。
  check('字号 16 → 导出图里的字是 64px（屏幕重画那次是 16px）',
        JSON.stringify(f16) === '["600 64px Consolas","600 16px Consolas"]', JSON.stringify(f16));
  check('字号 30 → 导出图里的字是 120px（屏幕重画那次是 30px）',
        JSON.stringify(f30) === '["600 120px Consolas","600 30px Consolas"]', JSON.stringify(f30));
  // 正好两种 = 导出那一次只用了**一个**字号（长度数字和标尺字同号）。
  // 两处各写各的话，这里会冒出第三种。
  check('长度数字和标尺字共用同一个字号',
        f16.length === 2 && f30.length === 2, `${f16.length} / ${f30.length} 种`);
  app.setFontPx(16);
}

// —— 标签的抬升量跟着字号走 ——
// 写死 20 的话，字号调到 30 时文字会压在线条上（还被深色描边吃掉半个字）。
{
  const lift = n => {
    app.setFontPx(n);
    app.S.view = { s: 1, tx: 0, ty: 0 };
    // 那条线的中点屏幕纵坐标是 (600+620)/2 = 610
    return 610 - app.labelScreen(app.S.lines[0])[1];
  };
  const l16 = lift(16), l32 = lift(32);
  check('字号翻倍，标签往上抬的距离也翻倍',
        Math.abs(l16 - 20) < 1e-6 && Math.abs(l32 - 40) < 1e-6, `${l16} / ${l32}`);
  app.setFontPx(16);
}

// —— 裁剪不能改变导出图里的字号 ——
// 用户 2026-09-23 报的：框一小块再导出，图上的字比整图导出时小一截，
// 而且框得越小、字越小。
// 根因：导出字号的分母是 S.fit，而 S.fit 是 fitView() 按 viewRegion() 算的；
// viewRegion() 在**有裁剪**时返回的是裁剪区 —— 框得越小，S.fit 越大，
// UIZ = sup / S.fit 越小，字就越小。它本该是"整张照片铺满屏幕"那个比例，
// 跟框了多大无关。
{
  app.setCW(940, 854);
  // 导出图里"字相对照片"有多大 = 字号 ÷ sup（照片在导出图里就是 ×sup）。
  // 两次用同一个 sup，所以比字号就行。
  const fontPxAt = crop => {
    app.S.crop = crop;
    app.fitView();                 // 框选前后页面都会重新 fit 一次
    app.S.view = { s: app.S.view.s, tx: 0, ty: 0 };
    calls.length = 0;
    app.renderExport(crop || { x0: 0, y0: 0, x1: app.S.W, y1: app.S.H }, 2);
    const m = String(drawn('off', 'font')[0]).match(/([\d.]+)px/);   // "600 64px Consolas"
    return m ? parseFloat(m[1]) : NaN;                              // 导出那一次排在前面
  };
  const whole = fontPxAt(null);
  const piece = fontPxAt({ x0: 400, y0: 560, x1: 560, y1: 720 });    // 160×160 一小块
  check('框一小块导出，字号跟整图导出一样',
        Math.abs(whole - piece) < 1e-6, `整图 ${whole}px vs 裁剪 ${piece}px`);
}

// —— 一次 fitView() 都没调过时，导出也不能跟着滚轮走 ——
// **这就是用户 2026-09-23 真正踩的那条路**：刷新页面回到上次那张照片时，
// openPhoto 走的是「恢复上次的缩放」那一支（v && v.s > 0 → 直接赋 S.view），
// **根本不调 fitView()** —— 于是"整张照片铺满屏幕"那个基准压根没被算过，
// 公式只好回退到当前缩放，导出的字号和线宽就跟着滚轮变了。
// 放大着看晶体再导出 → 字和线变小变细（用户原话："画质都变差了"）。
// 所以基准必须**现算**，不能存着用。
{
  const fresh = factory();          // 全新的页面：没打开过照片，更没调过 fitView()
  fresh.setCW(940, 854);
  fresh.S.imageId = 1; fresh.S.name = '样品2_1'; fresh.S.W = 960; fresh.S.H = 1280;
  fresh.S.circle = { cx: 480, cy: 640, a: 200, b: 200, th: 0 };
  fresh.S.umpp = 7.768;
  fresh.S.lines = [{ id: 1, seq: 1, x1: 400, y1: 600, x2: 460, y2: 620,
                     measured_um: 1000, color: '#FF2D55', note: '',
                     label_dx: 0, label_dy: 0 }];

  const shots = [];
  for (const zoom of [0.25, 1, 5]) {
    fresh.S.view = { s: zoom, tx: 0, ty: 0 };
    calls.length = 0;
    fresh.renderExport({ x0: 0, y0: 0, x1: 960, y1: 1280 }, 2);
    shots.push(String(drawn('off', 'font')[0]));
  }
  check('刷新恢复的那种（从没 fitView 过）：导出也不随缩放变',
        shots.every(x => x === shots[0]), shots.join(' | '));
}

// —— 一块区域铺满屏幕的比例：全项目只有 fitScaleFor() 算它 ——
// fitView()（定视图）、导出（字号基准）、恢复缩放（补 S.fit）三处都用它，
// 所以它对了三处都对；分开各写一份的话，早晚有一处跟另外两处对不上。
{
  const WHOLE = { x0: 0, y0: 0, x1: 960, y1: 1280 };
  app.setCW(940, 668);
  check('整张照片装进 940×668 = 0.5',
        Math.abs(app.fitScaleFor(WHOLE, 940, 668) - 0.5) < 1e-9,
        String(app.fitScaleFor(WHOLE, 940, 668)));
  check('框得越小，这个比例越大（裁剪区铺满屏幕要放得更大）',
        app.fitScaleFor({ x0: 400, y0: 560, x1: 560, y1: 720 }, 940, 668) > 0.5);
  check('画布尺寸或区域还没量出来时给 0，让调用方去回退',
        app.fitScaleFor(WHOLE, 0, 0) === 0 && app.fitScaleFor(WHOLE, 940, 0) === 0);
  check('导出基准永远按整张照片，跟框了多大无关',
        app.fitFullScale() === app.fitScaleFor(WHOLE, 940, 668));
}

// —— 恢复上次缩放那条支路，必须自己补上 S.fit ——
// 它不经过 fitView()，而缩放上下限的基准只有 fitView() 会设。少了它，
// 滚轮就没有范围限制（`S.view.s = S.fit ? 夹住 : 不夹`），用户滚几下能把照片甩丢。
// 这条支路要跑 openPhoto（异步，还得等图片 onload），假 DOM 里跑不通，
// 所以只能像 web_behavior_check 数 pFlushNames 那样钉住源码。
{
  const at = html.indexOf('const v = restore && restore.view');
  const seg = at < 0 ? '' : html.slice(at, at + 500);
  check('恢复缩放的支路补了 S.fit', /S\.fit\s*=\s*fitScaleFor\(/.test(seg),
        at < 0 ? '没找到那段代码' : seg.split('\n')[1].trim());
}

// —— 输出倍率：装不下就降档，别让用户吃红字 ——
// 默认 4× 是为了标注和照片都清楚（倍率越高，字和线占的像素越多），
// 但整张手机照片按 4× 是上亿像素，浏览器画布吃不下。
// 直接弹红字拒绝的话，用户只会觉得"这工具导不出图"—— 他其实只是想要一张清楚的图。
{
  const MAX = 40_000_000;                       // 和 measure.html 里那个上限一样
  const PHONE = { x0: 0, y0: 0, x1: 4000, y1: 3000 };   // 一张典型手机照片
  const s4 = app.exportSup(PHONE, 4);
  check('整张手机照片按 4× 装不下 → 自动降档', s4 > 1 && s4 < 4, `降到 ${s4}×`);
  check('降档之后确实装得下',
        PHONE.x1 * PHONE.y1 * s4 * s4 <= MAX,
        `${Math.round(PHONE.x1 * PHONE.y1 * s4 * s4 / 1e6)} 百万像素`);
  check('框选一小块时 4× 装得下，就不动用户选的倍率',
        app.exportSup({ x0: 0, y0: 0, x1: 600, y1: 400 }, 4) === 4);
  check('连 1× 都装不下就返回 0，让调用方去请用户框小一点',
        app.exportSup({ x0: 0, y0: 0, x1: 9000, y1: 9000 }, 4) === 0);
  check('用户本来选 1× 时，装得下就老老实实用 1×',
        app.exportSup(PHONE, 1) === 1);
}

console.log(fails ? `\n失败 ${fails} 项` : '\n全部通过');
process.exit(fails ? 1 : 0);
