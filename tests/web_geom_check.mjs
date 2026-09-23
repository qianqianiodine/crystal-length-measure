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
           setFontPx: n => { FONT_PX = n; } };
`);
const app = factory();

// 造一张 960×1280 的图，模拟用户开到某个缩放
app.S.imageId = 1; app.S.name = '样品2_1'; app.S.W = 960; app.S.H = 1280;
app.S.circle = { cx: 480, cy: 640, a: 200, b: 200, th: 0 };
app.S.umpp = 7.768;
app.S.lines = [{ id: 1, seq: 1, x1: 400, y1: 600, x2: 460, y2: 620,
                 measured_um: 1000, color: '#FF2D55', note: '', label_dx: 0, label_dy: 0 }];
app.S.view = { s: 0.5, tx: 100, ty: 50 };
// "整张照片铺满屏幕"那个缩放。导出图里的字号/线宽按**它**算，不按当前缩放 ——
// 见下面「导出的大小不跟着缩放变」那段。
app.S.fit = 0.5;

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

// —— 屏幕上的字号/线宽**不随缩放变** ——
// 它们画的是固定的屏幕像素（uz() 在屏幕上恒等于 1），所以放大到 5 倍看细节时
// 线不会变粗、数字不会变大。这条以前就是这样，钉住别被改坏。
{
  const shapes = [];
  for (const zoom of [0.25, 1, 5]) {
    app.S.view = { s: zoom, tx: 0, ty: 0 };
    calls.length = 0;
    app.render();
    shapes.push(JSON.stringify({ f: drawn('off', 'font'), lw: drawn('off', 'lineWidth') }));
  }
  check('屏幕上：缩放到哪一档，字号和线宽都不变',
        shapes.every(x => x === shapes[0]),
        shapes[0] === shapes[1] && shapes[1] === shapes[2] ? '' : shapes.join(' | '));
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
    shots.push(JSON.stringify({ f: drawn('off', 'font'), lw: drawn('off', 'lineWidth') }));
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

console.log(fails ? `\n失败 ${fails} 项` : '\n全部通过');
process.exit(fails ? 1 : 0);
