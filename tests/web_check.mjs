// HTML 里内联 <script> 的语法检查。
//
// 为什么需要它：web/ 下的页面是**单个大 HTML 文件**，JS 内联在里面。
// 语法错了浏览器只会白屏，控制台里那句话用户看不懂、也不会去开控制台。
//
// 用法：node tests/web_check.mjs [文件…]
//   不带参数：查 web/ 下的全部 .html（默认）。
//   带参数：只查这几个路径 —— 手机页不是文件（它是 app/api/mobile.py 里的
//   Python 字符串 _PAGE），由 tests/test_mobile_page_js.py 落成临时文件再传进来。
import { readFileSync, readdirSync } from 'node:fs';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const WEB = join(dirname(fileURLToPath(import.meta.url)), '..', 'web');
const args = process.argv.slice(2);
const targets = args.length
  ? args.map(p => [basename(p), readFileSync(p, 'utf8')])
  : readdirSync(WEB).filter(n => n.endsWith('.html')).sort()
      .map(f => [f, readFileSync(join(WEB, f), 'utf8')]);
let bad = 0;

for (const [f, html] of targets) {
  const blocks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)];
  if (!blocks.length) {
    console.log(`${f}: 没有内联 script，跳过`);
    continue;
  }
  blocks.forEach((m, i) => {
    try {
      new vm.Script(m[1], { filename: `${f}#script${i + 1}` });   // 只编译，不执行
      console.log(`${f} script#${i + 1}: 语法 OK`);
    } catch (e) {
      bad++;
      console.error(`${f} script#${i + 1}: 语法错误 —— ${e.message}`);
    }
  });
}

process.exit(bad ? 1 : 0);
