/* Signal Desk 只读面板。原生 JS，无构建步骤（ADR-0009）。
 *
 * 一条贯穿的原则：**能算在服务端的就算在服务端**。这里只做取数与渲染 ——
 * 分桶、统计口径、链路状态都由后端算好，前端算的话那些验收就没法测。
 *
 * 时间一律按市场本地时区渲染（NFR-5）：期货北京时间、加密 UTC。
 * 预热期的 null 一律显示破折号，不显示 0（ADR-0006）。
 */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const S = {
  meta: null, signals: [], selected: null, symbol: null, timeframe: "5m",
  chart: null, series: null, bars: [],
};

/* ── 格式化 ─────────────────────────────────────── */

const isCrypto = (uid) => uid.startsWith("CRYPTO");
const shortSym = (uid) => uid.split(".").slice(-2).join(".");

function fmtTime(ts, uid, withDate = true) {
  if (!ts) return "—";
  const off = isCrypto(uid) ? 0 : 8 * 3600 * 1000;
  const t = new Date(ts * 1000 + off);
  const p = (n) => String(n).padStart(2, "0");
  const hm = `${p(t.getUTCHours())}:${p(t.getUTCMinutes())}`;
  const tz = isCrypto(uid) ? "UTC" : "CST";
  return withDate ? `${p(t.getUTCMonth() + 1)}-${p(t.getUTCDate())} ${hm} ${tz}` : `${hm} ${tz}`;
}
const hhmm = (ts, uid) => fmtTime(ts, uid, false);

/* 距今多久。给"你看的是不是陈数据"一个一眼可见的答案。 */
function ago(ts) {
  const m = Math.round((Date.now() / 1000 - ts) / 60);
  if (m < 2) return "刚刚";
  if (m < 60) return `${m} 分钟前`;
  if (m < 60 * 24) return `${Math.round(m / 60)} 小时前`;
  return `${Math.round(m / 1440)} 天前`;
}

// 有效数字取 4 位就够读：atr14 显示 47.11 而不是 47.1104。
const num = (x) => {
  if (x === null || x === undefined) return "—";
  if (Math.abs(x) >= 1000) return x.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return String(Number(Number(x).toPrecision(4)));
};
const pct = (x) => (x === null || x === undefined ? "—" : (x * 100).toFixed(1) + "%");
const signed = (x) => (x === null || x === undefined ? "—"
  : (x >= 0 ? "+" : "−") + Math.abs(x * 100).toFixed(3) + "%");
const cls = (x) => (x > 0 ? "pos" : x < 0 ? "neg" : "dim");
const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const DIR = {
  long: { label: "多", color: "#26a69a", cls: "pos" },
  short: { label: "空", color: "#ef5350", cls: "neg" },
  neutral: { label: "提示", color: "#8b949e", cls: "dim" },
};
// 方向用 SVG 画，不用 ▲▼ 这类字符：字符在不同字体下大小/基线飘，也不好上色。
function dirIcon(dir, size = 11) {
  const d = DIR[dir] || DIR.neutral;
  if (dir === "short") {
    return `<svg width="${size}" height="${size}" viewBox="0 0 12 11" class="dir-icon"
      ><polygon points="6,11 12,0 0,0" fill="${d.color}"></polygon></svg>`;
  }
  if (dir === "neutral") {
    return `<svg width="${size}" height="${size}" viewBox="0 0 12 12"
      ><circle cx="6" cy="6" r="4" fill="${d.color}"></circle></svg>`;
  }
  return `<svg width="${size}" height="${size}" viewBox="0 0 12 11"
    ><polygon points="6,0 12,11 0,11" fill="${d.color}"></polygon></svg>`;
}

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}
async function send(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let payload = null;
  try { payload = await r.json(); } catch { /* 空响应体 */ }
  if (!r.ok) {
    // detail 是后端写给人看的整句话（"为什么拒绝 + 该怎么办"），原样呈现，不要截断成状态码
    const e = new Error(payload?.detail || `${path} -> ${r.status}`);
    e.status = r.status;
    throw e;
  }
  return payload;
}
const setBadge = (sel, text, kind) => {
  const el = $(sel);
  el.innerHTML = (kind === "ok" || kind === "bad" || kind === "warn")
    ? `<i class="dot"></i>${esc(text)}` : esc(text);
  el.className = "badge" + (kind ? " " + kind : "");
};

function filtered() {
  const rule = $("#f-rule").value, sym = $("#f-symbol").value;
  return S.signals.filter((s) => (!rule || s.rule_id === rule) && (!sym || s.symbol === sym));
}

function renderFeed() {
  const rows = filtered().slice().reverse();
  $("#sig-count").textContent = `${rows.length} 条`;
  const box = $("#feed");
  if (!rows.length) {
    const picked = $("#f-rule").value;
    box.innerHTML = `<div class="empty">${
      picked
        ? `规则 <b>${esc(picked)}</b> 还没有产生过信号。<br>`
          + `（下拉框里每条规则后面的数字就是它的信号条数）`
        : "还没有信号。<br>规则触发后会实时出现在这里。"
    }</div>`;
    renderDetail(null);
    return;
  }
  const sel = S.selected && rows.find((s) => s.dedup_key === S.selected.dedup_key)
    ? S.selected : rows[0];
  // 记下**当前展示的是哪一条**。没点过任何一条时默认展示第一条，但那时 S.selected
  // 仍是 null —— drawMarkers 拿分组回来后要重画详情，只看 S.selected 会把它清空。
  S.shown = sel;
  renderDetail(sel);
  box.innerHTML = rows.filter((s) => s.dedup_key !== sel.dedup_key).map((s) => `
    <div class="sig" data-key="${esc(s.dedup_key)}">
      ${dirIcon(s.direction, 10)}
      <div class="sig-body">
        <div class="sig-r1"><span class="mono">${esc(shortSym(s.symbol))}</span>
          <span class="price mono">${num(s.trigger_price)}</span></div>
        <div class="sig-r2"><span class="rule">${esc(s.rule_id)}</span>
          <span class="time mono">${fmtTime(s.fired_at, s.symbol)}</span></div>
      </div></div>`).join("");
  $$("#feed .sig").forEach((el) => {
    el.onclick = () => select(rows.find((s) => s.dedup_key === el.dataset.key));
  });
}

/* 选中的信号展开成「为什么触发」：逐级别给证据，而不是一行截断的 k=v。 */
function renderDetail(s) {
  const box = $("#detail");
  if (!s) { box.innerHTML = ""; return; }
  S.shown = s;
  const rule = (S.meta?.rules || []).find((r) => r.id === s.rule_id);
  const ctx = s.context || {};
  // 规则可能已经下线（历史信号仍在库里），那时拿不到层级定义与 when 表达式。
  // 退化成按 context 的 role 前缀分组 —— 至少别把各级别的快照值整个吞掉。
  const levels = rule?.levels?.length ? rule.levels : [...new Set(
    Object.keys(ctx).filter((k) => k.includes(".")).map((k) => k.split(".")[0]))]
    .map((role) => ({ role, on: (s.role_bars || {})[role] ? "" : "", when: "（规则已下线）" }));
  const evidence = levels.map((l) => {
    const vals = Object.entries(ctx)
      .filter(([k]) => k.startsWith(l.role + "."))
      .map(([k, v]) => `${k.slice(l.role.length + 1)} ${num(v)}`).join("  ");
    return `<div class="ev">
      <span class="role mono">${esc(l.role)}${l.on ? " " + esc(l.on) : ""}</span>
      <span class="mono">${esc(l.when)}${vals ? `<br><span class="dim">${esc(vals)}</span>` : ""}</span>
      <span class="at mono">${hhmm((s.role_bars || {})[l.role], s.symbol)}</span></div>`;
  }).join("");
  const foot = Object.entries(ctx).filter(([k]) => !k.includes("."))
    .map(([k, v]) => `<span class="mono"><span class="lbl">${esc(k)} </span>${num(v)}</span>`).join("");
  box.innerHTML = `
    <div class="detail-top">${dirIcon(s.direction, 12)}
      <span class="mono" style="font-size:13px">${esc(shortSym(s.symbol))}</span>
      <span class="detail-price mono ${DIR[s.direction]?.cls || ""}">${num(s.trigger_price)}</span></div>
    <div class="detail-sub"><span class="lbl">${esc(s.rule_id)}</span>
      <span class="mono lbl" style="margin-left:auto">${fmtTime(s.fired_at, s.symbol)}</span></div>
    ${siblingsHtml(s)}
    ${evidence ? `<hr>${evidence}` : ""}
    ${foot ? `<div class="detail-foot">${foot}</div>` : ""}`;
  $$("#detail .sib").forEach((el) => {
    el.onclick = () => {
      const hit = S.signals.find((x) => x.dedup_key === el.dataset.key);
      if (hit) select(hit);
    };
  });
}

/* 折叠标记的展开面板：图上「×3」只画得下一枚代表，另外两条得在这里点得到。
   缺了它，折叠就等于把信号藏起来了 —— 那比不折叠更糟。
   分组用服务端的结果（S.groups），不在前端重算分桶：两处各算一套迟早算出两种归属。 */
function siblingsHtml(s) {
  const g = (S.groups || []).find(
    (m) => (m.members || []).some((x) => x.dedup_key === s.dedup_key));
  if (!g || g.count < 2) return "";
  const rows = g.members.map((m) => `
    <div class="sib${m.dedup_key === s.dedup_key ? " on" : ""}" data-key="${esc(m.dedup_key)}">
      <span class="rule">${esc(m.rule_id)}</span>
      <span class="mono lbl">${esc(m.timeframe || "")}</span>
      <span class="mono">${num(m.trigger_price)}</span></div>`).join("");
  return `<div class="sibs"><div class="lbl">同一根 bar 上共 ${g.count} 条${
    g.direction === s.direction ? "" : ""}（图上折成 ×${g.count}）</div>${rows}</div>`;
}

async function select(s) {
  if (!s) return;
  S.selected = s;
  S.symbol = s.symbol;
  $("#c-symbol").value = s.symbol;
  renderFeed();
  await loadChart(s.fired_at);
}

/* ── 图表 ───────────────────────────────────────── */

function ensureChart() {
  if (S.chart) return;
  S.chart = LightweightCharts.createChart($("#chart"), {
    layout: { background: { color: "#0e1116" }, textColor: "#8b949e", fontSize: 11 },
    grid: { vertLines: { color: "#1b2028" }, horzLines: { color: "#1b2028" } },
    rightPriceScale: { borderColor: "#262c36" },
    timeScale: { borderColor: "#262c36", timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 },
  });
  S.volume = S.chart.addHistogramSeries({
    priceScaleId: VOLUME_SCALE, priceFormat: { type: "volume" },
    priceLineVisible: false, lastValueVisible: false,
  });
  S.chart.priceScale(VOLUME_SCALE).applyOptions({
    scaleMargins: { top: 0.78, bottom: 0 }, borderVisible: false,
  });
  // K 线在成交量之后建，画在上层
  S.series = S.chart.addCandlestickSeries({
    upColor: "#26a69a", downColor: "#ef5350", borderVisible: false,
    wickUpColor: "#26a69a", wickDownColor: "#ef5350",
  });
  // 买卖点走自绘层，不用内置 setMarkers —— 内置的位置只能贴 bar 上/下方，
  // 徽章因此不在价格上（见 createMarkerLayer 开头）。
  S.markerLayer = createMarkerLayer();
  S.series.attachPrimitive(S.markerLayer);
  S.chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.06, bottom: 0.26 } });
  S.maSeries = [];
  S.chart.subscribeCrosshairMove((p) => {
    const b = p?.seriesData?.get(S.series);
    renderOhlc(b || S.bars.at(-1));
  });
  new ResizeObserver(() => S.chart.applyOptions({
    width: $("#chart").clientWidth, height: $("#chart").clientHeight,
  })).observe($("#chart"));
}

function renderOhlc(b) {
  if (!b) { $("#ohlc").innerHTML = ""; return; }
  const up = b.close >= b.open;
  $("#ohlc").innerHTML = [["O", b.open], ["H", b.high], ["L", b.low], ["C", b.close]]
    .map(([k, v], i) => `<span${i === 3 ? ` class="${up ? "pos" : "neg"}"` : ""}>` +
      `<span class="lbl">${k} </span>${num(v)}</span>`).join("") +
    (b.volume !== undefined ? `<span><span class="lbl">量 </span>${num(b.volume)}</span>` : "");
  // 均线图例浮在图上，不进头部 —— 四条均线值会把标的选择器和周期按钮挤到换行
  // 价均线与量均线的图例**都浮在图上**：塞进头部会把标的选择器和周期按钮挤到换行
  // （这个坑踩了两次 —— 第一次是价均线，第二次是量均线）
  const box = $("#ma-legend");
  if (box) {
    const fmt = (l) => `<span style="color:${l.color}">${esc(l.label)} ${num(l.value)}</span>`;
    const ok = (l) => l.value !== null && l.value !== undefined;
    box.innerHTML = (S.legend || []).filter(ok).map(fmt).join("")
      + ((S.vmaLegend || []).filter(ok).length
        ? `<span class="lbl">量</span>` + (S.vmaLegend || []).filter(ok).map(fmt).join("")
        : "");
  }
}

// 图表按市场本地时间显示：lightweight-charts 把 time 当 UTC 渲染，
// 所以期货这里先把时间戳平移 +8h，标签才与看盘软件一致。
const chartTime = (ts, uid) => ts + (isCrypto(uid) ? 0 : 8 * 3600);

async function loadChart(centerTs) {
  if (!S.symbol) return;
  ensureChart();
  const tf = S.timeframe;
  const data = await api(
    `/api/bars?symbol=${encodeURIComponent(S.symbol)}&timeframe=${tf}&limit=1500`
    + `&ma=${MA_MAIN}&vma=${VMA_MAIN}`);
  S.bars = data.bars;
  $("#chart-sym").textContent = `${shortSym(S.symbol)} · ${tf}`;
  if (!data.bars.length) {
    S.series.setData([]); renderOhlc(null);
    // 自绘层也要清，否则上一个标的的标记会留在空图上
    S.markerLayer.setData({ groups: [], fills: [], trades: [], selected: null });
    S.trades = []; S.groups = [];
    // 空状态必须说清楚**为什么**空。只说"没有数据"，用户只会以为是连不上（真发生过）。
    const meta = (S.meta?.symbols || []).find((x) => x.uid === S.symbol);
    const msg = meta && meta.watched === false
      ? `没有任何规则盯 ${shortSym(S.symbol)}，所以盯盘进程<b>不采集</b>它的行情。<br>`
        + `把它加进某条规则的 universe（「规则」页），或用 scripts/backfill.py 回补历史。`
      : `本地还没有 ${shortSym(S.symbol)} 的 ${tf} 数据。<br>`
        + `用 scripts/backfill.py 回补，或等实时数据落盘。`;
    $("#chart-note").textContent = "";
    let empty = $("#chart .chart-empty");
    if (!empty) {
      empty = document.createElement("div");
      empty.className = "chart-empty";
      $("#chart").appendChild(empty);
    }
    // **包一层块级元素**：`.chart-empty` 是 flex 容器，直接塞
    // "文本 + <b> + <br> + 文本" 会被拆成好几个 flex item 排成一行，换行全乱
    // （截图当场看到）。裹进一个 div 就回到正常的行内排版。
    empty.innerHTML = `<div>${msg}</div>`;
    return;
  }
  $("#chart .chart-empty")?.remove();
  S.series.setData(data.bars.map((b) => ({
    time: chartTime(b.close_ts, S.symbol),
    open: b.open, high: b.high, low: b.low, close: b.close,
  })));
  S.legend = drawOverlays(S.chart, S.series, data,
    { maStore: "maSeries", volume: S.volume, host: S });
  renderOhlc(data.bars.at(-1));
  const span = `${fmtTime(data.bars[0].close_ts, S.symbol)} → ${fmtTime(data.bars.at(-1).close_ts, S.symbol)}`;
  // 末根有多旧。**只陈述事实，不判断对错** —— 判断"该不该有新数据"要看交易日历，
  // 但"你正在看三天前的数据"这件事本身必须一眼可见：
  // 盯盘进程没在跑 / 没落盘时，图看着完全正常，只是永远不动（踩过，被当成"行情连不上"）。
  const base = `${data.total} 根（显示最近 ${data.bars.length} 根）　${span}　末根 ${ago(data.bars.at(-1).close_ts)}`;
  const i = centerTs ? data.bars.findIndex((b) => b.close_ts >= centerTs) : -1;
  if (i >= 0) {
    // 夹到数据末端：信号靠近最新一根时，不夹的话右半屏全是空白
    const half = 60, to = Math.min(i + half, data.bars.length + 4);
    S.chart.timeScale().setVisibleLogicalRange({ from: Math.max(-2, to - 2 * half), to });
  } else {
    // 没有可定位的信号就铺满。**必须显式 fitContent** —— 可视区间是图表实例上的
    // 持久状态，换标的/换周期不会自己重置：从 259 根的 5m 切到 61 根的日线，
    // 旧区间会让新数据缩在右边、左边空掉三分之一（截图才看得出来）。
    S.chart.timeScale().fitContent();
  }
  await drawMarkers(tf, base);
}

/* 均线与成交量的绘制。单图与九宫格共用，避免两处画法慢慢走偏。
 *
 * 两个必须守住的点：
 * - **预热期的 None 要跳过，不能补 0**。补 0 会在图左端画出一条从零飙起来的假线。
 * - 均线 series 要**复用**，不能每次载入都 addLineSeries —— 那样切几次周期就堆出
 *   几十条隐形序列，图表越来越卡。
 */
function drawOverlays(chart, priceSeries, data, opts) {
  const host = opts.host;
  const lines = data.ma || [];
  drawVolumeMa(chart, data, opts);
  host[opts.maStore] = host[opts.maStore] || [];
  const store = host[opts.maStore];
  while (store.length < lines.length) {
    store.push(chart.addLineSeries({
      color: MA_COLORS[store.length % MA_COLORS.length], lineWidth: 1,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    }));
  }
  lines.forEach((m, i) => {
    store[i].applyOptions({ color: MA_COLORS[i % MA_COLORS.length] });
    store[i].setData(data.bars
      .map((b, k) => ({ time: chartTime(b.close_ts, S.symbol), value: m.values[k] }))
      .filter((pt) => pt.value !== null && pt.value !== undefined));
  });
  for (let i = lines.length; i < store.length; i += 1) store[i].setData([]);

  if (opts.volume) {
    opts.volume.setData(data.bars.map((b) => ({
      time: chartTime(b.close_ts, S.symbol),
      value: b.volume,
      // 量柱跟着当根阴阳走 —— 和 K 线一个颜色语言，不用另记一套
      color: b.close >= b.open ? "rgba(38,166,154,.5)" : "rgba(239,83,80,.5)",
    })));
  }
  return lines.map((m, i) => ({ label: m.label,
                                color: MA_COLORS[i % MA_COLORS.length],
                                value: m.values.at(-1) }));
}

/* 量能均线画在**成交量那条价格轴**上（priceScaleId 与量柱相同），
   否则它会按价格刻度画，直接飞出画面。 */
function drawVolumeMa(chart, data, opts) {
  if (!opts.volume) return;
  const host = opts.host;
  const lines = data.vma || [];
  host.vmaSeries = host.vmaSeries || [];
  const store = host.vmaSeries;
  while (store.length < lines.length) {
    store.push(chart.addLineSeries({
      priceScaleId: VOLUME_SCALE,
      color: VMA_COLORS[store.length % VMA_COLORS.length], lineWidth: 1,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    }));
  }
  lines.forEach((m, i) => {
    store[i].applyOptions({ color: VMA_COLORS[i % VMA_COLORS.length] });
    store[i].setData(data.bars
      .map((b, k) => ({ time: chartTime(b.close_ts, S.symbol), value: m.values[k] }))
      .filter((pt) => pt.value !== null && pt.value !== undefined));
  });
  for (let i = lines.length; i < store.length; i += 1) store[i].setData([]);
  host.vmaLegend = lines.map((m, i) => ({ label: m.label,
                                          color: VMA_COLORS[i % VMA_COLORS.length],
                                          value: m.values.at(-1) }));
}

/* 图上标注的信号点必须与 SignalStore 记录逐条对得上（M3 验收）。
 * 分桶与筛选都在服务端做（/api/markers），前端只负责画。 */
/* 三种形状各司其职，避免撞车：
     圆点 = 信号（带 B/S 字母）
     方块 = 开仓成交
     箭头 = 离场成交（朝向 = 平仓方向）
   信号改成圆点之后，成交的离场点原本也是圆点，两者同色时完全分不开 —— 换成箭头。 */
const FILL = {
  entry:   { label: "开仓", shape: "square", color: "#58a6ff" },
  stop:    { label: "止损", shape: "arrow", color: "#ef5350" },
  target:  { label: "止盈", shape: "arrow", color: "#26a69a" },
  horizon: { label: "到期", shape: "arrow", color: "#8b949e" },
  forced:  { label: "强平", shape: "arrow", color: "#d29922" },
};

/* 信号 = "我认为该进场"，成交 = "实际以什么价成交了"。**两件事，分开画**。
 * 信号：多 = 绿色上箭（bar 下方）／空 = 红色下箭（bar 上方）／中性 = 灰圆点。
 *   中性以前也画成向上箭头，灰色的上箭会被读成"看多"，是错的。
 * 成交：开仓方块、离场圆点，**带价格文字** —— 成交点数量少，不会叠成一片，
 *   而"看不到成交的具体价格点"正是要解决的问题。 */
/* 买卖点用**圆点 + B/S 字母**，不用箭头。
   箭头只有朝向之分，一屏全是多头信号时看着就是一模一样的一片（用户反馈）；
   字母是直接可读的，扫一眼就知道哪个是买、哪个是卖。
   中性信号不写字母 —— 它既不是买也不是卖，硬安一个字母是撒谎。 */
const MARK = {
  long: { text: "B", position: "belowBar" },
  short: { text: "S", position: "aboveBar" },
  neutral: { text: "", position: "belowBar" },
};

/* 盈亏文字。**算不出来给空串，不给 0%** —— 独立只读模式拿不到合约乘数，
   名义本金无从算起；显示 0% 会被读成"这笔白做了"，与"不知道"是两回事。
   （这个项目已经在"算不出来显示成 0"上栽过好几次。） */
function pnlText(trades, key) {
  const t = (trades || []).find((x) => x.signal_key === key);
  if (!t || t.pnl_pct === null || t.pnl_pct === undefined) return "";
  return `${t.pnl_pct >= 0 ? "+" : ""}${t.pnl_pct.toFixed(2)}%`;
}


/* ── 买卖点自绘层 ─────────────────────────────────
 *
 * lightweight-charts 的内置 `setMarkers` 表达能力只有：4 种形状、一个颜色、
 * 一段**没有底片的纯文字**，位置只能是 aboveBar / belowBar / inBar。
 * 两个后果，都是用户当场指出来的：
 *
 *   1. **徽章不在价格上** —— 它贴的是那根 bar 的最高/最低点，不是触发价、
 *      也不是成交价。换了形状和文字也还是"跟之前一样"。
 *   2. 设计稿（docs/design/markers，方案 B+C）里的胶囊底片、半透明交易带、
 *      盈亏 chip 全都无从表达；没有底片的文字会被烛身吃掉。
 *
 * series primitive 拿得到 priceToCoordinate / timeToCoordinate，两件事一起解决：
 * 所有几何都锚在**真实价格**上，底片和色块自己画。
 *
 * 纯绘制、无状态副作用：喂什么画什么，因此可以在冒烟里用假 canvas 录下每一笔。 */

const BG = "#0e1116";           // 面板底色，胶囊底片用它才压得住 K 线
const INK = "#0e1116";          // 实心徽章上的字
const MONO = '11px ui-monospace, "SF Mono", Menlo, Consolas, monospace';

function roundRect(ctx, x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

/* 胶囊：底片 + 描边 + 文字。**底片是关键** —— 没有它，文字落在烛身上就没了，
   那正是内置 marker 的老问题。 */
function pill(ctx, x, y, text, color, rects, anchorY, width) {
  ctx.font = MONO;
  const w = ctx.measureText(text).width + 14;
  const h = 18;
  // 夹在画布里：贴边的胶囊会被切掉半个数字，读出来是错的价
  const cx = width ? Math.min(Math.max(x, w / 2 + 2), width - w / 2 - 2) : x;
  const box = { x: cx - w / 2, y: y - h / 2, w, h };
  // 与已画的胶囊相撞就退化成只留锚点：密集处宁可少几个价格，也不要糊成一团。
  if (rects.some((r) => box.x < r.x + r.w && box.x + box.w > r.x
                     && box.y < r.y + r.h && box.y + box.h > r.y)) return false;
  rects.push(box);
  // 引线：胶囊被推开之后得有东西把它和锚点连回去，否则读者对不上这是谁的价
  if (anchorY !== undefined && Math.abs(anchorY - y) > h / 2 + 2) {
    ctx.strokeStyle = withAlpha(color, 0.55);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, anchorY);
    ctx.lineTo(cx, y + (anchorY > y ? h / 2 : -h / 2));
    ctx.stroke();
  }
  ctx.fillStyle = BG;
  roundRect(ctx, box.x, box.y, w, h, 5);
  ctx.fill();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  roundRect(ctx, box.x + 0.5, box.y + 0.5, w - 1, h - 1, 5);
  ctx.stroke();
  ctx.fillStyle = color;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, cx, y + 0.5);
  return true;
}

function withAlpha(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function createMarkerLayer() {
  const state = { groups: [], fills: [], trades: [], selected: null, symbol: null };
  let series = null;
  let chart = null;
  const ops = [];   // 冒烟用：记下画了什么、画在哪（见 ops()）

  let repaint = () => {};
  const px = (t) => chart && chart.timeScale().timeToCoordinate(t);
  const py = (p) => series && series.priceToCoordinate(p);

  function draw(ctx, width) {
    ops.length = 0;
    const rects = [];
    /* **视窗外的一律不画。**
       timeToCoordinate 对"在数据里、但滚出屏幕"的时刻仍会给出坐标（负数或超宽），
       照画就会在两侧边缘堆出一列根本不在视野里的胶囊 —— 试过，比不画难看得多，
       而且那些价格属于别的时段，读者会当成当前这段的。 */
    const onScreen = (x) => x !== null && x >= -2 && (!width || x <= width + 2);
    const ts = state.trades || [];

    // ① 交易带铺在最底下：半透明色块 + 虚线连接 + 两端锚点 + 盈亏 chip
    for (const t of ts) {
      if (!t.exit) continue;              // 持仓中：还没有终点，画线就是编造
      const x1 = px(chartTime(t.entry.bucket_ts, state.symbol));
      const x2 = px(chartTime(t.exit.bucket_ts, state.symbol));
      const y1 = py(t.entry.price);
      const y2 = py(t.exit.price);
      if (y1 === null || y2 === null || x1 === null || x2 === null) continue;
      if (!onScreen(x1) && !onScreen(x2)) continue;   // 整笔都在视窗外
      if (x2 <= x1) continue;
      const color = t.pnl_pct === null || t.pnl_pct === undefined ? DIR.neutral.color
                  : (t.pnl_pct >= 0 ? DIR.long.color : DIR.short.color);
      // 价差小的时候色块会细到看不见，给个最小高度
      const h = Math.max(Math.abs(y2 - y1), 12);
      const top = Math.min(y1, y2) - (h - Math.abs(y2 - y1)) / 2;
      ctx.fillStyle = withAlpha(color, 0.13);
      ctx.fillRect(x1, top, x2 - x1, h);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
      ctx.setLineDash([]);
      for (const [x, y] of [[x1, y1], [x2, y2]]) {
        ctx.beginPath();
        ctx.arc(x, y, 3.5, 0, Math.PI * 2);
        ctx.fillStyle = BG;
        ctx.fill();
        ctx.strokeStyle = color;
        ctx.stroke();
      }
      ops.push(`band|${x1.toFixed(1)},${y1.toFixed(1)}|${x2.toFixed(1)},${y2.toFixed(1)}|${color}`);
    }

    // 徽章的占位**先登记**：信号画在最上层，但胶囊要躲开它 ——
    // 不先占位的话，胶囊会画在徽章底下被盖掉一半（真图上就是这样）。
    const badges = [];
    for (const m of state.groups || []) {
      const x = px(chartTime(m.bucket_ts, state.symbol));
      const y = py(m.trigger_price);
      if (y === null) continue;
      const w = (m.count || 1) > 1 ? 44 : 20;
      if (!onScreen(x)) continue;
      const box = { x: x - w / 2, y: y - 11, w, h: 22 };
      rects.push(box);
      badges.push({ m, x, y });
    }

    // ② 成交胶囊，锚在**成交价**上：开仓写价格，离场写盈亏
    for (const f of state.fills || []) {
      const k = FILL[f.kind] || FILL.horizon;
      const x = px(chartTime(f.bucket_ts, state.symbol));
      const y = py(f.price);
      if (y === null || !onScreen(x)) continue;
      const mine = state.selected && f.signal_key === state.selected.dedup_key;
      const pnl = f.kind === "entry" ? "" : pnlText(ts, f.signal_key);
      const text = mine ? `${k.label} ${num(f.price)}${pnl ? ` ${pnl}` : ""}`
                        : (f.kind === "entry" ? num(f.price) : (pnl || num(f.price)));
      // 锚点永远画（它才是"成交发生在这个价"的证据），胶囊撞了可以不画
      ctx.fillStyle = k.color;
      ctx.fillRect(x - 3, y - 3, 6, 6);
      // **开仓向下、离场向上**：一笔交易的两端分到锚点两侧，天然不会互相压。
      // 撞了就一层层往外让，本侧让不开再翻到另一侧 —— 五次都撞才退化成只留锚点。
      // 多试几次是有理由的：退化掉的往往是**盈亏**，而丢盈亏比丢价格亏得多。
      const side = f.kind === "entry" ? 1 : -1;
      const shown = [20, 40, 60, -20, -40].some(
        (d) => pill(ctx, x, y + side * d, text, k.color, rects, y, width));
      ops.push(`fill|${f.kind}|${x.toFixed(1)},${y.toFixed(1)}|${text}|${shown ? "pill" : "dot"}`);
    }

    // ③ 信号徽章画在最上层，锚在**触发价**上
    for (const { m, x, y } of badges) {
      const dir = DIR[m.direction] ? m.direction : "neutral";
      const color = DIR[dir].color;
      const letter = dir === "long" ? "B" : dir === "short" ? "S" : "";
      const n = m.count || 1;
      const label = n > 1 ? `${letter || "·"}×${n}` : letter;
      const on = state.selected
        && m.members.some((v) => v.dedup_key === state.selected.dedup_key);
      ctx.font = `700 ${MONO}`;
      if (n > 1) {
        const w = ctx.measureText(label).width + 12;
        ctx.fillStyle = color;
        roundRect(ctx, x - w / 2, y - 9, w, 18, 9);
        ctx.fill();
      } else {
        ctx.beginPath();
        ctx.arc(x, y, 9, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
      }
      if (on) {                       // 选中：外圈加一道环，不改变位置
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(x, y, 13, 0, Math.PI * 2);
        ctx.stroke();
      }
      if (label) {
        ctx.fillStyle = INK;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(label, x, y + 0.5);
      }
      ops.push(`signal|${m.direction}|${x.toFixed(1)},${y.toFixed(1)}|${label}|${n}`);
    }
  }

  return {
    attached(p) { series = p.series; chart = p.chart; repaint = p.requestUpdate || (() => {}); },
    detached() { series = null; chart = null; },
    updateAllViews() {},
    paneViews() {
      return [{
        zOrder: () => "top",
        renderer: () => ({
          draw: (target) => target.useMediaCoordinateSpace(
            ({ context, mediaSize }) => draw(context, mediaSize && mediaSize.width)),
        }),
      }];
    },
    setData(next) { Object.assign(state, next); repaint(); },
    // 冒烟读它来断言"徽章画在触发价上"。挂返回值上而不是全局：
    // 九宫格有九层，全局只留得下最后一层。
    ops: () => ops.slice(),
  };
}

/* 选中一条信号时，把它的价位画成横线：触发价 + （若已成交）开仓价。
 * 箭头只说明"哪一根"，横线才说明"哪个价"。 */
function drawPriceLines(signal, fills) {
  for (const line of S.priceLines || []) S.series.removePriceLine(line);
  S.priceLines = [];
  if (!signal) return;
  const rows = [{ price: signal.trigger_price, color: DIR[signal.direction]?.color || DIR.neutral.color,
                  title: "触发" }];
  for (const f of fills.filter((x) => x.signal_key === signal.dedup_key)) {
    const k = FILL[f.kind] || FILL.horizon;
    rows.push({ price: f.price, color: k.color, title: k.label });
  }
  for (const r of rows) {
    if (!Number.isFinite(r.price)) continue;
    S.priceLines.push(S.series.createPriceLine({
      price: r.price, color: r.color, lineWidth: 1, lineStyle: 2,
      axisLabelVisible: true, title: r.title,
    }));
  }
}

async function drawMarkers(tf, base) {
  const d = await api(`/api/markers?symbol=${encodeURIComponent(S.symbol)}&timeframe=${tf}`);
  const fills = d.fills || [];
  const picked = S.selected?.dedup_key;
  S.trades = d.trades || [];
  S.groups = d.markers || [];
  S.markerLayer.setData({
    groups: S.groups, fills, trades: S.trades,
    selected: S.selected, symbol: S.symbol,
  });
  drawPriceLines(S.selected, fills);
  renderDetail(S.shown);   // 同根 bar 的兄弟信号要等分组回来才知道
  const extra = d.dropped.length ? `　⚠️ ${d.dropped.length} 条不在本周期序列内` : "";
  // 折叠后标记枚数 < 信号条数是**正常**的，得说清楚差额去哪了，
  // 否则"标注 12/37"会被读成"25 条丢了"。
  const stacked = S.groups.filter((m) => m.count > 1).length;
  const folded = stacked ? `（${stacked} 处叠了多条，已折成 ×N）` : "";
  const open = S.trades.filter((t) => t.open).length;
  // 持仓中的不画连线（没有终点），得说清楚，否则会被当成漏画。
  const traded = S.trades.length
    ? `　交易 ${S.trades.length} 笔` + (open ? `（${open} 笔持仓中未画连线）` : "")
    : (fills.length ? `　成交 ${fills.length} 笔` : "");
  $("#chart-note").textContent =
    `${base}　标注 ${S.groups.length} 枚 / 信号 ${d.signals} 条${folded}${traded}${extra}`;
}

/* ── 质量统计 ───────────────────────────────────── */

const EXIT = [
  { key: "horizon_rate", label: "持有到期", fill: "#8b949e" },
  { key: "stop", label: "触及止损", fill: "#ef5350" },
  { key: "target", label: "触及止盈", fill: "#26a69a" },
];

/* 统计口径。质量统计页与规则试算共用同一份 —— 两处各写一份迟早分家，
   然后就会出现"试算说赚钱、统计说不赚"而无从排查。表单里是百分数，接口要小数。 */
function statsParams() {
  const f = new FormData($("#stats-form"));
  return {
    horizon_bars: Number(f.get("horizon_bars")),
    stop_pct: Number(f.get("stop_pct")) / 100,
    target_pct: Number(f.get("target_pct")) / 100,
    cost_bps: Number(f.get("cost_bps")),
    entry_on_next_open: Boolean(f.get("entry_on_next_open")),
  };
}

async function loadStats(e) {
  if (e) e.preventDefault();
  const sp = statsParams();
  const q = new URLSearchParams({
    horizon_bars: String(sp.horizon_bars),
    stop_pct: String(sp.stop_pct),
    target_pct: String(sp.target_pct),
    cost_bps: String(sp.cost_bps),
    entry_on_next_open: sp.entry_on_next_open ? "true" : "false",
  });
  const rep = await api("/api/stats?" + q);
  const o = rep.overall, p = rep.params;

  // 没有可评价的信号时，胜率/期望收益一律显示破折号，**不显示 0**：
  // 0 会被读成"不赚不赔"，而真相是"算不出来"（ADR-0006 的同一条原则）。
  const none = !o.evaluated;
  $("#heroes").innerHTML = [
    ["期望收益 / 条", none ? "—" : signed(o.avg_return), none ? "dim" : cls(o.avg_return),
      none ? "还没有可评价的信号" : `中位 ${signed(o.median_return)}`],
    ["胜率", none ? "—" : pct(o.win_rate), none ? "dim" : "",
      none ? "—" : `${o.wins} 胜 / ${o.losses} 负`],
    ["盈亏比", none || !o.payoff ? "—" : o.payoff.toFixed(2), none ? "dim" : "",
      none ? "—" : `均盈 ${signed(o.avg_win)} / 均亏 ${signed(o.avg_loss)}`],
    ["平均持有", none ? "—" : o.avg_bars_held.toFixed(1) + " 根", none ? "dim" : "",
      `上限 ${p.horizon_bars} 根`],
  ].map(([k, v, c, sub]) => `<div class="hero"><div class="lbl">${k}</div>
      <div class="v mono ${c}">${v}</div><div class="lbl">${sub}</div></div>`).join("");

  const n = o.directional || 0;
  const counts = [
    { ...EXIT[0], n: Math.round(o.horizon_rate * n), rate: o.horizon_rate },
    { ...EXIT[1], n: Math.round(o.false_rate * n), rate: o.false_rate },
    { ...EXIT[2], n: Math.round(o.target_rate * n), rate: o.target_rate },
  ];
  $("#exit-sub").textContent = n ? `${n} 条方向性信号是怎么结束的` : "还没有可评价的信号";
  if (!n) {
    // 0 条时不要画空条与空轴 —— 空图表看着像"加载失败"，而且下面的比例全是 0/0
    $("#exit-bar").innerHTML = "";
    $("#exit-legend").innerHTML = "";
    $("#excursion").innerHTML = "";
    $("#exc-note").innerHTML =
      '<span class="dim">还没有可评价的信号：需要信号触发后、且其后有足够的 bar 才能评价。</span>';
    renderHours(rep.by_hour);
    for (const [id, group, fmt] of [["#t-symbol", rep.by_symbol, shortSym],
                                    ["#t-rule", rep.by_rule, null],
                                    ["#t-direction", rep.by_direction, (k) => DIR[k]?.label || k]]) {
      $(id).innerHTML = statTable(group, fmt);
    }
    return;
  }
  let x = 0;
  $("#exit-bar").innerHTML = counts.map((c) => {
    const w = (c.rate || 0) * 660;
    const seg = `<rect x="${x}" y="0" width="${Math.max(0, w - 2)}" height="28" rx="4" fill="${c.fill}"></rect>`;
    x += w;
    return w > 0 ? seg : "";
  }).join("");
  // 三段全部直接标注：状态色不单独表意（dataviz 规范）
  $("#exit-legend").innerHTML = counts.map((c) =>
    `<span><i class="swatch" style="background:${c.fill}"></i>${c.label}
      <span class="mono dim">${pct(c.rate)} · ${c.n} 条</span></span>`).join("");

  // 浮动幅度与止损止盈距离画在同一根轴上 —— 一眼看出"到期"的原因
  const lim = Math.max(p.target_pct, p.stop_pct, Math.abs(o.avg_mfe), Math.abs(o.avg_mae)) * 1.25;
  const px = (v) => 230 + (v / lim) * 228;
  const row = (name, from, to, fill, opacity, val, strong) => {
    const a = px(from), b = px(to);
    return `<div class="exc"><span class="name ${strong ? "" : "dim"}">${name}</span>
      <svg viewBox="0 0 460 14" width="100%" height="14" preserveAspectRatio="none" style="flex:1">
        <line x1="230" y1="0" x2="230" y2="14" stroke="#262c36" stroke-width="1"></line>
        <rect x="${Math.min(a, b)}" y="3" width="${Math.max(3, Math.abs(b - a))}" height="8"
          rx="4" fill="${fill}" opacity="${opacity}"></rect></svg>
      <span class="val mono" style="color:${strong ? fill : "#8b949e"}">${val}</span></div>`;
  };
  $("#excursion").innerHTML =
    row("止盈距离", 0, p.target_pct, "#26a69a", 0.22, signed(p.target_pct), false) +
    row("平均最大浮盈", 0, o.avg_mfe, "#26a69a", 1, signed(o.avg_mfe), true) +
    row("平均最大浮亏", o.avg_mae, 0, "#ef5350", 1, signed(o.avg_mae), true) +
    row("止损距离", -p.stop_pct, 0, "#ef5350", 0.22, signed(-p.stop_pct), false);
  const rMfe = p.target_pct ? (o.avg_mfe / p.target_pct) : 0;
  const rMae = p.stop_pct ? (Math.abs(o.avg_mae) / p.stop_pct) : 0;
  $("#exc-note").innerHTML = n
    ? `平均最大浮盈 <span class="mono">${signed(o.avg_mfe)}</span> 只走到止盈距离的
       <span class="mono">${(rMfe * 100).toFixed(0)}%</span>，平均最大浮亏
       <span class="mono">${signed(o.avg_mae)}</span> 走到止损的
       <span class="mono">${(rMae * 100).toFixed(0)}%</span>。
       <span class="dim">这与「${pct(o.horizon_rate)} 持有到期」是同一件事的两种说法。</span>`
    : '<span class="dim">还没有可评价的信号。</span>';

  renderHours(rep.by_hour);
  for (const [id, group, fmt] of [["#t-symbol", rep.by_symbol, shortSym],
                                  ["#t-rule", rep.by_rule, null],
                                  ["#t-direction", rep.by_direction, (k) => DIR[k]?.label || k]]) {
    $(id).innerHTML = statTable(group, fmt);
  }
}

function statTable(group, fmt) {
  const keys = Object.keys(group);
  if (!keys.length) return '<div class="empty">暂无数据。</div>';
  return `<table><thead><tr><th></th><th>触发</th><th>方向性</th><th>胜率</th>
    <th>期望</th><th>盈亏比</th><th>假信号率</th></tr></thead><tbody>` +
    keys.map((k) => {
      const s = group[k];
      return `<tr><td class="mono">${esc(fmt ? fmt(k) : k)}</td><td>${s.signals}</td>
        <td>${s.directional}</td><td>${pct(s.win_rate)}</td>
        <td class="${cls(s.avg_return)}">${signed(s.avg_return)}</td>
        <td>${s.payoff ? s.payoff.toFixed(2) : "—"}</td><td>${pct(s.false_rate)}</td></tr>`;
    }).join("") + "</tbody></table>";
}

/* 分时段画的是**触发次数**（可靠的量），不是胜率 ——
 * n=2 的时段显示"胜率 100%"会误导人，所以样本不足时明说。 */
function renderHours(byHour) {
  const keys = Object.keys(byHour).sort((a, b) => a - b);
  if (!keys.length) {
    $("#hour-warn").textContent = "";
    $("#hour-chart").innerHTML =
      '<text x="330" y="64" fill="#8b949e" font-size="12" text-anchor="middle">暂无数据</text>';
    return;
  }
  const max = Math.max(1, ...keys.map((k) => byHour[k].signals));
  // 阈值取 10：5 条算出来的"胜率 60%"和抛硬币没区别，不该让人盯着它做判断。
  const ENOUGH = 10;
  $("#hour-warn").textContent = max < ENOUGH
    ? `样本量不足以分时段下结论 —— 最多的时段也只有 ${max} 条`
    : "";
  const W = 660, step = keys.length ? Math.min(46, (W - 24) / keys.length) : 0;
  $("#hour-chart").innerHTML =
    `<g stroke="#1b2028" stroke-width="1">
      <line x1="14" y1="100" x2="${W}" y2="100"></line>
      <line x1="14" y1="60" x2="${W}" y2="60"></line>
      <line x1="14" y1="20" x2="${W}" y2="20"></line></g>` +
    keys.map((k, i) => {
      const s = byHour[k], h = (s.signals / max) * 80, x = 22 + i * step;
      const w = Math.max(8, step - 8);
      return `<g class="hbar" data-hour="${k}"><rect x="${x}" y="${100 - h}" width="${w}"
        height="${Math.max(4, h)}" rx="4" fill="#58a6ff"
        opacity="${s.signals >= ENOUGH ? 1 : 0.45}"></rect>
        <text x="${x + w / 2}" y="115" fill="#8b949e" font-size="9" text-anchor="middle"
          font-family="ui-monospace,monospace">${String(k).padStart(2, "0")}</text>
        <text x="${x + w / 2}" y="${95 - h}" fill="#8b949e" font-size="9" text-anchor="middle"
          font-family="ui-monospace,monospace">${s.signals}</text></g>`;
    }).join("") +
    `<g fill="#8b949e" font-size="9" font-family="ui-monospace,monospace">
      <text x="0" y="23">${max}</text><text x="0" y="103">1</text></g>`;

  const tip = $("#hour-tip");
  $$("#hour-chart .hbar").forEach((g) => {
    g.style.cursor = "pointer";
    g.onmouseenter = (ev) => {
      const s = byHour[g.dataset.hour];
      tip.innerHTML = `<div class="mono">${String(g.dataset.hour).padStart(2, "0")}:00</div>
        <div class="mono dim">${s.signals} 条 · 胜率 ${pct(s.win_rate)} · 期望 ${signed(s.avg_return)}</div>`;
      tip.style.display = "block";
      const box = $("#hour-chart").getBoundingClientRect();
      tip.style.left = Math.min(box.width - 190, ev.offsetX + 12) + "px";
      tip.style.top = "0px";
    };
    g.onmouseleave = () => { tip.style.display = "none"; };
  });
}

/* ── 纸上账户 ───────────────────────────────────── */

// 去重键形如 CRYPTO.OKX.BTCUSDT.PERP:multi-level-verify:1788142500 ——
// 表格里只显示"规则:时间戳"，完整值放在 title 里，鼠标悬停可见。
const shortKey = (k) => { const parts = String(k).split(":"); return parts.slice(1).join(":") || k; };

const FILL_KIND = { entry: "开仓", stop: "止损", target: "止盈",
                    horizon: "到期", forced: "强平" };
const REJECT = {
  duplicate: "重复信号", no_direction: "无方向", zero_qty: "不足一手",
  per_trade_risk: "单笔风险超限", per_trade_notional: "单笔名义超限",
  symbol_exposure: "单品种超限", total_exposure: "总持仓超限",
  daily_loss: "日亏熔断", rate_limit: "下单频率超限", no_equity: "权益不足",
};

async function loadTrade() {
  const d = await api("/api/trade?limit=500");
  const s = d.summary;

  $("#trade-banner").innerHTML = s
    ? `<div class="banner ${s.return_pct >= 0 ? "ok" : "warn"}">
        <div><div style="font-size:14px">纸上账户${s.stale ? "（最后一次运行的快照）" : ""}</div>
          <div class="lbl" style="margin-top:1px">初始 ${num(s.initial_cash)} ·
            交易日 ${esc(s.trading_day || "—")}${s.stale ? " · " + esc(d.note || "") : ""}</div></div>
        <div class="banner-nums">${[
          ["权益", num(s.equity), cls(s.equity - s.initial_cash)],
          ["收益率", signed(s.return_pct), cls(s.return_pct)],
          ["已实现", num(s.realized), cls(s.realized)],
          ["浮动", num(s.unrealized), cls(s.unrealized)],
          ["手续费", num(s.fees), "dim"],
          ["敞口", num(s.exposure), ""],
          ["持仓", s.positions.length, ""],
        ].map(([k, v, c]) => `<div><div class="lbl">${k}</div>
            <div class="v mono ${c || ""}">${v}</div></div>`).join("")}</div></div>`
    : `<div class="banner"><svg width="18" height="18" viewBox="0 0 20 20"><circle cx="10" cy="10"
        r="8.2" fill="none" stroke="#8b949e" stroke-width="1.6"></circle><line x1="10" y1="6"
        x2="10" y2="11" stroke="#8b949e" stroke-width="1.6" stroke-linecap="round"></line>
        <circle cx="10" cy="14" r="0.9" fill="#8b949e"></circle></svg>
        <div><div style="font-size:14px">纸上交易台未接入</div>
          <div class="lbl" style="margin-top:1px">${esc(d.note || "")}</div></div></div>`;

  const pos = s?.positions || [];
  $("#trade-positions").innerHTML = pos.length
    ? `<table><thead><tr><th>标的</th><th>方向</th><th>数量</th><th>开仓价</th><th>现价</th>
        <th>浮动盈亏</th><th>止损</th><th>止盈</th><th>剩余持有</th></tr></thead><tbody>` +
      pos.map((p) => `<tr><td class="mono">${esc(shortSym(p.symbol))}</td>
        <td class="${p.side === "buy" ? "pos" : "neg"}">${p.side === "buy" ? "多" : "空"}</td>
        <td class="mono">${num(p.qty)}</td><td class="mono">${num(p.entry_price)}</td>
        <td class="mono">${num(p.mark)}</td>
        <td class="mono ${cls(p.unrealized)}">${p.unrealized === null ? "—" : num(p.unrealized)}</td>
        <td class="mono dim">${num(p.stop)}</td><td class="mono dim">${num(p.target)}</td>
        <td class="mono dim">${p.horizon_left || "—"}</td></tr>`).join("") + "</tbody></table>"
    : '<div class="empty">当前没有持仓。</div>';

  const fills = d.fills || [];
  $("#fill-count").textContent = `${d.total_fills} 笔`;
  $("#trade-fills").innerHTML = fills.length
    ? `<table><thead><tr><th>时间</th><th>标的</th><th>性质</th><th>方向</th><th>数量</th>
        <th>成交价</th><th>手续费</th><th>盈亏</th><th>来源信号</th></tr></thead><tbody>` +
      fills.slice().reverse().map((f) => `<tr>
        <td class="mono">${fmtTime(f.ts, f.symbol)}</td>
        <td class="mono">${esc(shortSym(f.symbol))}</td>
        <td>${FILL_KIND[f.kind] || f.kind}</td>
        <td class="${f.side === "buy" ? "pos" : "neg"}">${f.side === "buy" ? "买" : "卖"}</td>
        <td class="mono">${num(f.qty)}</td><td class="mono">${num(f.price)}</td>
        <td class="mono dim">${num(f.fee)}</td>
        <td class="mono ${cls(f.realized)}">${f.realized ? num(f.realized) : "—"}</td>
        <td class="mono dim" style="font-size:11px" title="${esc(f.signal_key)}"
          >${esc(shortKey(f.signal_key))}</td></tr>`).join("") +
      "</tbody></table>"
    : '<div class="empty">还没有成交。纸上交易台默认关闭，在 config/trading.yaml 里打开。</div>';

  const rejects = s?.rejections || [];
  $("#trade-rejects").innerHTML = rejects.length
    ? `<table><thead><tr><th>时间</th><th>标的</th><th>原因</th><th>数量</th>
        <th>名义</th><th>风险</th><th>说明</th></tr></thead><tbody>` +
      rejects.slice().reverse().map((r) => `<tr>
        <td class="mono">${fmtTime(r.created_at, r.symbol)}</td>
        <td class="mono">${esc(shortSym(r.symbol))}</td>
        <td class="warn">${REJECT[r.reason] || r.reason}</td>
        <td class="mono">${num(r.qty)}</td><td class="mono">${num(r.notional)}</td>
        <td class="mono">${num(r.risk)}</td>
        <td class="dim" style="font-size:11px">${esc(r.detail)}</td></tr>`).join("") +
      "</tbody></table>"
    : '<div class="empty">没有被拒的单。</div>';
}

/* ── 运行健康 ───────────────────────────────────── */

async function loadOps() {
  const h = await api("/api/health");
  const problems = h.problems || [];
  const kind = h.healthy === null ? "" : h.healthy ? "ok" : (problems.length ? "warn" : "bad");
  const icon = kind === "ok"
    ? `<svg width="18" height="18" viewBox="0 0 20 20"><circle cx="10" cy="10" r="8.2" fill="none"
        stroke="#26a69a" stroke-width="1.6"></circle><path d="M6 10.3l2.6 2.6L14 7.5" fill="none"
        stroke="#26a69a" stroke-width="1.8" stroke-linecap="round"></path></svg>`
    : `<svg width="18" height="18" viewBox="0 0 20 20"><path d="M10 2.5 L18.5 17.5 L1.5 17.5 Z"
        fill="none" stroke="#d29922" stroke-width="1.6" stroke-linejoin="round"></path>
        <line x1="10" y1="8" x2="10" y2="12.5" stroke="#d29922" stroke-width="1.6"
        stroke-linecap="round"></line><circle cx="10" cy="15" r="0.9" fill="#d29922"></circle></svg>`;
  const title = h.healthy === null ? "未接入实时引擎"
    : h.healthy ? "一切正常" : `${problems.length} 处需要关注`;
  const sub = h.healthy === null
    ? "独立只读模式：只看历史信号与统计。想看数据流与实时信号，用 scripts/watch.py --web 启动。"
    : problems.length ? problems.join("、") : "所有数据流已连接，无缺口";
  $("#ops-banner").innerHTML = `<div class="banner ${kind}">${icon}
    <div><div style="font-size:14px${kind ? `;color:${kind === "ok" ? "#26a69a" : "#d29922"}` : ""}">${esc(title)}</div>
      <div class="lbl" style="margin-top:1px">${esc(sub)}</div></div>
    <div class="banner-nums">${[
      ["运行时长", h.uptime_s ? (h.uptime_s / 3600).toFixed(1) + " h" : "—"],
      ["已触发", h.signals_fired ?? "—"], ["累计缺口", h.total_gaps ?? "—"],
      ["异常 bar", h.bar_errors ?? "—"], ["SSE 订阅", h.sse_subscribers ?? "—"],
    ].map(([k, v]) => `<div><div class="lbl">${k}</div><div class="v mono">${v}</div></div>`).join("")}
    </div></div>`;

  $("#ops-feeds").innerHTML = (h.feeds || []).length
    ? h.feeds.map((f) => `<div class="feed-card ${f.connected ? "" : "bad"}">
        <div style="display:flex;align-items:center;gap:8px">
          <i class="dot" style="color:${f.connected ? "#26a69a" : "#ef5350"}"></i>
          <span style="font-size:13px">${esc(f.name)}</span>
          <span class="lbl" style="margin-left:auto;color:${f.connected ? "#26a69a" : "#ef5350"}">
            ${f.connected ? "已连接" : "断开"}</span></div>
        <div class="feed-metrics">
          ${[["重连", f.reconnects], ["缺口", f.gaps], ["回补", f.backfills]].map(([k, v]) =>
            `<div><div class="lbl">${k}</div><div class="v mono ${v ? "warn" : ""}">${v}</div></div>`).join("")}
          <div style="margin-left:auto;text-align:right;max-width:220px">
            <div class="lbl">最后错误</div>
            <div class="mono ${f.last_error ? "warn" : "dim"}" style="font-size:11px;margin-top:3px"
              >${esc(f.last_error || "—")}</div></div></div></div>`).join("")
    : '<div class="empty">未接入实时引擎 —— 用 scripts/watch.py --web 启动即可看到数据流状态。</div>';

  $("#tl-legend").innerHTML = [["数据连续", "#26a69a"], ["缺口", "#ef5350"], ["休市（正常）", "#262c36"]]
    .map(([l, c]) => `<span><i class="swatch" style="background:${c}"></i><span class="lbl">${l}</span></span>`).join("");

  const now = h.now_ts || Math.floor(Date.now() / 1000);
  const WIN = 6 * 3600, W = 900;
  const at = (ts) => Math.max(0, Math.min(W, ((ts - (now - WIN)) / WIN) * W));
  $("#ops-timeline").innerHTML = (h.symbols || []).length
    ? h.symbols.map((s) => {
        const gaps = (s.gaps || []).filter((g) => g.to > now - WIN);
        let spans = `<rect x="0" y="4" width="${W}" height="6" rx="3" fill="#26a69a"></rect>`;
        spans += gaps.map((g) =>
          `<rect x="${at(g.from)}" y="4" width="${Math.max(2, at(g.to) - at(g.from))}"
            height="6" rx="3" fill="#ef5350"></rect>`).join("");
        if (!s.in_session) {
          spans += `<rect x="${at(s.last_close_ts)}" y="4" width="${W - at(s.last_close_ts)}"
            height="6" rx="3" fill="#262c36"></rect>`;
        }
        return `<div class="tl-row"><span class="name mono">${esc(shortSym(s.symbol))}</span>
          <span class="lag mono ${s.stale ? "warn" : "dim"}">${s.lag_s === null ? "—" : s.lag_s + "s"}</span>
          <svg viewBox="0 0 ${W} 14" width="100%" height="14" preserveAspectRatio="none" style="flex:1">
            <rect x="0" y="4" width="${W}" height="6" rx="3" fill="#161b22"></rect>${spans}</svg>
          <span class="last mono">${fmtTime(s.last_close_ts, s.symbol)}</span>
          <span class="bars mono">${s.bars_seen}</span></div>`;
      }).join("")
    : '<div class="empty">暂无标的数据 —— 独立只读模式下没有实时新鲜度可看。</div>';

  const rules = S.meta?.rules || [];
  $("#ops-rules").innerHTML = rules.length
    ? rules.map((r) => `<div class="feed-card" style="display:flex;align-items:center;gap:20px;flex-wrap:wrap">
        <span class="mono" style="font-size:13px">${esc(r.id)}</span>
        <span class="mono lbl">${r.levels.map((l) =>
          // 单级别规则的角色名就是周期名，写成 15m:15m/event 是冗余
          esc(l.role === l.on ? `${l.on}/${l.mode}` : `${l.role}:${l.on}/${l.mode}`)).join(" → ")}</span>
        <div style="display:flex;gap:20px;margin-left:auto">
          <div><span class="lbl">方向 </span><span class="${DIR[r.direction]?.cls || ""}"
            >${DIR[r.direction]?.label || r.direction}</span></div>
          <div><span class="lbl">TTL </span><span class="mono">${r.ttl_bars || "—"}</span></div>
          <div><span class="lbl">冷却 </span><span class="mono">${r.cooldown_s ? r.cooldown_s / 60 + "m" : "—"}</span></div>
          <div><span class="lbl">标的 </span><span class="mono">${r.universe.length}</span></div>
        </div></div>`).join("")
    : '<div class="empty">没有加载任何规则。</div>';
}

/* ── 实时流 ─────────────────────────────────────── */

/* ── 九宫格：同一标的的 9 个周期同屏 ────────────────────────────
 *
 * 为什么不是"切回单图页"：看大做小要的是**同时**看见大级别形态和小级别时机。
 * 来回切会丢掉"大级别此刻长什么样"，那正是这个视图要解决的事。
 *
 * 几个实现上的要点：
 * - 9 个图表实例懒建、复用；换标的只换数据不重建（重建会闪一下，还漏掉滚动位置）。
 * - lightweight-charts **不会自己跟随容器尺寸**，放大/还原/切页都必须显式 applyOptions。
 * - 每格 bar 数比单图少：小格子里塞 1500 根就是一团黑，看不出形态。
 * - 信号标注每格都画 —— 同一条信号在 1m 和 1d 上的位置不同，那正是要看的。
 */
/* 第一格是**分时**，不是周期：它是当日 1m 的一种画法（折线 + 均价线），
   所以不占 Timeframe 枚举，数据走 /api/intraday（均价要合约乘数，只有服务端有）。 */
const INTRADAY = "分时";

/* 九宫格的周期排布（三行三列，顺序即屏幕顺序）。
   **不用 meta.timeframes**：那是"系统支持哪些周期"，这里是"我要同屏看哪九个"，
   两件事。4h 被挤掉了 —— 九个格子装不下十个周期，取舍由用户定。 */
const GRID_TFS = [INTRADAY, "1m", "5m",
                  "15m", "30m", "1h",
                  "1d", "1w", "1mon"];
const GRID_BARS = 220;
/* 均线由**服务端**用引擎同款 SMA/EMA 算（见 web/overlay.py）：
   自己在前端拿 close 重算，口径一偏就会出现"图上上穿了、规则没触发"。
   单图多画几条，小格子只画两条 —— 小格里六条线就是一团麻。 */
const MA_MAIN = "5,10,20,60";
const MA_CELL = "5,20";
/* 量能均线。**20 这个窗口不是随便挑的** —— 内置规则 volume-spike 判的就是
   `volume > sma(volume, 20) * 2.5`，图上这条线和规则看的必须是同一个数，
   否则你看不出它当时为什么触发。 */
const VMA_MAIN = "5,20";
const VMA_CELL = "20";
const VMA_COLORS = ["#e6c07b", "#8b949e"];
const MA_COLORS = ["#e6c07b", "#61afef", "#c678dd", "#98c379", "#e06c75", "#56b6c2"];
/* 成交量画在同一窗格底部（v4 没有真正的多窗格）：
   独立 priceScaleId + scaleMargins 把它压到下面 25%，不挤占 K 线。 */
const VOLUME_SCALE = "vol";
const MARKER_MIN_BARS = 20;   // 少于这么多根就不画标注（会被撑得巨大）
const G = {
  cells: new Map(), wcells: new Map(), on: false, zoomed: null,
  // 网格的两种模式。**同一套组件**：两处各画一套的话，同一条信号在
  // 单图和格子里会落在不同高度，那是最容易被读错又不会报错的不一致。
  mode: "watch",          // "watch" = 九标的×一周期；"tf" = 一标的×九周期
  market: "CN",           // 预警组按市场分组：加密信号不该挤掉你钉着的螺纹
  wtf: "5m",              // 预警组整组一个周期。默认 5m —— 找买点的级别
  wl: null,               // 最近一次 /api/watchlist 的返回
  seen: new Set(),        // 已读的信号 dedup_key（本地，见 loadSeen）
  slots: [],              // 预警组里的空槽元素，自己记着好清理

  pinned: null,     // 被 shift+单击锁定的时刻；不为空时十字线不跟鼠标走
  syncing: false,   // **防回环**：程序设置十字线会再次触发 crosshairMove 回调
};

/* 已读状态存本地：它是"我这个人看没看过"，不是账本上的事实，
   没必要占服务端一张表；换台机器重新标一遍也无所谓。 */
const SEEN_KEY = "sigdesk.watchlist.seen";
function loadSeen() {
  try { G.seen = new Set(JSON.parse(localStorage.getItem(SEEN_KEY) || "[]")); }
  catch { G.seen = new Set(); }
}
function markSeen(key) {
  if (!key || G.seen.has(key)) return;
  G.seen.add(key);
  // 只留最近 500 条：已读集合会随信号数无限长大，而老信号早就不在组里了
  const keep = [...G.seen].slice(-500);
  G.seen = new Set(keep);
  try { localStorage.setItem(SEEN_KEY, JSON.stringify(keep)); } catch { /* 无痕模式 */ }
}

/* 一个格子。两种模式**共用这一个构造函数** —— 图表机制（十字线同步、均线、
   成交量、双击放大、自绘标记层）完全相同，不同的只有格子头部长什么样、
   以及格子的身份是"周期"还是"标的"。
   两处各建一套的话，同一条信号在两种模式下会落在不同高度，
   那是最容易被读错、又不会报错的一种不一致。 */
function makeCell(map, key, tf, headHtml) {
  let cell = map.get(key);
  if (cell) return cell;
  const root = document.createElement("div");
  root.className = "cell";
  root.innerHTML = headHtml
    + `<div class="cell-body"></div><span class="zoom">双击放大 / 还原　Esc 返回</span>`;
  $("#grid").appendChild(root);
  const body = root.querySelector(".cell-body");
  const chart = LightweightCharts.createChart(body, {
    // 小格子里那个 TradingView 角标正好压在 K 线上，9 格就是 9 个（v4.2 起可关）。
    // 单图页保留 —— 那里空间够，不碍事。
    layout: { background: { color: "#0e1116" }, textColor: "#8b949e", fontSize: 10,
              attributionLogo: false },
    grid: { vertLines: { color: "#1b2028" }, horzLines: { color: "#1b2028" } },
    rightPriceScale: { borderColor: "#262c36" },
    timeScale: { borderColor: "#262c36", timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 },
    handleScroll: false, handleScale: false,   // 小格子里误拖一下就乱了，放大后再交互
  });
  // 分时是两条折线（价格 + 均价），其余是 K 线 —— 结构不同，建的时候就分岔
  const isIntraday = tf === INTRADAY;
  const series = isIntraday
    ? chart.addLineSeries({ color: "#e6edf3", lineWidth: 1, priceLineVisible: true,
                            lastValueVisible: true })
    : chart.addCandlestickSeries({
        upColor: "#26a69a", downColor: "#ef5350", borderVisible: false,
        wickUpColor: "#26a69a", wickDownColor: "#ef5350",
      });
  const avg = isIntraday
    ? chart.addLineSeries({ color: "#d29922", lineWidth: 1, priceLineVisible: false,
                            lastValueVisible: false })
    : null;
  let volume = null;
  if (!isIntraday) {
    volume = chart.addHistogramSeries({
      priceScaleId: VOLUME_SCALE, priceFormat: { type: "volume" },
      priceLineVisible: false, lastValueVisible: false,
    });
    chart.priceScale(VOLUME_SCALE).applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 }, borderVisible: false,
    });
    chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.08, bottom: 0.22 } });
  }
  // **双击**放大，不是单击：单击要留给"停在这一格上看十字线、对比多周期"，
  // 单击就放大等于根本没法在小格里看盘（用户实际用起来第一件事就撞上）。
  root.ondblclick = () => zoomCell(key);
  // points 存这一格的 (time, close)，供跨周期对齐十字线用
  // 分时那格没有信号标记（它是折线，不走 /api/markers），其余每格挂一层
  const layer = isIntraday ? null : createMarkerLayer();
  if (layer) series.attachPrimitive(layer);
  cell = { key, tf, root, body, chart, series, avg, volume, layer, points: [], maSeries: [],
           head: root.querySelector(".ohlc") };
  // 悬停即同步：在任意一格移动，其余八格显示**同一时刻**的十字线。
  // 这正是"看大做小"要的 —— 一眼看清同一刻各级别长什么样。
  chart.subscribeCrosshairMove((param) => {
    if (G.syncing) return;              // 防回环：下面的 setCrosshairPosition 会再触发本回调
    if (G.pinned !== null) return;      // 已锁定就别被鼠标带跑
    syncCrosshair(param.time ?? null, key);
  });
  // shift + 单击 = 锁定这一刻（再点一次或按 Esc 解除）。
  // 不用普通单击：普通单击要留给"随便点点不触发任何东西"。
  root.addEventListener("click", (ev) => {
    if (!ev.shiftKey) return;
    ev.preventDefault();
    G.pinned = G.pinned === null ? (G.lastHover ?? null) : null;
    if (G.pinned === null) syncCrosshair(null, null);
    else syncCrosshair(G.pinned, null);
    renderPinBadge();
  });
  map.set(key, cell);
  new ResizeObserver(() => fitCell(cell)).observe(body);
  return cell;
}

/* 模式一：一个标的的九个周期。格子的身份是周期。 */
function gridCell(tf) {
  return makeCell(G.cells, tf, tf,
    `<div class="cell-head"><span class="cell-tf mono">${esc(tf)}</span>
       <span class="ohlc mono"></span></div>`);
}

/* 模式二（预警组）：九个标的、同一个周期。格子的身份是标的。
   头部比周期模式多两样：**钉图标**（人工判断「还需要观察」的唯一表达）
   和**触发理由那一行**（它为什么在这个组里 —— 不写就是一堆无差别的缩略图）。 */
function watchCell(uid) {
  const cell = makeCell(G.wcells, uid, G.wtf,
    `<div class="cell-head">
       <span class="cell-mark"></span>
       <span class="cell-tf mono"></span>
       <button class="cell-pin" type="button" title="钉住：不会被新信号挤掉">${PIN_SVG}</button>
       <span class="ohlc mono"></span></div>
     <div class="cell-why"><span class="rule"></span><span class="lbl ago"></span></div>`);
  if (!cell.name) {
    cell.name = cell.root.querySelector(".cell-tf");
    cell.mark = cell.root.querySelector(".cell-mark");
    cell.pin = cell.root.querySelector(".cell-pin");
    cell.why = cell.root.querySelector(".cell-why .rule");
    cell.ago = cell.root.querySelector(".cell-why .ago");
  }
  return cell;
}

const PIN_SVG = `<svg width="11" height="11" viewBox="0 0 16 16" fill="none"
  stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"
  ><path d="M9.5 1.5 L14.5 6.5 L12 7 L8.5 10.5 L8 13.5 L2.5 8 L5.5 7.5 L9 4 Z"></path
  ><line x1="2.5" y1="13.5" x2="6" y2="10"></line></svg>`;

/* 把某个时刻同步到所有格子。
 * **跨周期要对齐到"包含该时刻的那根 bar"** —— 1m 上的 10:03 在 1d 上不是一根 bar，
 * 直接把原时刻塞给日线图，十字线要么不显示要么落在错的位置。
 * 取"最后一根 time <= 目标"的 bar，就是包含它的那根。 */
function syncCrosshair(time, sourceKey) {
  G.syncing = true;
  try {
    G.lastHover = time;
    // **走 activeCells() 并按 key 比对。** 只遍历 G.cells 的话预警组里同步不生效；
    // 按 tf 比对的话预警组九格 tf 相同，会被当成"源格子"全部跳过 —— 两个都错。
    for (const c of activeCells()) {
      if (c.key === sourceKey) continue;   // 源格子由图表自己画，别覆盖
      if (time === null || !c.points.length) { c.chart.clearCrosshairPosition(); continue; }
      let lo = 0, hi = c.points.length - 1, found = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (c.points[mid].time <= time) { found = mid; lo = mid + 1; } else { hi = mid - 1; }
      }
      if (found < 0) { c.chart.clearCrosshairPosition(); continue; }
      const p = c.points[found];
      c.chart.setCrosshairPosition(p.value, p.time, c.series);
    }
  } finally {
    G.syncing = false;
  }
}

function renderPinBadge() {
  const note = $("#chart-note");
  if (!note) return;
  const base = note.dataset.base || note.textContent;
  note.dataset.base = base;
  note.textContent = G.pinned === null ? base
    : `${base}　📌 已锁定 ${fmtTime(G.pinned - (isCrypto(S.symbol) ? 0 : 8 * 3600), S.symbol)}`
      + `（Esc 或再次 shift+单击解除）`;
}

const fitCell = (c) => c.chart.applyOptions({
  width: c.body.clientWidth, height: c.body.clientHeight,
});

/* 双击放大一格 / 再双击还原。

   **按 `key` 而不是 `tf` 匹配，且两种模式的格子都要遍历。**
   预警组模式下九格的 `tf` 全都一样（整组一个周期），拿 tf 匹配会让九格一起变 big；
   而只遍历 G.cells 的话，预警组里双击**什么都不会发生** —— 放大失效，
   连带"放大后才允许缩放"也一并失效（用户报的"不支持放大缩小"就是这个）。 */
function zoomCell(key) {
  const grid = $("#grid");
  const next = G.zoomed === key ? null : key;
  G.zoomed = next;
  grid.classList.toggle("zoomed", !!next);
  for (const c of activeCells()) {
    const big = c.key === next;
    c.root.classList.toggle("big", big);
    // 放大的那格允许滚轮缩放 / 拖动；缩回去要关掉，否则小格里误拖会把视图弄乱
    c.chart.applyOptions({ handleScroll: big, handleScale: big });
  }
  // 尺寸变了必须显式重算 —— 图表不会自己跟随容器
  requestAnimationFrame(() => {
    for (const c of activeCells()) {
      if (!next || c.key === next) { fitCell(c); c.chart.timeScale().fitContent(); }
    }
  });
}

/* 当前模式下挂在网格里的格子。两种模式的格子分别存在两个 Map 里，
   凡是"对所有格子做点什么"的地方都必须走这里，否则又会漏掉一半。 */
function activeCells() {
  return [...(G.mode === "watch" ? G.wcells : G.cells).values()];
}

async function loadIntradayCell(cell) {
  const d = await api(`/api/intraday?symbol=${encodeURIComponent(S.symbol)}`);
  cell.body.querySelector(".cell-empty")?.remove();
  if (!d.points.length) {
    const empty = document.createElement("div");
    empty.className = "cell-empty";
    empty.textContent = "没有当日数据";
    cell.body.appendChild(empty);
    cell.series.setData([]); cell.avg?.setData([]);
    cell.points = [];
    cell.head.textContent = "";
    return;
  }
  const t = (p) => chartTime(p.ts, S.symbol);
  const rows = d.points.map((p) => ({ time: t(p), value: p.price }));
  cell.series.setData(rows);
  cell.points = rows;
  // 均价线只画算得出来的那些点：成交额缺失时是 None，**不能用收盘价冒充**（ADR-0006）
  cell.avg?.setData(d.points.filter((p) => p.avg !== null)
    .map((p) => ({ time: t(p), value: p.avg })));
  const last = d.points.at(-1);
  cell.head.textContent =
    `${num(last.price)}　均 ${last.avg === null ? "—" : num(last.avg)}　${d.trading_day || ""}`;
  cell.chart.timeScale().fitContent();
}

async function loadCell(cell) {
  if (cell.tf === INTRADAY) return loadIntradayCell(cell);
  const q = `symbol=${encodeURIComponent(S.symbol)}&timeframe=${cell.tf}&ma=${MA_CELL}&vma=${VMA_CELL}`;
  const [data, marks] = await Promise.all([
    api(`/api/bars?${q}&limit=${GRID_BARS}`),
    api(`/api/markers?${q}`).catch(() => ({ markers: [], fills: [] })),
  ]);
  cell.body.querySelector(".cell-empty")?.remove();
  if (!data.bars.length) {
    const empty = document.createElement("div");
    empty.className = "cell-empty";
    empty.textContent = `没有 ${cell.tf} 数据`;
    empty.textContent = `没有 ${cell.tf} 数据`;
    cell.body.appendChild(empty);
    cell.series.setData([]);
    cell.layer?.setData({ groups: [], fills: [], trades: [], selected: null });
    cell.points = [];
    cell.head.textContent = "";
    return;
  }
  const rows = data.bars.map((b) => ({
    time: chartTime(b.close_ts, S.symbol),
    open: b.open, high: b.high, low: b.low, close: b.close,
  }));
  cell.series.setData(rows);
  cell.bars = data.bars;   // 预警组的头部要用它算当日涨跌
  cell.points = rows.map((r) => ({ time: r.time, value: r.close }));
  // 同一条信号在 1m 和 1d 上落的位置不同，那正是"看大做小"要看的东西。
  // 但 **bar 太少时不画**：lightweight-charts 的标注尺寸随 bar 宽度缩放，
  // 只有一两根 bar 时每根极宽、箭头被撑到半个格子，反而把 K 线盖住，
  // 还会让人误以为那是个特别重要的信号。数据攒够了自然就会出现。
  // 九宫格也走自绘层，徽章同样锚在触发价上 —— 两处用两套画法，
  // 同一条信号在单图和格子里会落在不同高度，那是最容易被读错的一种不一致。
  // 格子里**只画徽章**：成交胶囊在 1/9 屏宽里挤不下，那是单图的事。
  const shown = new Set(data.bars.map((b) => b.close_ts));
  cell.layer.setData({
    symbol: S.symbol,
    groups: data.bars.length < MARKER_MIN_BARS ? []
      : (marks.markers || []).filter((m) => shown.has(m.bucket_ts)),
    fills: [], trades: [], selected: null,
  });
  const legend = drawOverlays(cell.chart, cell.series, data,
    { maStore: "maSeries", volume: cell.volume, host: cell });
  const last = data.bars.at(-1);
  // 均线值写进格子标题：小格子里挤不下图例，但不写就不知道哪条是哪条
  cell.head.innerHTML = `${num(last.close)}　`
    + legend.filter((l) => l.value !== null && l.value !== undefined)
        .map((l) => `<span style="color:${l.color}">${esc(l.label)} ${num(l.value)}</span>`)
        .join("　")
    + `　<span class="lbl">${data.total} 根</span>`;
  cell.chart.timeScale().fitContent();
}

async function loadGrid() {
  if (!S.symbol || !S.meta) return;
  const tfs = GRID_TFS;
  const cells = tfs.map(gridCell);
  delete $("#chart-note").dataset.base;   // 上一次锁定留下的后缀不该粘到新文案上
  $("#chart-note").textContent =
    `${shortSym(S.symbol)}　九宫格：${tfs.join(" / ")}　每格最多 ${GRID_BARS} 根`
    + `　悬停 = 九格同步十字线　shift+单击 = 锁定该时刻　双击 = 放大　Esc = 返回`;
  await Promise.all(cells.map((c) => loadCell(c).catch((e) => {
    c.head.textContent = "加载失败";
    console.error(c.tf, e);
  })));
  requestAnimationFrame(() => cells.forEach(fitCell));
}

/* Esc：先解锁十字线，再退出放大 —— 两件事都是"我想回到上一层"。
   放大后没有可见的关闭按钮，只能双击，这在全屏状态下很不直觉（用户反馈）。 */
function onGridKey(ev) {
  if (ev.key !== "Escape" || !G.on) return;
  if (G.pinned !== null) {
    G.pinned = null;
    syncCrosshair(null, null);
    renderPinBadge();
    return;
  }
  if (G.zoomed) zoomCell(G.zoomed);
}

/* ── 预警组 ─────────────────────────────────────────
 *
 * 组由**服务端算**（web/watchlist.py）：钉住的 ∪ 最近触发的，取前九。
 * 前端只负责画，以及"已读"这一件本地状态。
 */
async function loadWatch() {
  const d = await api("/api/watchlist");
  G.wl = d;
  renderWlTabs();
  const market = d.markets.find((m) => m.key === G.market) || d.markets[0];
  G.market = market.key;
  const entries = market.entries.slice(0, d.slots);

  // 组里没有的格子要从 DOM 里摘掉，否则切市场后上一组的标的还挂着
  const keep = new Set(entries.map((e) => e.symbol));
  for (const [uid, cell] of G.wcells) {
    if (!keep.has(uid)) { cell.root.remove(); G.wcells.delete(uid); }
  }
  // 空槽自己记着，靠选择器找回来再删太脆（选择器一改就静默漏删，格子越堆越多）
  for (const el of G.slots) el.remove();
  G.slots = [];

  const cells = entries.map((e) => {
    const cell = watchCell(e.symbol);
    cell.entry = e;
    $("#grid").appendChild(cell.root);      // 重新 append = 按组的顺序排位
    paintWatchHead(cell, e);
    return cell;
  });
  // 空槽：看着像「留着位子」，不像「坏了」
  for (let i = entries.length; i < d.slots; i += 1) {
    const slot = document.createElement("div");
    slot.className = "cell";
    slot.innerHTML = `<div class="cell-slot">等待信号</div>`;
    $("#grid").appendChild(slot);
    G.slots.push(slot);
  }

  const over = market.pinned_over_slots;
  $("#chart-note").textContent =
    `${market.label}预警组：${entries.length} / ${d.slots} 格`
    + `　周期 ${G.wtf}　钉住的不会被新信号挤掉`
    + (over ? `　⚠️ 钉住了 ${entries.length} 个，超出 ${over} 个仍会显示但挤到了后面` : "")
    + (d.local_only ? "" : "　（面板未绑回环，钉住已禁用）");

  await Promise.all(cells.map((c) => loadWatchCell(c).catch((e) => {
    c.head.textContent = "加载失败";
    console.error(c.key, e);
  })));
  requestAnimationFrame(() => cells.forEach(fitCell));
}

/* 格子头部：标的名、钉图标、价格、触发理由。
   未读用**三重信号**：整格内描边 + 头部一条竖条 + 规则名转蓝。 */
function paintWatchHead(cell, e) {
  const unread = !!e.dedup_key && !G.seen.has(e.dedup_key);
  cell.root.classList.toggle("unread", unread);
  cell.name.textContent = shortSym(e.symbol);
  cell.pin.classList.toggle("on", e.pinned);
  cell.pin.disabled = G.wl ? !G.wl.local_only : false;
  cell.pin.onclick = (ev) => { ev.stopPropagation(); togglePin(e); };
  // 没有信号的钉住项显示「手动钉住」，不编一条不存在的规则出来
  cell.why.textContent = e.rule_id || "手动钉住";
  cell.ago.textContent = e.fired_at ? ago(e.fired_at) : (e.watched ? "等待触发" : "无规则");
  // 点开即已读。**没有这个状态，扫第二遍时分不清哪个是新的。**
  cell.root.onclick = (ev) => {
    if (ev.shiftKey) return;             // shift+单击留给锁定十字线
    if (!e.dedup_key) return;
    markSeen(e.dedup_key);
    paintWatchHead(cell, e);
    renderWlTabs();
  };
}

/* 相对时间。绝对时刻在放大后的图里看，格子里只要"多久之前"。 */
function ago(ts) {
  const s = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return `${Math.floor(s / 86400)} 天前`;
}

function renderWlTabs() {
  const box = $("#wl-tabs");
  if (!box || !G.wl) return;
  box.innerHTML = G.wl.markets.map((m) => {
    const n = m.entries.filter((e) => e.dedup_key && !G.seen.has(e.dedup_key)).length;
    // 未读数只在有未读时出现：常驻一个 0 会让人以为一直有东西没看
    return `<button class="wl-tab${m.key === G.market ? " active" : ""}" data-k="${esc(m.key)}">`
      + `<span>${esc(m.label)}</span>`
      + (n ? `<span class="wl-n">${n}</span>` : "")
      + `</button>`;
  }).join("");
  $$("#wl-tabs .wl-tab").forEach((el) => {
    el.onclick = async () => { G.market = el.dataset.k; await loadWatch(); };
  });
}

async function togglePin(e) {
  try {
    if (e.pinned) await api(`/api/watchlist/pin?symbol=${encodeURIComponent(e.symbol)}`,
                            { method: "DELETE" });
    else await send("/api/watchlist/pin", { symbol: e.symbol });
  } catch (err) {
    toast(String(err.message || err), "bad");
    return;
  }
  await loadWatch();
}

/* 一格的行情。与周期模式共用 loadCell —— 那边已经处理好均线、成交量、
   标记层与空状态，这里只需要把格子的周期指到整组选定的那个。 */
async function loadWatchCell(cell) {
  cell.tf = G.wtf;
  const e = cell.entry;
  // **没有规则盯 ⇒ 盯盘进程不采集 ⇒ 图永远是空的。** 这条要当场说清楚，
  // 否则又是一个静默的空（这个项目在这上面栽过：用户以为是行情连不上）。
  if (!e.watched) {
    cell.series.setData([]);
    cell.layer?.setData({ groups: [], fills: [], trades: [], selected: null });
    cell.points = [];
    cell.head.textContent = "";
    let empty = cell.body.querySelector(".cell-empty");
    if (!empty) {
      empty = document.createElement("div");
      empty.className = "cell-empty";
      cell.body.appendChild(empty);
    }
    empty.innerHTML = `没有规则盯 ${esc(shortSym(e.symbol))}<br>盯盘进程不采集它的行情`;
    return;
  }
  const prev = S.symbol;
  S.symbol = e.symbol;                 // loadCell 按 S.symbol 取数
  try { await loadCell(cell); } finally { S.symbol = prev; }
  // **头部改写成最新价 + 当日涨跌**，覆盖掉 loadCell 写进去的均线图例。
  // 预警组的格子头部已经有标的名和钉图标，再塞两个均线值就会换行 ——
  // 这个坑在 chart-head 上踩过两次（价均线一次、量均线一次）。
  // 而且横着扫九个标的时，要的是"现在多少钱、涨还是跌"，不是 SMA 值。
  paintWatchPrice(cell);
}

/* 当日涨跌：以**本交易日第一根的开盘**为基准。

   窗口没覆盖到今天开盘时（休市后看昨天的图、或格子周期太小）给破折号，
   **不拿窗口首根凑数** —— 那个数看着像涨跌幅，其实是"最近 18 小时的涨跌"，
   量级和含义都不对，比不显示危险得多。 */
function paintWatchPrice(cell) {
  const bars = cell.bars || [];
  const last = bars[bars.length - 1];
  if (!last) { cell.head.textContent = ""; return; }
  const day = last.trading_day;
  const first = day ? bars.find((b) => b.trading_day === day) : null;
  const base = first ? first.open : null;
  const chg = base ? (last.close - base) / base * 100 : null;
  const cls = chg === null ? "dim" : (chg >= 0 ? "pos" : "neg");
  const text = chg === null ? "—" : `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%`;
  cell.head.innerHTML = `<span>${esc(num(last.close))}</span>`
    + `<span class="${cls}" style="margin-left:6px">${esc(text)}</span>`;
}

async function toggleGrid(want) {
  G.on = want === undefined ? !G.on : want;
  $("#grid").hidden = !G.on;
  $("#chart").hidden = G.on;
  $("#grid-toggle").classList.toggle("active", G.on);
  $("#grid-mode").hidden = !G.on;
  // 周期按钮在两种模式下含义不同：周期模式里由格子决定（藏起来），
  // 预警组里是**整组一个周期**（显示，但绑的是 G.wtf 不是 S.timeframe）
  $("#tf-group").hidden = G.on;
  $("#wl-tabs").hidden = !(G.on && G.mode === "watch");
  if (!G.on) { G.pinned = null; G.zoomed = null; }
  if (G.on) await renderGrid();
  else { ensureChart(); await loadChart(S.selected?.fired_at); }
}

/* 切模式时把另一种模式的格子从 DOM 里摘掉（不销毁图表，切回来还要用）。
   销毁重建九个图表只为切一次模式太浪费，而模式是会来回切的。 */
async function renderGrid() {
  const watch = G.mode === "watch";
  for (const c of G.cells.values()) c.root.remove();
  for (const c of G.wcells.values()) c.root.remove();
  for (const el of G.slots) el.remove();
  G.slots = [];
  $("#wl-tabs").hidden = !watch;
  renderGridMode();
  if (watch) await loadWatch();
  else {
    for (const c of G.cells.values()) $("#grid").appendChild(c.root);
    await loadGrid();
  }
}

function renderGridMode() {
  const box = $("#grid-mode");
  if (!box) return;
  const modes = [["watch", "预警组 · 九标的"], ["tf", "九周期 · 单标的"]];
  const tfs = G.mode === "watch"
    ? ["1m", "5m", "15m", "30m", "1h"].map((t) =>
        `<button class="tf${t === G.wtf ? " active" : ""}" data-wtf="${t}">${t}</button>`)
    : [];
  box.innerHTML = modes.map(([k, label]) =>
    `<button class="tf${k === G.mode ? " active" : ""}" data-mode="${k}">${label}</button>`)
    .concat(tfs.length ? [`<span class="seg-lbl">周期</span>`] : [])
    .concat(tfs).join("");
  $$("#grid-mode [data-mode]").forEach((el) => {
    el.onclick = async () => {
      if (el.dataset.mode === G.mode) return;
      G.mode = el.dataset.mode;
      G.pinned = null; G.zoomed = null;
      await renderGrid();
    };
  });
  $$("#grid-mode [data-wtf]").forEach((el) => {
    el.onclick = async () => {
      if (el.dataset.wtf === G.wtf) return;
      G.wtf = el.dataset.wtf;
      renderGridMode();
      await loadWatch();
    };
  });
}

/* ── 信号提醒：弹窗 / 声音 / 语音播报 / 桌面通知 ────────────────
 *
 * 四条不可违反的约束（每条都能单独把这个功能毁掉）：
 * 1. **只有新信号才提醒**。开机时 /api/signals 会灌进几十条历史信号，
 *    那些绝不能响 —— 只认 SSE 推来的。
 * 2. **一批同时到达只响一次**。多标的同刻收盘很常见，一根 bar 响五声等于噪音。
 * 3. **AudioContext 必须由用户手势创建**（浏览器自动播放策略）。
 *    点"声音"这个开关本身就是那个手势，所以在开关的 onclick 里建/恢复。
 * 4. **每个浏览器 API 都可能不存在或被拒绝**（无痕模式、非 https、用户拒绝授权、
 *    桩化冒烟的沙箱）。一律 try/catch + 能力探测，任何一个缺失都不能让面板起不来。
 */
const ALERT_KEYS = ["toast", "sound", "speech", "desktop"];
const A = {
  on: { toast: true, sound: false, speech: false, desktop: false },  // 声音默认关：
  // 一打开页面就开始响是很讨厌的，而且没有用户手势也放不出来（见约束 3）
  audio: null,
  queue: [],
  timer: null,
};

function loadAlertPrefs() {
  try {
    const raw = localStorage.getItem("sigdesk.alerts");
    if (raw) Object.assign(A.on, JSON.parse(raw));
  } catch { /* 无痕模式 / 禁用 storage：用默认值，不该因此崩掉 */ }
}

function saveAlertPrefs() {
  try { localStorage.setItem("sigdesk.alerts", JSON.stringify(A.on)); } catch { /* 同上 */ }
}

function renderAlertOpts() {
  $$("#alerts .opt").forEach((b) => {
    const k = b.dataset.alert;
    b.classList.toggle("on", !!A.on[k]);
    if (k === "desktop") {
      const denied = typeof Notification !== "undefined" && Notification.permission === "denied";
      b.classList.toggle("denied", denied);
      b.title = denied ? "浏览器已拒绝通知权限，需要在地址栏的站点设置里改" : "";
    }
  });
}

/* 短促提示音。用 Web Audio 现场合成，不引外部音频文件 —— ADR-0009 无构建单页。
   多头上行两声、空头下行两声、中性单声，闭着眼也能听出方向。 */
function beep(direction) {
  if (!A.audio) return;
  const notes = direction === "long" ? [660, 880]
    : direction === "short" ? [660, 440] : [620];
  notes.forEach((f, i) => {
    const t = A.audio.currentTime + i * 0.13;
    const osc = A.audio.createOscillator();
    const gain = A.audio.createGain();
    osc.type = "sine";
    osc.frequency.value = f;
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.16, t + 0.012);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.12);
    osc.connect(gain).connect(A.audio.destination);
    osc.start(t);
    osc.stop(t + 0.13);
  });
}

function speak(text) {
  try {
    if (typeof speechSynthesis === "undefined") return;
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "zh-CN";
    u.rate = 1.05;
    speechSynthesis.speak(u);
  } catch { /* 没有语音引擎就算了，不该影响其它提醒 */ }
}

const signalWords = (s) =>
  `${shortSym(s.symbol)} ${DIR[s.direction]?.label || "提示"}，${num(s.trigger_price)}`;

function pushToast(s) {
  const box = $("#toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = `toast ${s.direction}`;
  el.innerHTML = `
    <div class="toast-top">${dirIcon(s.direction, 12)}
      <span class="sym mono">${esc(shortSym(s.symbol))}</span>
      <span class="price mono ${DIR[s.direction]?.cls || ""}">${num(s.trigger_price)}</span></div>
    <div class="toast-sub"><span class="lbl">${esc(s.rule_id)}</span>
      <span class="lbl" style="margin-left:auto">${fmtTime(s.fired_at, s.symbol)}</span></div>`;
  // 点弹窗 = 跳到那条信号（选中 + 图表定位），这是它最有用的动作
  el.onclick = () => { el.remove(); select(s); };
  box.appendChild(el);
  setTimeout(() => el.remove(), 15000);
  // 只留最近 4 条，再多就折叠 —— 一屏弹窗把图表盖住比不提醒还糟
  const items = $$("#toasts .toast");
  if (items.length > 4) items.slice(0, items.length - 4).forEach((x) => x.remove());
}

/* 一批信号合并成一次提醒（约束 2）。SSE 逐条推，所以用一个短窗口攒一下。 */
function alertSignals(list) {
  if (!list.length) return;
  if (A.on.toast) {
    list.slice(0, 4).forEach(pushToast);
    if (list.length > 4) {
      const el = document.createElement("div");
      el.className = "toast-more";
      el.textContent = `另有 ${list.length - 4} 条`;
      $("#toasts")?.appendChild(el);
      setTimeout(() => el.remove(), 8000);
    }
  }
  const lead = list[0];
  if (A.on.sound) beep(lead.direction);
  if (A.on.speech) {
    speak(list.length === 1 ? signalWords(lead)
      : `${signalWords(lead)}，另有 ${list.length - 1} 条信号`);
  }
  if (A.on.desktop) {
    try {
      if (typeof Notification !== "undefined" && Notification.permission === "granted") {
        const n = new Notification(
          list.length === 1 ? `${shortSym(lead.symbol)} ${DIR[lead.direction]?.label || ""}`
            : `${list.length} 条新信号`,
          { body: `${lead.rule_id}　${num(lead.trigger_price)}`, tag: "sigdesk" });
        n.onclick = () => { window.focus(); select(lead); n.close(); };
      }
    } catch { /* 通知构造失败（如非安全上下文）不该影响别的提醒 */ }
  }
}

function queueAlert(signal) {
  A.queue.push(signal);
  if (A.timer) return;
  // 400ms 窗口：同刻收盘的一批 bar 产生的信号会在这个窗口内陆续到达
  A.timer = setTimeout(() => {
    const batch = A.queue.slice();
    A.queue = [];
    A.timer = null;
    try { alertSignals(batch); } catch (e) { console.error("提醒失败", e); }
  }, 400);
}

async function toggleAlert(key) {
  const want = !A.on[key];
  if (want && key === "sound") {
    // 约束 3：AudioContext 必须在用户手势里创建/恢复，否则被自动播放策略静音
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      A.audio = A.audio || new Ctx();
      if (A.audio.state === "suspended") await A.audio.resume();
    } catch { return; }
  }
  if (want && key === "desktop") {
    try {
      if (typeof Notification === "undefined") return;
      const perm = Notification.permission === "granted"
        ? "granted" : await Notification.requestPermission();
      if (perm !== "granted") { renderAlertOpts(); return; }  // 被拒就别装作开了
    } catch { return; }
  }
  A.on[key] = want;
  saveAlertPrefs();
  renderAlertOpts();
  if (want && (key === "sound" || key === "speech")) {
    // 开的时候立刻示范一下，否则你不知道它到底会不会响
    if (key === "sound") beep("long");
    else speak("信号播报已开启");
  }
}

function initAlerts() {
  loadAlertPrefs();
  A.on.desktop = A.on.desktop
    && typeof Notification !== "undefined" && Notification.permission === "granted";
  $$("#alerts .opt").forEach((b) => { b.onclick = () => toggleAlert(b.dataset.alert); });
  renderAlertOpts();
}

function connectSSE() {
  const es = new EventSource("/api/events");
  es.addEventListener("signal", async (ev) => {
    const sig = JSON.parse(ev.data);
    S.signals.push(sig);
    queueAlert(sig);   // **只有 SSE 推来的才提醒**；开机灌进来的历史信号不响
    fillSymbolPicker();  // 某标的第一次触发时，它才会进「有信号」那一组
    fillRuleFilter();  // 新规则第一次触发时，它才会出现在筛选框里
    renderFeed();
    if (S.symbol) await loadChart(S.selected ? S.selected.fired_at : null);
    setBadge("#mode", "实时 · 已连接", "ok");
  });
  es.addEventListener("hello", () => setBadge("#mode", "实时 · 已连接", "ok"));
  es.onerror = () => setBadge("#mode", "实时 · 断开，重连中", "warn");
}

async function refreshBadges() {
  try {
    const h = await api("/api/health");
    if (h.healthy === null) setBadge("#flows", "无实时数据流", "");
    else {
      const bad = (h.feeds || []).filter((f) => !f.connected).length;
      setBadge("#flows", `${(h.feeds || []).length} 路数据流 · ${h.total_gaps ?? 0} 缺口`,
        bad || !h.healthy ? "warn" : "ok");
    }
  } catch { setBadge("#flows", "健康：取不到", "bad"); }
}

/* ── 启动 ───────────────────────────────────────── */


/* ── 规则编辑与历史试算（FR-5.3）────────────────── */
/* 校验、试算、区间统统在服务端算 —— 写在这里的逻辑测不到。
   这一层只负责：发请求、把后端给的话原样显示出来、把区间画成条。 */

const R = { list: [], editable: false, current: null, saved: "", creating: false };

const TEMPLATE = (uid) => `id: my-rule
description: 说点人话，方便以后想起来这条规则是干嘛的
universe: [${uid || "CRYPTO.OKX.BTCUSDT.PERP"}]
timeframes:
  trend: 1h
  trigger: 5m
conditions:
  # 顺序即链路顺序，最后一条是扳机
  - on: trend
    mode: state
    when: close > ema(close, 60)
  - on: trigger
    mode: event
    when: cross_up(close, ema(close, 20))
context:
  atr14: atr(14)
emit:
  direction: long
  dedup_key: "{symbol}:{rule}:{trend_bar_close_ts}"
`;

function ruleMsg(text, kind) {
  const el = $("#rule-msg");
  el.textContent = text || "";
  el.className = "rule-msg" + (kind ? " " + kind : "");
}

function markDirty() {
  const dirty = R.current !== null && $("#rule-src").value !== R.saved;
  $("#rule-dirty").hidden = !dirty;
}

function renderRuleList() {
  $("#rule-count").textContent = R.list.length ? `${R.list.length} 条` : "";
  $("#rule-items").innerHTML = R.list.length
    ? R.list.map((r) => `
      <div class="rule-item${r.id === R.current ? " active" : ""}${r.enabled ? "" : " off"}"
           data-id="${esc(r.id)}">
        <div class="id mono">${esc(r.id)}</div>
        <div class="meta">
          ${dirIcon(r.direction)}
          <span class="lbl">${esc(r.timeframe)} · ${r.levels.length} 级</span>
        </div>
      </div>`).join("")
    : `<p class="empty">还没有规则</p>`;
  $$("#rule-items .rule-item").forEach((el) => el.onclick = () => openRule(el.dataset.id));
}

async function openRule(id) {
  try {
    const body = await api(`/api/rules/${encodeURIComponent(id)}/source`);
    R.current = id; R.creating = false; R.saved = body.source;
    $("#rule-src").value = body.source;
    $("#rule-title").textContent = id;
    ruleMsg("");
    renderRuleList(); markDirty(); setRuleButtons();
  } catch (e) { ruleMsg(e.message, "bad"); }
}

function newRule() {
  R.current = null; R.creating = true; R.saved = "";
  $("#rule-src").value = TEMPLATE(S.symbol);
  $("#rule-title").textContent = "新建规则";
  ruleMsg("先「校验」看语法，再「历史试算」看它在过去会怎样，最后才保存。");
  renderRuleList(); markDirty(); setRuleButtons();
}

function setRuleButtons() {
  const has = R.current !== null || R.creating;
  $("#rule-src").disabled = !R.editable && !has;
  for (const [sel, on] of [
    ["#rule-new", R.editable],
    ["#rule-validate", has],
    ["#rule-trial", has && R.editable],
    ["#rule-save", has && R.editable],
    ["#rule-delete", R.current !== null && R.editable],
  ]) $(sel).disabled = !on;
}

async function validateRule() {
  try {
    const body = await send("POST", "/api/rules/validate", { source: $("#rule-src").value });
    if (body.ok) {
      const r = body.rule;
      ruleMsg(`✓ 语法通过 · ${r.id} · 扳机 ${r.timeframe} · ${r.levels.length} 级 · `
        + `${r.universe.length} 个标的`, "ok");
    } else ruleMsg(body.error, "bad");
    return body.ok;
  } catch (e) { ruleMsg(e.message, "bad"); return false; }
}

async function saveRule() {
  const source = $("#rule-src").value;
  try {
    const body = R.creating
      ? await send("POST", "/api/rules", { source })
      : await send("PUT", `/api/rules/${encodeURIComponent(R.current)}`, { source });
    R.current = body.id; R.creating = false; R.saved = source;
    $("#rule-title").textContent = body.id;
    ruleMsg(`✓ 已保存 ${body.id}。\n注意：只读面板已认得这条规则，但**信号要盯盘进程产生** ——`
      + `重启 watch.py 后它才会真正开始盯。想先看它在历史上的表现，用「历史试算」。`, "ok");
    await loadRules();
    await refreshMeta();
    markDirty();
  } catch (e) { ruleMsg(e.message, "bad"); }
}

async function deleteRule() {
  const id = R.current;
  if (!id || !confirm(`删除规则 ${id}？\n（会移进 _trash/ 归档，不是真删）`)) return;
  try {
    const body = await send("DELETE", `/api/rules/${encodeURIComponent(id)}`);
    R.current = null; R.saved = ""; $("#rule-src").value = ""; $("#rule-title").textContent = "—";
    ruleMsg(`已归档到 ${body.archived_to}`, "ok");
    await loadRules();
    await refreshMeta();
    setRuleButtons(); markDirty();
  } catch (e) { ruleMsg(e.message, "bad"); }
}

/* 各级别条件成立区间：绿=成立，灰=未知（预热期），空=不成立。
   蓝色竖线是信号。“为什么这一刻没触发”看的就是这张图。 */
/* 每一级判过多少根、成立几根。空区间时这张表就是全部答案：
   全 unknown = 指标还没预热完；全 false = 条件本身太严。两者的下一步完全不同。 */
function renderTally(counts) {
  const roles = Object.keys(counts || {});
  if (!roles.length) return `<p class="empty">这段历史里没有一根 bar 走到过这条规则</p>`;
  const rows = roles.map((role) => {
    const t = counts[role];
    const n = t.true + t.false + t.unknown;
    return `<tr><td class="mono">${esc(role)}</td>
      <td class="mono ${t.true ? "pos" : "dim"}">${t.true}</td>
      <td class="mono dim">${t.false}</td>
      <td class="mono ${t.unknown ? "warn" : "dim"}">${t.unknown}</td>
      <td class="mono dim">${n}</td></tr>`;
  }).join("");
  const allUnknown = roles.every((r) => counts[r].true === 0 && counts[r].false === 0);
  return `<table><thead><tr><th>级别</th><th>成立</th><th>不成立</th>
      <th>未知</th><th>判过</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="callout">${allUnknown
      ? "全部是「未知」= 指标还没预热完，不是规则太严。把区间往前拉长再试。"
      : "没有任何一级成立过。先放宽最严的那一级，或确认阈值口径（ADR-0006）与看盘软件一致。"}</div>`;
}

function renderBands(uid, bands, signals, counts) {
  const all = [...bands.map((b) => b.from_ts), ...bands.map((b) => b.to_ts)];
  if (!all.length) return renderTally(counts);
  const fired = signals.map((s) => s.fired_at);
  const lo = Math.min(...all, ...fired);
  const hi = Math.max(...all, ...fired);
  const span = Math.max(hi - lo, 1);
  const x = (ts) => ((ts - lo) / span) * 100;

  const cap = (name, sub) => `<div class="band-cap">
    <span class="mono">${esc(name)}</span><span class="lbl">${esc(sub)}</span></div>`;

  const roles = [];
  for (const b of bands) if (!roles.includes(b.role)) roles.push(b.role);
  const rows = roles.map((role) => {
    const mine = bands.filter((b) => b.role === role);
    const t = (counts || {})[role];
    const segs = mine.map((b) => {
      const left = x(b.from_ts);
      const w = Math.max(x(b.to_ts) - left, 0.4);
      return `<i class="${b.value}" style="left:${left}%;width:${w}%"
        title="${esc(role)} ${b.value === "true" ? "成立" : "未知"} ${b.bars} 根"></i>`;
    }).join("");
    const sub = t ? `${mine[0].timeframe} · 成立 ${t.true}/${t.true + t.false + t.unknown}`
                  : mine[0].timeframe;
    return `<div class="band-row">${cap(role, sub)}
      <div class="band-track">${segs}</div></div>`;
  }).join("");

  // 信号单独一行。原先画在每条轨道上，1m 那条 1000+ 根挤进几百像素，
  // 绿段和蓝线糊成一片噪声 —— 重复画同一份信息只会互相遮蔽。
  const fires = signals.map((s) =>
    `<i class="fire" style="left:${x(s.fired_at)}%" title="${esc(fmtTime(s.fired_at, uid))}"></i>`
  ).join("");
  const fireRow = `<div class="band-row">${cap("信号", `${signals.length} 条`)}
    <div class="band-track fires">${fires}</div></div>`;

  return `<div class="bands">${rows}${fireRow}</div>
    <div class="legend" style="margin-top:9px">
      <span><i class="swatch" style="background:var(--up)"></i><span class="lbl">成立</span></span>
      <span><i class="swatch" style="background:var(--dim);opacity:.4"></i>
        <span class="lbl">未知（预热期）</span></span>
      <span><i class="swatch" style="background:var(--accent)"></i><span class="lbl">信号</span></span>
    </div>`;
}

async function trialRule() {
  const params = statsParams();
  $("#trial-body").innerHTML = `<p class="empty">试算中…</p>`;
  $("#trial-sub").textContent = "";
  let body;
  try {
    body = await send("POST", "/api/rules/trial", { source: $("#rule-src").value, ...params });
  } catch (e) {
    $("#trial-body").innerHTML = `<p class="empty">${esc(e.message)}</p>`;
    return;
  }
  const o = body.report.overall;
  $("#trial-sub").textContent = `${body.bars_scanned} 根 1m · ${body.signals.length} 条信号`;

  const missing = body.symbols_without_data.length
    ? `<div class="callout">这些标的没有已落盘的数据，本次未参与：
        ${esc(body.symbols_without_data.join(", "))}<br>
        用 <span class="mono">scripts/backfill.py</span> 回补后再试算。</div>` : "";

  const bandsBySym = body.condition_bands;
  const bandBlocks = Object.keys(bandsBySym).map((uid) => `
    <div>
      <div class="sec-title"><b>${esc(shortSym(uid))}</b>
        <span class="lbl">各级别条件成立区间</span></div>
      ${renderBands(uid, bandsBySym[uid],
        body.signals.filter((s) => s.symbol === uid), (body.condition_counts || {})[uid])}
    </div>`).join("");

  const hero = (label, value, klass) =>
    `<div class="hero"><div class="lbl">${esc(label)}</div>
      <div class="v mono ${klass || ""}">${value}</div></div>`;
  // 0 条信号时"胜率 0.0%""期望收益 +0.000%"是彻头彻尾的误导 —— 那不是不赚钱，是算不出来
  const none = !o.evaluated;

  $("#trial-body").innerHTML = `
    ${missing}
    <div class="heroes" style="grid-template-columns:repeat(2,minmax(0,1fr))">
      ${hero("触发", o.signals, o.signals ? "" : "dim")}
      ${hero("胜率", none ? "—" : pct(o.win_rate), none ? "dim" : cls(o.win_rate - 0.5))}
      ${hero("期望收益", none ? "—" : signed(o.avg_return), none ? "dim" : cls(o.avg_return))}
      ${hero("盈亏比", none || !o.payoff ? "—" : num(o.payoff), none ? "dim" : "")}
    </div>
    ${bandBlocks}
    <div class="callout">口径：持有 ${params.horizon_bars} 根 · 止损
      ${(params.stop_pct * 100).toFixed(2)}% · 止盈 ${(params.target_pct * 100).toFixed(2)}%
      · 单边 ${params.cost_bps}bp。<br>
      试算不落盘、不推送、不下单；用的是与实盘同一个引擎（ADR-0001）。</div>`;
}

/* 信号流的「规则」筛选框。**必须能重填** —— 它的数据来自页面加载时拉的一次 /api/meta，
   在「规则」页新建一条规则后不重填，那条规则就要刷新整个页面才看得到（踩过）。 */
/* 图表的标的下拉框。**分两组**：

   上面「有信号」—— 这才是你平时要切的（跟信号流、预警组是同一批标的）；
   下面「其他已注册」—— 从没预警过的，用来确认"是规则太严还是行情真没走出形态"。
   两类混在一起时，冷启动看到的是一串一模一样的选项，点进去全是空图。

   每一项都标出**为什么点进去可能是空的**：
     · 无数据      本地一根 bar 都没有（行情没接入 / 没回补）
     · 数据止于 X  有数据但停更了（主连这种派生序列不随盘更新）
     · 未盯        没有规则盯它 ⇒ 盯盘进程根本不采它
     · 主连        拼接序列，可看图可回测但**不可下单**
   前三个都是"选中会是空图"的原因。不标出来的话，用户只会以为面板坏了。 */
function fillSymbolPicker() {
  const keep = $("#c-symbol").value;
  const counts = new Map();
  for (const s of S.signals || []) counts.set(s.symbol, (counts.get(s.symbol) || 0) + 1);
  const today = new Date().toISOString().slice(0, 10);

  const label = (s) => {
    const tags = [];
    if (s.is_continuous) tags.push("主连");
    if (!s.last_day) tags.push("无数据");
    else if (s.last_day < prevDays(today, 3)) tags.push(`数据止于 ${s.last_day.slice(5)}`);
    if (s.watched === false && !s.is_continuous) tags.push("未盯");
    const n = counts.get(s.uid) || 0;
    return `<option value="${esc(s.uid)}">${esc(shortSym(s.uid))}`
      + (n ? `（${n}）` : "")
      + (tags.length ? " · " + tags.map(esc).join(" · ") : "")
      + `</option>`;
  };

  const fired = S.meta.symbols.filter((s) => counts.has(s.uid));
  const rest = S.meta.symbols.filter((s) => !counts.has(s.uid));
  $("#c-symbol").innerHTML =
    (fired.length ? `<optgroup label="有信号">${fired.map(label).join("")}</optgroup>` : "")
    + (rest.length ? `<optgroup label="其他已注册">${rest.map(label).join("")}</optgroup>` : "");
  if (keep) $("#c-symbol").value = keep;

  // **筛选框只列筛得出东西的标的。** 旁边的「全部规则」一直是按信号条数生成的，
  // 这个却是照抄注册表 —— 于是筛选项和被筛的数据不同源，选一个从没触发过的
  // 标的必然是空列表，而下拉框刚才还让你以为那里有东西。
  const keepF = $("#f-symbol").value;
  const uids = [...counts.keys()].sort();
  $("#f-symbol").innerHTML = '<option value="">全部标的</option>'
    + uids.map((u) => `<option value="${esc(u)}">${esc(shortSym(u))}（${counts.get(u)}）</option>`)
      .join("");
  $("#f-symbol").value = keepF;
}

/* 往前推 n 天的 YYYY-MM-DD。判"停更"用，不需要日历精度 —— 周末休市两天，
   所以阈值取 3 天，否则每个周末所有期货都会被标成"数据止于"。 */
function prevDays(iso, n) {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() - n);
  return d.toISOString().slice(0, 10);
}

/* 一个标的都没有数据：这不是错误，是**还没开始用**。
   说清楚下一步做什么，而不是留一张空图让人以为坏了。 */
function showNoDataAtAll() {
  const box = $("#chart");
  if (!box) return;
  const el = document.createElement("div");
  el.className = "chart-empty";
  el.innerHTML = `<div>本地还没有任何行情数据。<br><br>`
    + `期货：先回补历史 <span class="mono">scripts/backfill.py &lt;标的&gt; &lt;起&gt; &lt;止&gt;</span><br>`
    + `加密：跑 <span class="mono">scripts/watch.py --crypto-only --web 127.0.0.1:8000</span>`
    + `（不需要凭据）<br><br>`
    + `<span class="lbl">回补/盯盘跑起来之后刷新本页即可。</span></div>`;
  box.appendChild(el);
  $("#chart-note").textContent = "没有行情数据";
}

function fillRuleFilter() {
  const keep = $("#f-rule").value;
  // **信号流里的规则 ∪ 当前加载的规则**，不能只取后者：
  // 库里的历史信号可能来自已经删掉的规则（实测 50 条里 44 条来自 multi-level-verify，
  // 而它早已不在 config/rules 里）。只列已加载的，那 44 条就永远筛不出来；
  // 而列表里的规则可能一条信号都没有，选中是空的 —— 两种情况都要能一眼看出来。
  const counts = new Map();
  for (const s of S.signals || []) counts.set(s.rule_id, (counts.get(s.rule_id) || 0) + 1);
  const loaded = new Set((S.meta?.rules || []).map((r) => r.id));
  const ids = [...new Set([...loaded, ...counts.keys()])].sort();
  $("#f-rule").innerHTML = '<option value="">全部规则</option>' + ids.map((id) => {
    const n = counts.get(id) || 0;
    const tag = loaded.has(id) ? "" : " · 已下线";
    return `<option value="${esc(id)}">${esc(id)}（${n}）${tag}</option>`;
  }).join("");
  $("#f-rule").value = keep;   // 重填不该把用户选中的筛选项冲掉
}

/* 规则增删改之后重新拉一次 meta，让盯盘页的筛选框跟着变。 */
async function refreshMeta() {
  try {
    S.meta = await api("/api/meta");
    fillRuleFilter();
  } catch { /* meta 拉不到不该影响刚保存成功这件事 */ }
}

async function loadRules() {
  try {
    const body = await api("/api/rules");
    R.list = body.rules;
    R.editable = body.editable;
    renderRuleList();
    const banner = $("#rule-banner");
    if (body.editable) { banner.innerHTML = ""; banner.className = ""; }
    else {
      banner.className = "banner warn";
      banner.textContent = body.live
        ? "盯盘进程里不能改规则：热替换会静默丢掉已布防的链路。请用 serve.py --allow-edit 编辑，改完重启盯盘进程。"
        : "只读模式。要编辑规则请用 serve.py --allow-edit 启动（面板没有鉴权，所以默认关闭）。";
    }
    setRuleButtons();
  } catch (e) { ruleMsg(e.message, "bad"); }
}

async function boot() {
  loadSeen();   // 已读集合是本地状态，早点读出来，第一次画预警组就能用上
  S.meta = await api("/api/meta");
  setBadge("#mode", S.meta.live ? "实时 · 连接中" : "独立只读", "");

  fillSymbolPicker();
  fillRuleFilter();

  $("#tf-group").innerHTML = S.meta.timeframes
    .map((t) => `<button class="tf${t === S.timeframe ? " active" : ""}" data-tf="${t}">${t}</button>`).join("");
  $$(".tf").forEach((b) => b.onclick = async () => {
    $$(".tf").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    S.timeframe = b.dataset.tf;
    await loadChart(S.selected ? S.selected.fired_at : null);   // 多周期联动
  });

  S.signals = (await api("/api/signals?limit=1000")).signals;
  // **必须在信号加载之后再填一次**：筛选框要统计每条规则的信号数，
  // 还要把"已下线但库里仍有信号"的规则补进来 —— boot 前面那次拿不到这些。
  fillRuleFilter();
  renderFeed();

  // 优先级：选中的信号 > 最新信号 > 正在被规则监视的标的 > 注册表第一个。
  // 直接取注册表第一个会选到一个规则根本没盯、本地也没数据的标的，开屏就是一片空白。
  // 冷启动时**别自作主张选一个没数据的**。原来退到"第一条规则 universe 的第一个"，
  // 那个标的多半一根 bar 都没有 —— 一打开就是空图，看着像面板坏了（用户实际撞上了）。
  // 一个都没有数据时干脆不选，让空状态自己去说明为什么空。
  const hasData = (uid) => (S.meta.symbols.find((x) => x.uid === uid) || {}).last_day;
  S.symbol = S.selected?.symbol || S.signals.at(-1)?.symbol
    || (S.meta.rules[0]?.universe || []).find(hasData)
    || (S.meta.symbols.find((x) => x.last_day) || {}).uid
    || null;
  if (S.symbol) { $("#c-symbol").value = S.symbol; await loadChart(S.selected?.fired_at); }
  else showNoDataAtAll();
  $("#c-symbol").onchange = async () => {
    S.symbol = $("#c-symbol").value; S.selected = null;
    renderFeed();
    if (G.on) await loadGrid(); else await loadChart();
  };
  $("#grid-toggle").onclick = () => toggleGrid();
  $("#f-rule").onchange = $("#f-symbol").onchange = renderFeed;
  $("#stats-form").onsubmit = loadStats;

  $("#rule-new").onclick = newRule;
  $("#rule-validate").onclick = validateRule;
  $("#rule-trial").onclick = trialRule;
  $("#rule-save").onclick = saveRule;
  $("#rule-delete").onclick = deleteRule;
  $("#rule-src").oninput = markDirty;

  $$(".tab").forEach((t) => t.onclick = async () => {
    $$(".tab").forEach((x) => x.classList.remove("active"));
    $$(".view").forEach((v) => v.classList.remove("active"));
    t.classList.add("active");
    $("#view-" + t.dataset.view).classList.add("active");
    if (t.dataset.view === "rules") await loadRules();
    if (t.dataset.view === "stats") await loadStats();
    if (t.dataset.view === "trade") await loadTrade();
    if (t.dataset.view === "ops") await loadOps();
    if (t.dataset.view === "live") {
      if (G.on) activeCells().forEach(fitCell);
      else if (S.chart) S.chart.applyOptions({
        width: $("#chart").clientWidth, height: $("#chart").clientHeight });
    }
  });

  initAlerts();
  document.addEventListener("keydown", onGridKey);
  if (S.meta.live) connectSSE();
  await refreshBadges();
  setInterval(refreshBadges, 15000);
  setInterval(() => $("#clock").textContent = new Date().toISOString().slice(11, 19) + "Z", 1000);
}

boot().catch((e) => {
  $("#app").innerHTML = `<div class="empty">面板启动失败：${esc(e.message)}<br>后端是否在跑？</div>`;
});
