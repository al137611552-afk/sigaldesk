/* 面板视觉自检：把真实页面截成 PNG，好在没有浏览器的机器上"看见"它。
 *
 * 开发机没装桌面浏览器，但系统 Chrome 是在的，playwright 的 node 模块
 * 随 @playwright/mcp 一起装了 —— 两者拼起来就能无头截图。playwright 自带的
 * chromium 缓存版本对不上，所以必须显式指定 executablePath 用系统浏览器。
 * Windows/macOS 上会自动找 Chrome 或 Edge；找不到就用 SIGDESK_CHROME 指定。
 *
 * 用法（先起面板）：
 *   .venv/bin/python scripts/serve.py --port 8899 &
 *   node scripts/shoot_panel.mjs                       # 输出到 docs/design/shots/
 *   node scripts/shoot_panel.mjs http://127.0.0.1:8000 /tmp/out
 *
 * 控制台里的任何 error 都会在最后汇总打印 —— 白屏和资源 404 就是这么被发现的。
 */
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import { execSync } from 'node:child_process';

const require = createRequire(import.meta.url);
function loadPlaywright() {
  const roots = [];
  try { roots.push(execSync('npm root -g', { encoding: 'utf8' }).trim()); } catch { /* ignore */ }
  for (const r of roots) {
    for (const rel of ['playwright', '@playwright/mcp/node_modules/playwright']) {
      const p = path.join(r, rel);
      if (fs.existsSync(p)) return require(p);
    }
  }
  throw new Error('找不到 playwright 模块。装一个：npm i -g playwright（浏览器用系统 Chrome，无需 install）');
}

/* 系统 Chrome 的位置。playwright 自带的 chromium 缓存版本常常对不上，
   所以一律显式 executablePath 指系统浏览器 —— Linux / Windows / macOS 都要能找到。 */
function findChrome() {
  const env = process.env.SIGDESK_CHROME;
  if (env && fs.existsSync(env)) return env;
  const pf = process.env['PROGRAMFILES'] || 'C:\\Program Files';
  const pf86 = process.env['PROGRAMFILES(X86)'] || 'C:\\Program Files (x86)';
  const local = process.env['LOCALAPPDATA'] || '';
  const candidates = process.platform === 'win32'
    ? [
        path.join(pf, 'Google/Chrome/Application/chrome.exe'),
        path.join(pf86, 'Google/Chrome/Application/chrome.exe'),
        path.join(local, 'Google/Chrome/Application/chrome.exe'),
        path.join(pf, 'Microsoft/Edge/Application/msedge.exe'),
        path.join(pf86, 'Microsoft/Edge/Application/msedge.exe'),
      ]
    : process.platform === 'darwin'
      ? ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
         '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge']
      : ['/usr/bin/google-chrome', '/usr/bin/google-chrome-stable',
         '/usr/bin/chromium', '/usr/bin/chromium-browser'];
  return candidates.find((p) => fs.existsSync(p));
}

const CHROME = findChrome();
if (!CHROME) {
  throw new Error('找不到 Chrome/Edge。装一个，或用 SIGDESK_CHROME 指定可执行文件路径。');
}
console.log(`  浏览器: ${CHROME}`);

const URL_BASE = process.argv[2] || 'http://127.0.0.1:8899';
const OUT = process.argv[3] || 'docs/design/shots';
fs.mkdirSync(OUT, { recursive: true });

const { chromium } = loadPlaywright();
const errors = [];
const browser = await chromium.launch({ executablePath: CHROME,
  args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'] });
const ctx = await browser.newContext({ viewport: { width: 1600, height: 980 }, deviceScaleFactor: 2 });
const page = await ctx.newPage();
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
page.on('pageerror', (e) => errors.push('JS: ' + e.message));

/* 截图之外的一道视觉不变量：**可见的文案不能被别的元素盖住**。
   实测踩过 —— 空状态提示元素在、文字在、visibility 正常，
   却被 lightweight-charts 的 canvas（z-index:1/2）完全盖住，屏幕上一片漆黑。
   这种缺陷截图看得见但说不出原因，DOM 里又一切正常，只有命中测试能定位。 */
const covered = async () => page.evaluate(() => {
  const bad = [];
  for (const el of document.querySelectorAll('.chart-empty, .empty, .callout, .banner')) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || !el.textContent.trim()) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') continue;
    if (cs.pointerEvents === 'none') continue;  // 命中测试会穿透，测不了，跳过
    const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    if (top && !el.contains(top) && !top.contains(el)) {
      bad.push(`${el.className} 被 <${top.tagName.toLowerCase()}> 盖住: `
        + el.textContent.trim().slice(0, 30));
    }
  }
  return bad;
});

/* 第二道视觉不变量：**文字不能和背景一个色**。
   实测踩过 —— 基础表单样式 `select,input,button{color:var(--fg)}` 漏了 textarea，
   而表单控件不继承页面 color，于是规则编辑器是纯黑字配深色底（对比度 1.1:1）。
   截图本该抓到，却因为恰好截到了唯一显式设过颜色的 `:disabled` 态而漏掉。
   阈值 3:1 —— 只抓"基本看不见"，不去管有意做淡的次要文字（var(--dim) 有 7:1）。 */
const lowContrast = async () => page.evaluate(() => {
  const lum = (c) => {
    const [r, g, b] = c.map((v) => {
      const x = v / 255;
      return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const parse = (s) => (s.match(/[\d.]+/g) || []).slice(0, 4).map(Number);
  const bgOf = (el) => {
    for (let n = el; n; n = n.parentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c.length >= 3 && (c[3] === undefined || c[3] > 0)) return c;
    }
    return [0, 0, 0];
  };
  const bad = [];
  for (const el of document.querySelectorAll(
    'textarea, input, select, button, .band-cap, .hero .v, .empty, .callout')) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
    const hasText = (el.value || el.textContent || '').trim().length > 0;
    if (!hasText) continue;
    const fg = parse(cs.color).slice(0, 3);
    const bg = bgOf(el).slice(0, 3);
    if (fg.length < 3 || bg.length < 3) continue;
    const [a, b2] = [lum(fg), lum(bg)];
    const ratio = (Math.max(a, b2) + 0.05) / (Math.min(a, b2) + 0.05);
    if (ratio < 3) {
      bad.push(`<${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}> 对比度 `
        + `${ratio.toFixed(2)}:1（字 rgb(${fg}) 底 rgb(${bg})）`);
    }
  }
  return bad;
});

const shot = async (name, opts) => {
  const f = path.join(OUT, `${name}.png`);
  await page.screenshot({ path: f, ...opts });
  for (const c of await covered()) errors.push(`遮挡[${name}]: ${c}`);
  for (const c of await lowContrast()) errors.push(`低对比[${name}]: ${c}`);
  console.log(`  ${f}  ${(fs.statSync(f).size / 1024).toFixed(0)} KB`);
};

// 不能用 networkidle：同进程模式下 SSE 是长连接，网络永远"不空闲"。
await page.goto(URL_BASE, { waitUntil: 'load' });
await page.waitForFunction(() => !document.querySelector('#chart-note')?.textContent.trim()
  ? false : true, null, { timeout: 20000 }).catch(() => {});
await page.waitForTimeout(2000);
await shot('panel-1-信号与图表');

const sig = page.locator('#feed .sig').first();
if (await sig.count()) { await sig.click(); await page.waitForTimeout(1800); await shot('panel-2-选中信号'); }

for (const [tab, name] of [['质量统计', 'panel-3-质量统计'], ['纸上账户', 'panel-5-纸上账户'], ['运行健康', 'panel-4-运行健康']]) {
  const t = page.locator('.tab', { hasText: tab });
  if (await t.count()) { await t.click(); await page.waitForTimeout(2000); await shot(name, { fullPage: true }); }
}

// 日线：日线走交易日聚合（不是墙钟分桶），单独截一张确认真画得出来。
// 上面的循环停在「运行健康」页上，盯盘视图是 display:none，先切回去 —— 否则
// #c-symbol 存在但不可见，selectOption 会一直等到超时。
await page.locator('.tab', { hasText: '盯盘' }).click();
await page.waitForTimeout(1200);

const symSelPick = async (label) => {
  await page.locator('#c-symbol').selectOption({ label });
};

// 九宫格：同标的 9 个周期同屏 + 点一格放大。选一个数据最全的标的来截。
{
  // 挑数据最全的标的，否则日/周/月几格是空的，验证不到真实排布。
  const withBars = await page.evaluate(async () => {
    let best = null, most = -1;
    for (const o of document.querySelectorAll('#c-symbol option')) {
      const r = await fetch(`/api/bars?symbol=${encodeURIComponent(o.value)}&timeframe=1d&limit=1`);
      const n = r.ok ? (await r.json()).total : 0;
      if (n > most) { most = n; best = o.textContent; }
    }
    return best;
  });
  if (withBars) { await symSelPick(withBars); await page.waitForTimeout(1200); }
  await page.locator('#grid-toggle').click();
  await page.waitForTimeout(3500);
  // 网格默认落在**预警组**（九标的×一周期）。先拍它，再切到九周期模式 ——
  // 下面那几步（十字线同步、放大一格）要的是周期模式的格子。
  await shot('panel-15-预警组');
  const tabs = await page.$$('#wl-tabs .wl-tab');
  if (tabs.length > 1) {
    await page.click('#wl-tabs .wl-tab:nth-child(2)');
    await page.waitForTimeout(2500);
    await shot('panel-16-预警组-加密');
    await page.click('#wl-tabs .wl-tab:nth-child(1)');
    await page.waitForTimeout(2500);
  }
  // 切到九周期模式。**不切的话下面按 .cell 下标取格子会取到空槽**
  // （空槽没有 .cell-body），脚本会卡在 boundingBox 上超时 —— 已经踩过一次。
  await page.click('#grid-mode [data-mode="tf"]');
  await page.waitForTimeout(3500);
  await shot('panel-11-九宫格');
  // 十字线跨周期同步：悬停一格，九格显示同一时刻。静态图看不出"跟随"，
  // 所以 shift+单击锁定后再截 —— 锁定态下九条十字线都停在同一时刻。
  {
    const box = await page.locator('.cell').nth(4).locator('.cell-body').boundingBox();
    if (box) {
      await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.5);
      await page.waitForTimeout(600);
      await page.keyboard.down('Shift');
      await page.mouse.click(box.x + box.width * 0.6, box.y + box.height * 0.5);
      await page.keyboard.up('Shift');
      await page.waitForTimeout(900);
      await shot('panel-14-多周期十字线同步');
      await page.keyboard.press('Escape');
      await page.waitForTimeout(500);
    }
  }

  await page.locator('.cell').nth(6).dblclick();   // 双击放大第 7 格
  await page.waitForTimeout(1800);
  await shot('panel-12-九宫格放大');
  await page.locator('.cell.big').dblclick();        // 双击还原
  await page.waitForTimeout(1200);
  await page.locator('#grid-toggle').click();       // 切回单图
  await page.waitForTimeout(1200);

  // 「没人盯的标的」空状态：必须说清楚为什么空、以及怎么办
  const idle = await page.evaluate(() => {
    const o = [...document.querySelectorAll('#c-symbol option')]
      .find((x) => x.textContent.includes('未盯'));
    return o ? o.textContent : null;
  });
  if (idle) {
    await symSelPick(idle);
    await page.waitForTimeout(1800);
    await shot('panel-13-未盯标的');
    if (withBars) { await symSelPick(withBars); await page.waitForTimeout(1200); }
  }
}


// 信号提醒：弹窗是实时推来的，静态截图看不到 —— 直接调 pushToast 造几条出来看样子。
// 只验视觉（配色、层级、会不会盖住图表），行为由桩化冒烟覆盖。
await page.evaluate(() => {
  const mk = (sym, dir, px, rule) => ({
    rule_id: rule, symbol: sym, direction: dir, timeframe: "5m",
    fired_at: Math.floor(Date.now() / 1000), trigger_price: px, dedup_key: `${sym}:demo`,
  });
  pushToast(mk("CRYPTO.OKX.BTCUSDT.PERP", "long", 78012.4, "kan-da-zuo-xiao"));
  pushToast(mk("CN.SHFE.rb2610", "short", 3131, "volume-spike"));
  pushToast(mk("CRYPTO.OKX.ETHUSDT.PERP", "neutral", 2451.7, "breakout-long"));
});
await page.waitForTimeout(600);
await shot('panel-10-信号弹窗');


const symSel = page.locator('#c-symbol');
if (await symSel.count()) {
  // 挑一个**真有日线数据**的标的。随手取第一个非加密标的会选到空序列，
  // 截出来是一张纯黑图 —— 那验证的是空状态，不是日线。
  const withData = await page.evaluate(async () => {
    for (const o of document.querySelectorAll('#c-symbol option')) {
      if (o.value.startsWith('CRYPTO')) continue;
      const r = await fetch(`/api/bars?symbol=${encodeURIComponent(o.value)}&timeframe=1d&limit=1`);
      if (r.ok && (await r.json()).total > 0) return o.textContent;
    }
    return null;
  });
  const opts = await symSel.locator('option').allTextContents();
  const futures = withData || opts.find((o) => !o.includes('USDT'));
  if (futures) {
    await symSel.selectOption({ label: futures });
    await page.waitForTimeout(1500);
    const d1 = page.locator('#tf-group .tf', { hasText: '1d' });
    if (await d1.count()) { await d1.click(); await page.waitForTimeout(2000); }
    await shot('panel-8-日线');
  }
}

// 规则页：列表 + 编辑器 + 试算结果三栏，另外单独截一张跑完试算的
const rulesTab = page.locator('.tab', { hasText: '规则' });
if (await rulesTab.count()) {
  await rulesTab.click();
  await page.waitForTimeout(1200);
  const first = page.locator('#rule-items .rule-item').first();
  if (await first.count()) { await first.click(); await page.waitForTimeout(800); }
  await shot('panel-6-规则编辑');
  // 「新建」塞入示例模板 —— 编辑器**启用态**的文字颜色只有这时才看得到。
  // 之前只截了加载已有规则的那一瞬（恰好是 :disabled 态，唯一有颜色的状态），
  // 于是纯黑字配深色底一直没被发现。
  const mk = page.locator('#rule-new');
  if (await mk.count() && !(await mk.isDisabled())) {
    await mk.click();
    await page.waitForTimeout(700);
    await shot('panel-9-新建规则');
  }
  const trial = page.locator('#rule-trial');
  if (await trial.count() && !(await trial.isDisabled())) {
    await trial.click();
    await page.waitForTimeout(6000);
    await shot('panel-7-历史试算');
  }
}

await browser.close();
console.log(errors.length ? `\n❌ 控制台错误 ${errors.length} 条:\n  ${errors.slice(0, 8).join('\n  ')}`
                          : '\n✅ 控制台无错误');
process.exit(errors.length ? 1 : 0);
