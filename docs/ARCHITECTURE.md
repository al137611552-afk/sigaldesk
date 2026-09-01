# ARCHITECTURE — Signal Desk

- 版本: 0.1.0 (draft)
- 日期: 2026-08-28

## 0. 一条主线

**一份信号代码，三种跑法。** 回测(replay)、实时预警(live)、自动交易(trade) 共用 ②③④⑤ 层，
只替换最外面的 Feed（数据从哪来）和 Sink（结果去哪）。
任何"回测里有、实盘里没有"的分支逻辑都是架构缺陷。

```
              ┌───────── Feed（唯一有 IO 的入口）─────────┐
  期货 QuoteAPI 轮询 ─┐                                    │
  加密 交易所 WS    ─┼─→ ① 归一化 Bar 事件 ─→ ② BarStore（as-of 视图）
  历史 Parquet 回放 ─┘                                    │
              └──────────────────────────────────────────┘
                                   ↓
              ③ Indicator（增量）  ④ Pattern（表达式 / 结构原语 / Python插件）
                                   ↓
              ⑤ RuleEngine：多级别条件树 + 状态机 + TTL/冷却/去重
                                   ↓
              ┌──────── Sink（唯一有副作用的出口）────────┐
              │ Notifier(TG/Bark/飞书) │ SignalStore │    │
              │ Strategy → Risk → Broker(CTP / ccxt)      │
              └───────────────────────────────────────────┘
```

## 1. 三条不可违反的不变量

### INV-1 as-of 视图：从结构上杜绝未来函数

规则求值时**不允许直接拿到完整历史数组**，只能通过视图对象取数：

```python
view = bar_store.view(symbol, as_of=trigger_bar.close_ts)
view.bars("1h")      # 只返回 close_ts <= as_of 的【已收盘】bar
view.bars("1h")[-1]  # 最近一根已收盘 1h bar
```

回测与实盘调用完全相同的 `view()`。回测里 as_of 由回放时钟推进，实盘里由真实时钟推进。
因为视图会物理截断数据，**即使自定义 Python 插件写错也拿不到未来数据**。

### INV-2 已收盘判定：`bar.close_ts <= now`

Quote API 与交易所 WS 返回的**最后一根 K 线永远是进行中的**，其 OHLCV 会变（重绘根源）。
Feed 层统一打标 `closed: bool`，规则默认只消费 `closed=True` 的 bar。

判据按数据源能力分两路，**结论字段统一**：
- 期货（Quote API）：无收盘标记，只能按 `close_ts <= now` 推断（末根恒为进行中）。
- 加密（OKX）：数据源直接给 `confirm` 字段，**不依赖本地时钟**，WS 与 REST 同源同字段。
  进行中的那根在数组**首位**（OKX 降序返回），因此判据绝不能依赖位置。
盘中预报（tentative）是可选特性，且信号必须带 `tentative` 标记，不进统计、不触发下单。

### INV-3 时间：内部统一「UTC 秒级 epoch + 显式语义」

内部 Bar 同时携带 `open_ts` 与 `close_ts`，不依赖单一时间戳的隐含语义（数据源之间不一致，见 §3.2）。
展示层按市场时区渲染；期货另有「交易日 trading_day」字段，与自然日解耦。

## 2. 目录结构

```
signal-desk/
├── docs/                  PRD / ARCHITECTURE / ROADMAP / CONVENTIONS / adr/
├── config/
│   ├── symbols.yaml       标的清单与三方代码映射
│   ├── calendars/         交易日历（品种 → session 定义 + 节假日）
│   └── rules/             形态规则（YAML）
├── src/sigdesk/
│   ├── core/              Bar/Symbol/Signal/Event 数据模型，事件总线，时钟
│   ├── feed/              quote_api.py(期货轮询) / crypto_ws.py / replay.py
│   ├── store/             bar_store.py(as-of视图) / parquet_io.py / state.py(SQLite)
│   ├── indicators/        增量指标
│   ├── patterns/          builtin/(结构原语) + registry.py(插件注册) + expr.py(AST求值)
│   ├── rules/             loader.py / evaluator.py / statemachine.py
│   ├── sinks/             notifier/ + signal_store.py
│   ├── trade/             strategy/ risk/ broker/(ctp, ccxt) 【M4+】
│   ├── backtest/          runner.py / metrics.py
│   └── web/               FastAPI 应用 + SSE
├── web/                   前端（Vite + React + TS + lightweight-charts）
└── tests/
```

## 3. Feed 层：两个市场的差异必须显式建模

### 3.1 两种 Feed 形态

| | 期货（Quote API） | 加密（交易所） |
|---|---|---|
| 获取方式 | **HTTP 轮询**（无推送） | **WebSocket 推送** + REST 补历史 |
| 触发时机 | 定时器：每根 bar 收盘后 +1.5s 拉取 | WS 事件驱动 |
| 批量 | 单请求最多 2000 品种，非常适合一次拉全 | 每标的一路订阅 |
| 缺口修复 | 重叠拉取（每次多要 N 根做校验） | 断线后 REST 回补 |
| 运行时段 | 交易日历驱动，非交易时段休眠 | 7×24 |

两者统一实现 `Feed` 接口：`async def stream() -> AsyncIterator[BarEvent]`。
`BarEvent` 携带 `closed` 标记，由 Feed 层负责判定，上层不关心来源。

### 3.2 Quote API 实测契约（2026-08-28 探测确认）

- 端点：`POST /api/v1/kline/by-count`、`POST /api/v1/kline/by-timerange`、
  `POST /api/v1/varieties/search`、`POST /api/v1/varieties/main-by-date`、`GET /api/v1/varieties/main`
- 认证：`Authorization: Bearer <AK>`；`interval_range`：1/5/10/15/30/60/101/102/103
- **【实测】分钟线 `time_stamp` = K线收盘时刻的 UTC epoch**。
  依据：rb8888 各交易节首根标 09:01/10:31/13:31/21:01，末根标 10:15/11:30/15:00/23:00。
  ⇒ 内部 `close_ts = time_stamp`，`open_ts = time_stamp - interval`。
- **【实测】最后一根是进行中 bar**。同一分钟内两次采样，末根 volume 由 3037 变为 5190。
  ⇒ Feed 必须丢弃或标记末根，判据 `time_stamp <= now`。
- **【实测】日线 `time_stamp` = 交易日的 UTC 零点**（纯日期编码，与分钟线语义不同）。
  ⇒ 日线走独立转换路径：`trading_day = date(ts)`，不能与分钟线共用换算。
- **【实测】`GET /api/v1/varieties/main` 当前返回 500**
  （`failed to unmarshal response`，服务端故障）。主力映射改用 `main-by-date` 查当日区间。
- **【实测】无加密货币数据**（搜 "BTC" 命中的是美股/ETF）⇒ 加密必须自接交易所。
- **【实测】两个 K 线接口的数据不完全等价，且分工不同：**
  - `by-count`：**唯一能拿到当日盘中数据**的接口（上限 2000 根，不支持复权）。
  - `by-timerange`：**不含当日自然日数据**（实测截止到当日 00:00 之前），支持复权，无数量限制。
  - ⇒ 当日实时走 `by-count`，次日用 `by-timerange` 做**权威回填与校正**；归档数据以 `by-timerange` 为准。
- **【实测】主力连续 `rb8888` 数据不可用于回测。** 同一时段 1m 数据在两接口间 272/345 根不一致、
  其中 75 根**价格**不一致；而真实合约 `rb2610` 仅 2/345 根有微差（1 根价格差 1 tick）。
  ⇒ 一切以**真实合约**为准；长周期连续合约由本系统用 `main-by-date` + 真实合约**自行拼接**，
  拼接方式可控、可复现。`8888`/`9999` 仅供粗略看图，永不进入回测与统计。
- 数据校验容差：价格字段要求**逐根精确一致**；成交量允许 ≤0.5% 差异但必须记录告警。
- 复权仅 `by-timerange` 支持；`by-count` 上限 2000 根。
- TLS 为自签名证书 —— 见 ADR-0002，**不用全局 `verify=False`**。

### 3.3 期货交易日历与 K 线合成

- Session 定义按品种配置（例：rb = 09:00-10:15 / 10:30-11:30 / 13:30-15:00 / 21:00-23:00）。
- **【实测】分桶规则 = 纯墙钟对齐 + 跳过空桶**，无需 session 内计数：
  `bucket_close_ts = ceil(bar.close_ts / period) * period`，该桶无任何 1m bar 则不产出。
  实测依据（rb8888 08-27 交易日）：5m 在 10:15 休盘后首根标 **10:35**；15m 标 **10:45**；
  60m 日盘序列为 `10:00 / 11:00 / 12:00 / 14:00 / 15:00` —— 其中 12:00 桶实际只含 11:00-11:30 的
  30 分钟数据，13:00 桶因无交易而被跳过。完全符合"墙钟对齐 + 跳过空桶"。
- 因北京时间为 UTC+8 整小时偏移，且周期均整除小时，可直接在 UTC epoch 上取整，无需时区换算。
- 已验证（2026-08-28）：跨零点夜盘品种（au 21:00-02:30）的 15m/1h 分桶与本规则完全一致。
- 交易日归属：夜盘属于**下一个**交易日（08-27 21:00 的 bar 属于 08-28 交易日 —— 待 M0-A 用日线成交量对拍确认）。
- 断点即 session 边界：相邻 1m bar 间隔 > 60s 处切段，可作为日历自检的交叉验证手段。

### 3.4 主力换月

- `main-by-date` 提供品类 → 历史主力合约的生效区间，是换月的**单一事实源**。
- 预警：订阅当前主力真实合约；换月当日新旧合约都跑，规则状态机**不迁移**（新合约从 IDLE 起）。
- 回测：**不使用数据源的 8888/9999**（口径不可复现，见 §3.2）。长周期连续序列由本系统
  依 `main-by-date` 的生效区间拼接真实合约生成，拼接方式（不复权/价差平移/比例复权）写入产物元数据。

### 3.5 OKX 实测契约（2026-08-28 探测确认，见 ADR-0005）

交易所选型见 ADR-0005（Binance 合约 WS 在开发机零帧，改走 OKX 永续）。

- 端点：WS `wss://ws.okx.com:8443/ws/v5/business` 的 `candle1m` 频道
  （**不是** `/ws/v5/public`，订错会收到 60018）；
  REST `/api/v5/market/candles`（最近 300 根）与 `/api/v5/market/history-candles`（分页回溯）。
- 数组列位：`[ts, o, h, l, c, vol(张), volCcy(币), volCcyQuote(计价币), confirm]`，全为字符串。
- **【实测】`ts` = K 线开盘时刻的毫秒 epoch** ⇒ `open_ts = ts//1000`，`close_ts = open_ts + period`。
  **与 Quote API 的收盘时刻语义相反**，套用会整体错位一根。
- **【实测】返回顺序为新→旧降序**，进行中的那根在首位；`history-candles` **同样含**进行中那根。
- **【实测】`confirm="1"` 即已收盘**，WS 上该消息在 bar 收盘后 **+0.5~1.2s** 到达；
  同一根进行中 bar 每分钟被推 30~80 次。
- **【实测】WS 与 REST 的同一根 bar 数值完全一致，但字符串格式不同**
  （WS `"79382.0"` / REST `"79382"`；volCcyQuote 有尾随零）⇒ 对拍必须转 float。
  夹具 `tests/fixtures/btcusdt_swap_okx_ws.json` 留证。
- **【实测】分页**：`after` 取更旧、`before` 取更新；`limit` 上限 300 且**超出静默截断**。
  以本页最早 ts 作下一页锚点，实测相邻页首尾正好相接，无重叠无缺口。
- **【实测】1m→5m/15m/1h 聚合与 OKX 自身高周期线逐根精确一致（价格零容差，300 根 1m）**——
  比期货数据源干净（后者自身不自洽，见 §3.2）。但设计不变：**高周期一律自建**，
  这样两个市场共用同一条合成路径，也不受数据源口径变化影响。
- 心跳：30s 无往来即断开；发**裸字符串** `"ping"`、回**裸字符串** `"pong"`（非 JSON）。
- 断线重连：指数退避（1s 起，封顶 30s），重连后按 `(last_close_ts, first_ws_close_ts - period]`
  用 REST 回补缺口，再续播；去重由 `BarCursor` 统一保证。
- 归一化**只对 SWAP 成立**：`volume=volCcy`、`money=volCcyQuote`、张数 `= volume / ctVal`
  （`Symbol.multiplier` 即 ctVal）。OKX 现货字段含义不同，需另走一条归一化路径。
- REST 拒绝默认 User-Agent（403），必须显式带 UA。加密无交易日概念 ⇒ `trading_day=None`，
  Parquet 按 UTC 自然日分区。

## 4. 指标层与形态层

### 4.1 指标：增量计算

每个 `(symbol, timeframe, indicator, params)` 维护一个 O(1) 更新的状态对象；
bar 收盘时 `update(bar)`，回测与实盘走同一条路径。禁止每根 bar 全量重算。

已实现于 `indicators/`（SMA/EMA/Wilder/StdDev/RSI/MACD/BOLL/ATR/KDJ）。要点：

- **口径见 ADR-0006**：EMA 用 SMA 播种、MACD 柱 = 2×(DIF−DEA)（国内口径）、
  BOLL 用总体标准差、RSI/ATR 用 Wilder（α=1/n，**不是** EMA 的 2/(n+1)）。
- **预热期返回 `None`，绝不用 0 顶替**。用 0 会让"均线尚未形成"表现为"价格跌到 0"，
  于是 `close > ema(close,60)` 在预热期恒真 —— 数据刚接上的头 60 根会疯狂误报。
- 滚动窗口和每满一个窗口用 `math.fsum` 重算一次，摊还仍是 O(1)，误差不随更新次数累积。
- 指标由表达式**求值时懒建**（规则是 YAML，编译期不知道要建哪些），首次用 as-of 窗口回放预热，
  之后只喂增量；**续喂起点用 bisect 定位** —— 曾写成从头遍历再逐个跳过，
  结果全对但复杂度退化成 O(窗口长度)，实盘预热 2000 根跑了 5 分半。

### 4.2 形态：三档能力，同一注册表

| 档 | 面向 | 形态示例 |
|---|---|---|
| A 表达式 | 指标类 | `ema(close,20) > ema(close,60) and rsi(14) < 45` |
| B 结构原语 | K线/价格结构 | `breakout(range(20), dir="up")`、`pin_bar(dir="up")`、`swing_high(5)` |
| C Python 插件 | 任意复杂逻辑 | `@pattern("my_setup")` 装饰器注册 |

A/B 共用一个白名单 AST 求值器（`ast.parse` + 节点白名单，禁 `eval/exec/import/属性访问`）；
B 的原语与 A 的指标注册在同一函数表里，所以规则里可以自由混用。

已实现于 `patterns/`。安全边界与语义：

- **规则来自 YAML，等于"配置即代码"**，所以不是"eval 加黑名单"，而是按节点类型白名单放行、
  自己走一遍求值。属性访问、下标、lambda、推导式、海象、星号展开、`**` 幂运算
  （`2**(10**9)` 能挂死求值线程）全部在**编译期**拒绝；未注册函数名与未知变量名同样编译期报错
  —— 一条打错字的规则应当启动时炸掉，而不是盘中静默不触发。
- **三值逻辑**：任一操作数为 `None`（预热期）⇒ 结果 `None` ⇒ 判定为"不成立"。
  `and` 遇明确的 `False` 仍直接判假（否则预热期什么都判不了），`or` 遇 `True` 直接判真。
- 指标函数返回 `Level(cur, prev)` 而非裸 float，`cross_up` 这类穿越原语才拿得到上一根。
- **C 档不是沙箱，也不打算是**：插件是用户自己写的 Python，能做的事和进程里任何代码一样多。
  被保证的是**数据边界** —— `PatternCtx` 只给到 as-of 截断后的只读序列，
  所以插件即使写错，最坏是形态判断错，不会造成未来函数、不会污染回测有效性。

C 的签名：

```python
@pattern("inside_bar_breakout", params={"lookback": 20})
def inside_bar_breakout(ctx: PatternCtx) -> bool:
    trend = ctx.bars("trend")        # 已按 as-of 截断的只读序列
    trig  = ctx.bars("trigger")
    return ctx.ind.ema(trend, 20) > ctx.ind.ema(trend, 60) and trig[-1].high > trig[-2].high
```

`PatternCtx` 是 as-of 视图的包装，只读、已截断、无网络无磁盘 ⇒ 插件天然可单测、天然无未来函数。

## 5. 规则引擎（核心）

### 5.1 规则结构

```yaml
id: trend-pullback-long
enabled: true
universe: [CN.SHFE.rb.MAIN, CRYPTO.BINANCE.BTCUSDT.PERP]
timeframes: { trend: 1h, setup: 15m, trigger: 5m }
conditions:
  - on: trend
    mode: state                 # 求值时取最近【已收盘】1h bar
    when: "ema(close,20) > ema(close,60) and close > ema(close,60)"
  - on: setup
    mode: window                # 最近 6 根 15m 内出现过
    within: 6
    when: "rsi(14) < 45"
  - on: trigger
    mode: event                 # 边沿：上一根不成立、这一根成立
    when: "cross_up(close, ema(close,20)) and volume > sma(volume,20)*1.5"
emit:
  direction: long
  confirm_on_close: true
  ttl: 8 bars                   # SETUP_ARMED 后 8 根 trigger bar 内未触发则作废
  cooldown: 30m
  dedup_key: "{symbol}:{rule}:{trend_bar_close_ts}"
  priority: high
```

M2 已实现完整的多级别引擎（`rules/`），语义定案见 **ADR-0007**。要点：

- 规则是**有序条件链路**（`conditions` 的顺序即链路顺序，最后一条是扳机）。
  段数任意 —— 长度 1 就是单级别规则，**M1 与 M2 共用一个模型、一个引擎**。
- 只有 `mode: state` 是持续型，**只有它失效才让链路回退**；`window`/`event` 是瞬时的，
  要求它们"仍然成立"会让链路永远推进不下去。回退目标是失效条件**之前那一段**，不是 IDLE。
- 求值分**记账**与**判定**两步：任何周期的 bar 收盘都记账（每根只求值一次），
  只有扳机周期的 bar 才跑状态机。**一批同时收盘的 bar 必须全部记完账再判定** ——
  否则 15m(setup) 与 5m(trigger) 同刻收盘时，扳机会读到上一根 setup，信号晚一拍。
- 状态机 + 条件日志 + 去重表持久化到 SQLite；重启时**有存档走 `resume`（补判漏掉的）、
  无存档走 `prime`（只预热不发信号）**，两条路不能混。

### 5.2 求值时机

**每根 trigger 周期的 bar 收盘时**求值一次（唯一入口）。此刻：
- `trend`/`setup` 条件读取各自最近**已收盘** bar（可能是几分钟前形成的，这是正确行为）；
- 因此不存在"大小周期时间对不齐"的模糊地带 —— 对齐点永远是 trigger bar 的 close_ts。

### 5.3 状态机

每个 `(symbol, rule)` 一个实例，持久化到 SQLite，重启后恢复：

```
IDLE ──trend成立──> TREND_OK ──setup成立──> SETUP_ARMED(TTL倒计时)
                       ↑                          │
                       └──trend失效──┬────────────┤ trigger成立
                                     │            ↓
                            IDLE <───┴──── FIRED ──> COOLDOWN ──超时──> TREND_OK
```

- `trend` 失效则整条链路回退，防止在反向趋势里残留 armed 状态；
  同一根上"trend 失效且扳机成立"时**必须回退，不能发信号** —— 反向趋势里的扳机
  正是最该被拦掉的那种假信号。
- TTL 用**扳机周期的 bar 根数**而非墙钟：休市、午休与数据缺口不消耗 TTL，
  因此回放与实盘的作废时刻完全一致。加载器直接拒绝 `ttl: 30m` 这种写法。
- 冷却精确等于配置秒数：**解冷那一根继续参与判定**，不被"消耗"掉。
- 去重键默认绑 `trend_bar_close_ts`：同一根大级别 bar 内同规则同标的只报一次；
  每个角色都有对应的 `{<角色>_bar_close_ts}` 占位符。

### 5.4 信号对象

`Signal(rule_id, symbol, direction, fired_at, trigger_price, tentative, context)`；
`context` 快照各级别关键指标值与 bar 时间戳，供推送内容、Web 回看与事后统计使用。

## 6. 存储

| 数据 | 方案 | 理由 |
|---|---|---|
| 历史 K 线 | Parquet，按 `market/symbol/timeframe/date` 分区，DuckDB 查询 | 零运维、列存压缩、开发机 7.6G 磁盘可控 |
| 运行态（规则、状态机、信号、账户） | SQLite（WAL），`store/runtime_store.py` | 单进程、事务、易备份；2核4G 不上 PG |
| Tick | **不落盘** | 本系统分钟级起步，tick 存储成本与收益不成正比 |
| 运行时 bar 序列 | `store/bar_store.py` 内存序列 + as-of 视图 | INV-1 的落地；两个市场共用一个 `BarStore`，落盘另由 `parquet_io` 负责 |

`BarStore` 只吃 1m（`push`）并增量派生高周期，历史用 `load` 直接装载；
`view(symbol, as_of)` 在**构造时**就按 `as_of` 定死截断位置 ——
即使求值期间又有新 bar 到达，已发出的视图也看不到，未来函数在结构上不可能发生。

## 7. Web 服务

- 后端 FastAPI + uvicorn，与引擎**同进程**（asyncio），信号经内存队列推给 SSE。
- 前端：**无构建单页**（原生 JS + 内置 `lightweight-charts`），见 **ADR-0009**
  —— 取代本节原写的 Vite + React + TS。P1/P2 交互复杂起来后可能要重写前端，这是明确接受的代价。
- 交付节奏：P0 只读（信号流 / 图表回看 / 运行健康）→ P1 规则 CRUD + 历史试算 → P2 可视化编辑器。
  **不在 M1/M2 阶段做前端**，否则会拖垮引擎正确性验证。
- 一条设计约束：**凡是能算在服务端的就算在服务端**。K 线上的信号标注就是典型 ——
  放在前端 JS 里，"标注与记录逐条对得上"这条验收根本没法测；
  改成 `/api/markers` 出标注、前端只画，它就变成了一个可单测的服务端不变量。
- 两种运行模式共用同一套端点：`watch.py --web`（同进程，健康与 SSE 有真数据）与
  `serve.py`（独立只读，只连 SQLite 与 Parquet）。后者的健康页**如实显示"未接入实时引擎"**。

## 8. 交易层（M4 起）

```
Signal → Intent(标的/方向/数量/价格类型) → RiskGate → Broker
                                            │           ├─ PaperBroker（纸上撮合，先行）
                                            │           ├─ CtpBroker（期货实盘）
                                            │           └─ CcxtBroker（加密实盘）
                                            └─ 单笔上限/品种上限/日亏熔断/持仓上限/频率限制
```

- 行情与交易通道分离（ADR-0002）：`SymbolRegistry` 是唯一映射源，启动时校验
  「Quote API 代码 ↔ CTP 代码 ↔ 交易所代码」三方一致，任一缺失则拒绝启动交易模块。
- 下单幂等：本地 `client_order_id` + 启动时与柜台对账，杜绝重启重复下单。

## 9. 部署

- 目标：云 VPS，`docker compose`（单容器：引擎+Web；数据卷挂 Parquet/SQLite）。
- 本开发机（2核4G / 剩 7.6G）只做开发与单测；全市场长周期回测放 VPS 或 Windows 机。
