/* 面板 JS 的桩化冒烟：不起浏览器，把 DOM / fetch / 图表库换成桩，
 * 验证 boot() 跑得通、渲染函数不抛、以及它到底打了哪些接口。
 * 由 tests/test_panel_js.py 调起；结果以一行 JSON 输出。 */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const APP = process.argv[2] || path.join("src", "sigdesk", "web", "static", "app.js");
const calls = [];

const FIX = {
  "/api/meta": {
    live: true, now_ts: 1788140000,
    symbols: [
      { uid: "CRYPTO.OKX.BTCUSDT.PERP", market: "CRYPTO", code: "BTC-USDT-SWAP", exchange: "OKX", price_tick: 0.1 },
      { uid: "CN.SHFE.rb2610", market: "CN", code: "rb2610", exchange: "SHFE", price_tick: 1 },
      { uid: "CN.SHFE.rb.CONT", market: "CN", code: "rb", exchange: "SHFE", price_tick: 1,
        is_continuous: true },
    ],
    timeframes: ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1mon"],
    rules: [{ id: "r1", description: "d", enabled: true, universe: ["CRYPTO.OKX.BTCUSDT.PERP"],
              timeframe: "1m", direction: "long", ttl_bars: 6, cooldown_s: 600,
              levels: [
                { role: "trend", on: "15m", mode: "state", within: 1, when: "close > ema(close,5)" },
                { role: "trigger", on: "1m", mode: "event", within: 1,
                  when: "cross_up(close, ema(close,10))" }] }],
  },
  "/api/rules": { editable: true, live: false, rules: [
    { id: "r1", description: "d", enabled: true, universe: ["CRYPTO.OKX.BTCUSDT.PERP"],
      timeframe: "1m", direction: "long",
      levels: [{ role: "trend", on: "15m", mode: "state", when: "close > ema(close,5)" },
               { role: "trigger", on: "1m", mode: "event", when: "cross_up(close, ema(close,10))" }] }] },
  "/api/rules/r1/source": { id: "r1", source: "id: r1\nuniverse: [X]\n" },
  "/api/rules/validate": { ok: true, rule: { id: "r1", timeframe: "1m",
    universe: ["CRYPTO.OKX.BTCUSDT.PERP"], levels: [{ role: "trend" }, { role: "trigger" }] } },
  "/api/rules/trial": {
    bars_scanned: 1200, symbols_scanned: ["CRYPTO.OKX.BTCUSDT.PERP"],
    symbols_without_data: ["CN.SHFE.nope"], warmup_bars: 0,
    signals: [{ rule_id: "r1", symbol: "CRYPTO.OKX.BTCUSDT.PERP", fired_at: 1788139900,
                direction: "long", timeframe: "1m", trigger_price: 77000, dedup_key: "k",
                context: {}, role_bars: {}, tentative: false, priority: "normal",
                trading_day: null }],
    outcomes: [], report: { overall: { signals: 1, win_rate: 1, avg_return: 0.004, payoff: 2.1,
      wins: 1, losses: 0, directional: 1, evaluated: 1, avg_bars_held: 12, median_return: 0.004,
      total_return: 0.004, avg_win: 0.004, avg_loss: 0, false_rate: 0, target_rate: 1,
      horizon_rate: 0, avg_mfe: 0.006, avg_mae: -0.001 },
      by_rule: {}, by_symbol: {}, by_hour: {}, by_direction: {}, params: { horizon_bars: 20 } },
    condition_bands: { "CRYPTO.OKX.BTCUSDT.PERP": [
      { role: "trend", timeframe: "15m", value: "unknown", from_ts: 1788139000,
        to_ts: 1788139600, bars: 5 },
      { role: "trend", timeframe: "15m", value: "true", from_ts: 1788139700,
        to_ts: 1788140000, bars: 4 },
      { role: "trigger", timeframe: "1m", value: "true", from_ts: 1788139900,
        to_ts: 1788139900, bars: 1 }] },
    condition_counts: { "CRYPTO.OKX.BTCUSDT.PERP": {
      trend: { true: 4, false: 2, unknown: 5 }, trigger: { true: 1, false: 30, unknown: 0 } } },
    rule: { id: "r1", timeframe: "1m", universe: [], levels: [] }, range: [0, 2147483648] },
  "/api/chains": { live: true, now_ts: 1788140000, chains: [
    { rule_id: "r1", symbol: "CRYPTO.OKX.BTCUSDT.PERP", phase: "armed", stage: 1, chain_len: 2,
      ttl_left: 4, ttl_bars: 6, armed_at: 1788139800, cooldown_until: null, cooldown_s: 600,
      last_fired_ts: null, steps: [
        { role: "trend", timeframe: "15m", mode: "state", within: 1, when: "close > ema(close,5)",
          done: true, satisfied: true, last_ts: 1788139800 },
        { role: "trigger", timeframe: "1m", mode: "event", within: 1, when: "cross_up(close, ema(close,10))",
          done: false, satisfied: false, last_ts: 1788139860 }] },
    { rule_id: "r1", symbol: "CN.SHFE.rb2610", phase: "cooldown", stage: 1, chain_len: 2,
      ttl_left: 0, ttl_bars: 6, armed_at: null, cooldown_until: 1788140600, cooldown_s: 600,
      last_fired_ts: 1788140000, steps: [
        { role: "trend", timeframe: "15m", mode: "state", when: "x", done: true, satisfied: true,
          last_ts: 1788139800 },
        { role: "trigger", timeframe: "1m", mode: "event", when: "y", done: false, satisfied: false,
          last_ts: null }] }] },
  "/api/trade": { live: true, total_fills: 3,
    fills: [
      { signal_key: "k1", symbol: "CRYPTO.OKX.BTCUSDT.PERP", side: "buy", qty: 0.05,
        price: 77010.2, ts: 1788139860, kind: "entry", fee: 0.77, realized: 0 },
      { signal_key: "k1", symbol: "CRYPTO.OKX.BTCUSDT.PERP", side: "sell", qty: 0.05,
        price: 77400.0, ts: 1788140400, kind: "target", fee: 0.77, realized: 18.03 },
      { signal_key: "k2", symbol: "CN.SHFE.rb2610", side: "sell", qty: 1,
        price: 3120.0, ts: 1788140100, kind: "entry", fee: 0.62, realized: 0 }],
    summary: { enabled: true, cash: 100018.03, equity: 100031.5, initial_cash: 100000,
      return_pct: 0.000315, realized: 18.03, fees: 2.16, unrealized: 13.47,
      exposure: 31200, trading_day: "2026-08-31", fills: 3,
      positions: [{ symbol: "CN.SHFE.rb2610", side: "sell", qty: 1, entry_price: 3120,
        multiplier: 10, opened_at: 1788140100, signal_key: "k2", stop: 3135.6,
        target: 3088.8, horizon_left: 12, mark: 3118.65, unrealized: 13.47 }],
      pending: [], daily_realized: { "2026-08-31": 18.03 },
      rejections: [{ signal_key: "k9", rule_id: "r1", symbol: "CRYPTO.OKX.BTCUSDT.PERP",
        side: "buy", qty: 0.4, created_at: 1788140200, ref_price: 77000, multiplier: 0.01,
        stop: 76615, target: 77770, notional: 308, risk: 1.54,
        reason: "per_trade_risk", detail: "单笔风险 1.54 > 上限 1.00" }] } },
  "/api/health": { live: true, healthy: false, problems: ["OKX WS"], uptime_s: 3600,
    signals_fired: 2, total_gaps: 1, bar_errors: 0, sse_subscribers: 1, sse_dropped: 0,
    feeds: [{ name: "OKX WS", connected: false, reconnects: 3, gaps: 1, backfills: 1, last_error: "抖动" }],
    symbols: [{ symbol: "CRYPTO.OKX.BTCUSDT.PERP", last_close_ts: 1788139900, lag_s: 100,
                bars_seen: 50, in_session: true, stale: true, gaps: [{ from: 1, to: 2 }] }] },
  // 与真实 /api/signals 一致：按 fired_at **升序**返回，面板自己 reverse 成最新在前
  "/api/signals": { total: 3, signals: [
    // 一条来自**已删掉的规则**：库里还有它的历史信号，但 /api/meta 里已经没有了。
    // 筛选框必须仍能筛到它，否则那些信号永远看不到（实测 50 条里 44 条是这种）。
    { rule_id: "retired-rule", symbol: "CN.SHFE.rb2610", direction: "long", timeframe: "1m",
      fired_at: 1788139700, trigger_price: 3100, dedup_key: "old", context: {}, role_bars: {} },
    { rule_id: "r1", symbol: "CN.SHFE.rb2610", direction: "short", timeframe: "1m",
      fired_at: 1788139800, trigger_price: 3120, dedup_key: "k2", context: {}, role_bars: {} },
    { rule_id: "r1", symbol: "CRYPTO.OKX.BTCUSDT.PERP", direction: "long", timeframe: "1m",
      fired_at: 1788139860, trigger_price: 77000.5, dedup_key: "k1",
      context: { close: 77000.5, volume: 12, "trend.ema5": 76990.25, rsi14: null },
      role_bars: { trend: 1788139800, trigger: 1788139860 } }] },
  "/api/bars": { symbol: "X", timeframe: "1m", total: 2, bars: [
    { open_ts: 1788139740, close_ts: 1788139800, open: 1, high: 2, low: 0.5, close: 1.5, volume: 3, trading_day: null },
    { open_ts: 1788139800, close_ts: 1788139860, open: 1.5, high: 2, low: 1, close: 1.8, volume: 4, trading_day: null }],
    // 均线由服务端算，预热期是 null —— 前端必须**跳过**这些点，不能补 0
    ma: [{ kind: "sma", window: 5, source: "close", label: "SMA5", values: [null, 1.65] },
         { kind: "sma", window: 20, source: "close", label: "SMA20", values: [null, null] }],
    vma: [{ kind: "sma", window: 20, source: "volume", label: "VSMA20",
            values: [null, 3.5] }] },
  "/api/intraday": { symbol: "X", trading_day: "2026-08-31", multiplier: 1,
    points: [{ ts: 1788139800, price: 77000.5, avg: 76980.2 },
             { ts: 1788139860, price: 77010.0, avg: 76990.0 },
             { ts: 1788139920, price: 77020.0, avg: null }] },
  "/api/markers": { symbol: "X", timeframe: "1m", signals: 4,
    markers: [
      { bucket_ts: 1788139800, fired_at: 1788139800, direction: "long", rule_id: "r1",
        dedup_key: "k1", trigger_price: 77000.5 },
      { bucket_ts: 1788139860, fired_at: 1788139860, direction: "short", rule_id: "r1",
        dedup_key: "k3", trigger_price: 77100.0 },
      { bucket_ts: 1788139920, fired_at: 1788139920, direction: "neutral", rule_id: "r1",
        dedup_key: "k4", trigger_price: 77050.0 }],
    fills: [
      { bucket_ts: 1788139860, ts: 1788139860, kind: "entry", side: "buy",
        price: 77010.2, qty: 0.05, realized: 0, signal_key: "k1" },
      { bucket_ts: 1788140400, ts: 1788140400, kind: "target", side: "sell",
        price: 77400.0, qty: 0.05, realized: 18.03, signal_key: "k1" }],
    dropped: [{ dedup_key: "k2", fired_at: 1788139860 }] },
  "/api/stats": { overall: { signals: 2, evaluated: 2, directional: 2, wins: 1, losses: 1,
      win_rate: 0.5, avg_return: 0.001, median_return: 0.001, total_return: 0.002,
      avg_win: 0.01, avg_loss: -0.008, payoff: 1.25, false_rate: 0.5, target_rate: 0.5,
      horizon_rate: 0, avg_mfe: 0.012, avg_mae: -0.004, avg_bars_held: 8.5 },
    by_rule: { r1: { signals: 2, directional: 2, win_rate: 0.5, avg_return: 0.001, payoff: 1.25, false_rate: 0.5 } },
    by_symbol: { "CRYPTO.OKX.BTCUSDT.PERP": { signals: 2, directional: 2, win_rate: 0.5,
      avg_return: 0.001, payoff: 1.25, false_rate: 0.5 } },
    by_hour: { 9: { signals: 2, directional: 2, win_rate: 0.5, avg_return: 0.001,
                    payoff: 1.25, false_rate: 0.5 },
               15: { signals: 3, directional: 3, win_rate: 0.67, avg_return: 0.002,
                     payoff: 1.4, false_rate: 0.33 } },
    by_direction: { long: { signals: 2, directional: 2, win_rate: 0.5, avg_return: 0.001,
      payoff: 1.25, false_rate: 0.5 } },
    params: { horizon_bars: 20, stop_pct: 0.005, target_pct: 0.01, cost_bps: 0 }, outcomes: [] },
};

function el() {
  const node = {
    children: [], dataset: {}, style: {}, value: "", _html: "", _text: "", classes: new Set(),
    get innerHTML() { return this._html; }, set innerHTML(v) { this._html = String(v); },
    get textContent() { return this._text; }, set textContent(v) { this._text = String(v); },
    classList: {
      add: (c) => node.classes.add(c), remove: (c) => node.classes.delete(c),
      contains: (c) => node.classes.has(c),
      // 桩要覆盖 app.js 用到的**每一个** DOM API，少一个就整条路径跑不通
      toggle: (c, on) => (on === undefined ? (node.classes.has(c)
        ? node.classes.delete(c) : node.classes.add(c))
        : (on ? node.classes.add(c) : node.classes.delete(c))),
    },
    querySelector: () => el(),
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ width: 660, height: 128, left: 0, top: 0 }),
    hidden: false,
    dataset: {},
    addEventListener(k, fn) { (node.listeners ||= {})[k] = fn; },
    get className() { return [...node.classes].join(" "); },
    set className(v) { node.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    children: [],
    appendChild(c) { node.children.push(c); return c; },
    remove() {}, onclick: null, onchange: null, onsubmit: null,
    onmouseenter: null, onmouseleave: null, setAttribute() {}, style: {},
    clientWidth: 800, clientHeight: 400,
  };
  return node;
}
// 夹具覆盖：测试用 SMOKE_FIX 环境变量塞进特定场景（如"扫了一堆 bar 但一条没触发"）。
// 比在测试里对本文件做字符串手术可靠得多 —— 那种改法很容易改出个真值来。
if (process.env.SMOKE_FIX) Object.assign(FIX, JSON.parse(process.env.SMOKE_FIX));

const nodes = new Map();
const asked = new Set();
const get = (sel) => {
  asked.add(sel);
  if (!nodes.has(sel)) nodes.set(sel, el());
  return nodes.get(sel);
};

/* **DOM 桩对任意选择器都返回节点** —— HTML 里少一个元素，桩化冒烟完全看不出来，
   真实浏览器一跑就白屏（`#grid-toggle` 就是这么漏过去的：替换没匹配上，
   按钮压根没插进 index.html，冒烟照样绿）。
   所以把 app.js 真正查过的 #id 拿去和 index.html 对一遍。 */
const INDEX = fs.readFileSync(
  path.join(path.dirname(APP), "index.html"), "utf8");
const missingIds = () => [...asked]
  .filter((s) => /^#[\w-]+$/.test(s))
  .filter((s) => !INDEX.includes(`id="${s.slice(1)}"`))
  .sort();

class FormData {
  get(k) { return { horizon_bars: "20", stop_pct: "0.5", target_pct: "1.0",
                    cost_bps: "0", entry_on_next_open: "on" }[k]; }
}
const sandbox = {
  document: { querySelector: get, querySelectorAll: () => [], addEventListener() {},
              createElement: () => el() },
  FormData, console, URLSearchParams, Date, Math, JSON, Number, String, Object, Array,
  Set, Map, Error, encodeURIComponent, setInterval: () => 0, clearInterval: () => {},
  ResizeObserver: class { observe() {} },
  // 存下 signal 监听器，测试里手动触发，才能验证"只有 SSE 推来的才提醒"
  EventSource: class {
    constructor(u) { calls.push(u); sandbox.__sse = this; this.on = {}; }
    addEventListener(k, fn) { this.on[k] = fn; }
    set onerror(_) {}
  },
  localStorage: {
    _d: {},
    getItem(k) { return this._d[k] ?? null; },
    setItem(k, v) { this._d[k] = String(v); },
  },
  Notification: Object.assign(
    class { constructor(t, o) { (sandbox.__notes ||= []).push({ t, o }); } },
    { permission: "granted", requestPermission: async () => "granted" }),
  SpeechSynthesisUtterance: class { constructor(t) { this.text = t; } },
  speechSynthesis: { speak(u) { (sandbox.__spoken ||= []).push(u.text); } },
  AudioContext: class {
    constructor() { this.state = "running"; this.currentTime = 0;
      this.destination = {}; sandbox.__beeps = sandbox.__beeps || 0; }
    async resume() { this.state = "running"; }
    createOscillator() {
      sandbox.__beeps += 1;
      return { type: "", frequency: { value: 0 },
        connect: (n) => n, start() {}, stop() {} };
    }
    createGain() {
      return { gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} },
        connect: (n) => n };
    }
  },
  setTimeout, clearTimeout,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  // 桩要覆盖 app.js 真正用到的每个图表 API —— 少一个方法 boot() 就整页失败
  LightweightCharts: { createChart: () => ({
    // 桩要覆盖 app.js 用到的**每一个**图表 API，少一个 boot() 就整页失败
    addHistogramSeries: () => ({ setData(d) { sandbox.__vol = d; }, setMarkers() {},
      applyOptions() {}, createPriceLine: (o) => o, removePriceLine() {} }),
    priceScale: () => ({ applyOptions() {} }),
    // 记下 priceScaleId：量能均线画在成交量那条轴上，价均线在默认轴 ——
    // 靠顺序区分很脆，靠它挂的是哪条轴才准
    addLineSeries: (o) => ({
      setData(d) { (sandbox.__lines ||= []).push({ scale: (o || {}).priceScaleId || "right",
                                                   n: d.length }); },
      applyOptions() {}, setMarkers() {},
      createPriceLine: (o) => o, removePriceLine() {} }),
    addCandlestickSeries: () => ({
      setData() {}, setMarkers(m) { sandbox.__markers = m; },
      createPriceLine(o) { (sandbox.__priceLines ||= []).push(o); return o; },
      removePriceLine(o) {
        sandbox.__priceLines = (sandbox.__priceLines || []).filter((x) => x !== o);
      },
    }),
    applyOptions() {}, subscribeCrosshairMove(fn) { sandbox.__xhair = fn; },
    setCrosshairPosition(price, time, series) {
      (sandbox.__xhairSet ||= []).push({ price, time });
    },
    clearCrosshairPosition() { (sandbox.__xhairClear ||= []).push(1); },
    timeScale: () => ({ setVisibleLogicalRange() {}, fitContent() {} }) }) },
  // 写端点也走这里：send() 会带 method/body，桩只按路径取夹具
  fetch: async (p, opts) => {
    calls.push((opts && opts.method ? opts.method + " " : "") + p);
    const key = "/" + p.split("?")[0].split("/").slice(1).join("/");
    if (!FIX[key]) throw new Error("桩里没有这个接口: " + key);
    return { ok: true, status: 200, json: async () => FIX[key] };
  },
  confirm: () => true,
  Boolean,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(APP, "utf8"), sandbox);

setTimeout(async () => {
  // boot() 只跑盯盘页；另两个视图由标签点击触发，桩里点不了，
  // 所以直接调它们的渲染函数（vm 里顶层函数就是全局）。
  let statsError = "", opsError = "", tradeError = "";
  try { await sandbox.loadStats(); } catch (e) { statsError = String(e && e.message || e); }
  try { await sandbox.loadOps(); } catch (e) { opsError = String(e && e.message || e); }
  try { await sandbox.loadTrade(); } catch (e) { tradeError = String(e && e.message || e); }
  // ---- 九宫格：9 个周期各建一格，点一格放大 ----
  let gridError = "";
  try {
    // `const G` 是词法绑定，**不会挂到 vm 的全局对象上**（只有 function/var 会）——
    // sandbox.G 是 undefined，得用 runInContext 在同一上下文里求值。
    const G = () => vm.runInContext("G", sandbox);
    await sandbox.toggleGrid(true);
    sandbox.__grid = {
      cells: [...G().cells.keys()],
      loaded: [...G().cells.values()].filter((c) => c.head._text || c.head._html).length,
      zoomed_before: G().zoomed,
    };
    // 十字线跨周期同步：给一个时刻，每格应对齐到**包含它的那根 bar**
    sandbox.__xhairSet = [];
    const gcells = [...G().cells.values()];
    const dense = gcells.find((c) => c.points.length > 1) || gcells[0];
    const target = dense.points[dense.points.length - 1].time;
    sandbox.syncCrosshair(target, dense.tf);
    sandbox.__grid = sandbox.__grid || {};
    sandbox.__sync = {
      target,
      source_tf: dense.tf,
      set: (sandbox.__xhairSet || []).map((x) => x.time),
      // 每个被设置的时刻都不能晚于目标 —— 那才叫"包含它的那根"
      all_at_or_before: (sandbox.__xhairSet || []).every((x) => x.time <= target),
      source_untouched: !(sandbox.__xhairSet || []).some((x) => x.tf === dense.tf),
    };
    sandbox.__xhairClear = [];
    sandbox.syncCrosshair(null, null);
    sandbox.__sync.cleared = (sandbox.__xhairClear || []).length;

    sandbox.zoomCell("1d");
    sandbox.__grid.zoomed_after = G().zoomed;
    sandbox.zoomCell("1d");            // 再点一次还原
    sandbox.__grid.zoomed_toggled_off = G().zoomed;
    await sandbox.toggleGrid(false);
    sandbox.__grid.off = G().on;
  } catch (e) { gridError = String(e && e.message || e); }

  // ---- 提醒：先记下"开机灌进历史信号后"的基线，再模拟 SSE 推两条 ----
  // 详情区默认显示**最新**一条信号，注入之后就会变成注入的那条 ——
  // 所以先把注入前的快照存下来，后面的断言仍针对真实夹具。
  const detailBefore = get("#detail").innerHTML;
  // 未选中任何信号时的标注（成交只画点、不写价）
  const marksUnselected = (sandbox.__markers || []).map(
    (m) => `${m.shape}|${m.position}|${m.color}|${m.text}`);
  // 选中带成交的那条信号后，它那几笔才显示价格
  try {
    await sandbox.select({ rule_id: "r1", symbol: "CRYPTO.OKX.BTCUSDT.PERP", direction: "long",
      timeframe: "1m", fired_at: 1788139800, trigger_price: 77000.5, dedup_key: "k1",
      context: {}, role_bars: {} });
    await new Promise((r) => setTimeout(r, 300));
  } catch { /* 选中失败不该影响其它断言 */ }
  sandbox.__marksSelected = (sandbox.__markers || []).map(
    (m) => `${m.shape}|${m.position}|${m.color}|${m.text}`);
  sandbox.__marksUnselected = marksUnselected;
  const baseToasts = get("#toasts").children.length;
  const baseBeeps = sandbox.__beeps || 0;
  let alertError = "";
  try {
    await sandbox.toggleAlert("sound");
    await sandbox.toggleAlert("speech");
    const afterToggle = { beeps: sandbox.__beeps || 0,
                          spoken: (sandbox.__spoken || []).length };
    const mk = (sym, dir, px) => ({ data: JSON.stringify({
      rule_id: "r1", symbol: sym, direction: dir, timeframe: "1m", fired_at: 1788140000,
      trigger_price: px, dedup_key: `${sym}:x`, context: {}, role_bars: {},
      tentative: false, priority: "normal", trading_day: null }) });
    sandbox.__sse.on.signal(mk("CRYPTO.OKX.BTCUSDT.PERP", "long", 78000));
    sandbox.__sse.on.signal(mk("CRYPTO.OKX.ETHUSDT.PERP", "long", 2400));
    await new Promise((r) => setTimeout(r, 900));   // 等合并窗口（400ms）过去
    sandbox.__alertResult = {
      toasts_before: baseToasts,
      toasts_after: get("#toasts").children.length,
      beeps_on_toggle: afterToggle.beeps - baseBeeps,
      beeps_from_signals: (sandbox.__beeps || 0) - afterToggle.beeps,
      spoken: (sandbox.__spoken || []).slice(afterToggle.spoken),
      notifications: (sandbox.__notes || []).length,
    };
  } catch (e) { alertError = String(e && e.message || e); }

  let rulesError = "", trialError = "";
  try {
    await sandbox.loadRules();
    await sandbox.openRule("r1");
    await sandbox.validateRule();
  } catch (e) { rulesError = String(e && e.message || e); }
  try { await sandbox.trialRule(); } catch (e) { trialError = String(e && e.message || e); }

  console.log(JSON.stringify({
    endpoints: [...new Set(calls.map((c) => c.split("?")[0]))].sort(),
    missing_ids: missingIds(),
    grid_error: gridError,
    grid: sandbox.__grid || null,
    sync: sandbox.__sync || null,
    alert_error: alertError,
    alerts: sandbox.__alertResult || null,
    rules_error: rulesError,
    trial_error: trialError,
    rule_filter_html: get("#f-rule").innerHTML,
    rule_items_html: get("#rule-items").innerHTML,
    symbol_options_html: get("#c-symbol").innerHTML,
    tf_buttons_html: get("#tf-group").innerHTML,
    rule_msg_text: get("#rule-msg").textContent,
    rule_banner_html: get("#rule-banner").innerHTML,
    trial_body_html: get("#trial-body").innerHTML,
    trial_sub_text: get("#trial-sub").textContent,
    feed_html: get("#feed").innerHTML,
    detail_html: (typeof detailBefore === "string") ? detailBefore
      : get("#detail").innerHTML,
    chains_html: get("#chain-grid").innerHTML,
    chains_hidden: get("#chains").hidden,
    heroes_html: get("#heroes").innerHTML,
    exit_legend_html: get("#exit-legend").innerHTML,
    exc_note_html: get("#exc-note").innerHTML,
    hour_warn: get("#hour-warn").textContent,
    ops_banner_html: get("#ops-banner").innerHTML,
    ops_timeline_html: get("#ops-timeline").innerHTML,
    ops_rules_html: get("#ops-rules").innerHTML,
    markers: (sandbox.__markers || []).length,
    ma_lines: (sandbox.__lines || []).filter((x) => x.scale !== "vol").map((x) => x.n),
    vma_lines: (sandbox.__lines || []).filter((x) => x.scale === "vol").map((x) => x.n),
    volume_points: (sandbox.__vol || []).length,
    volume_colors: [...new Set((sandbox.__vol || []).map((v) => v.color))].length,
    marker_detail: sandbox.__marksUnselected || [],
    marker_detail_selected: sandbox.__marksSelected || [],
    price_lines: (sandbox.__priceLines || []).map((l) => `${l.title}@${l.price}`),
    boot_failed: get("#app").innerHTML.includes("启动失败"),
    chart_note: get("#chart-note").textContent,
    stats_error: statsError,
    ops_error: opsError,
    trade_error: tradeError,
    trade_banner_html: get("#trade-banner").innerHTML,
    trade_positions_html: get("#trade-positions").innerHTML,
    trade_fills_html: get("#trade-fills").innerHTML,
    trade_rejects_html: get("#trade-rejects").innerHTML,
  }));
}, 500);
