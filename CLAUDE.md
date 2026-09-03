# Signal Desk — 项目须知

多级别技术形态盯盘预警系统；中国期货 + 加密货币。设计见 `docs/ARCHITECTURE.md`，进度见 `docs/ROADMAP.md`。

## 结构速览
```
docs/         PRD / ARCHITECTURE / ROADMAP / CONVENTIONS / adr
config/       symbols.yaml、calendars/(交易日历)、rules/(形态规则 YAML)
src/sigdesk/  core feed store indicators patterns rules sinks trade backtest web
web/          前端（M3 才开工）
tests/
```

## 凭据配置（配一次，之后都不用管）
```bash
python scripts/setup_env.py          # 交互写入 ~/.signal-desk/.env（只本人可读）
python scripts/setup_env.py --show   # 只看有没有配，**不显示值**
```
- **脚本自己读 `.env`**，不要再用 `set -a; . ./.env; set +a` —— 那是 bash 专有写法，
  Windows 上没有对应物，是"每次都要重配"的根因。
- 查找顺序：`SIGDESK_ENV` → 项目 `./.env` → `~/.signal-desk/.env`。
  **已经在环境里的键永远不被覆盖**（显式 export / CI 注入优先）。
- 用户级那份是关键：**分发包不含 `.env`**，换新包时项目内那份就没了。
- 诊断信息只打印键名，**绝不打印值**。没有前端配置界面，也不该做 ——
  面板无鉴权，"改规则文件"和"写 API 密钥"是两个风险等级。

## 常用命令
```bash
# 环境
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

# 测试（全绿才算完成）
pytest -q
pytest tests/test_barbuilder.py -q      # 单模块

# 探行情接口（AK 从 .env 读，勿写进命令行历史）
curl -sk -X POST -H "Authorization: Bearer $QUOTE_API_KEY" -H "Content-Type: application/json" \
  "$QUOTE_API_BASE/api/v1/kline/by-count" \
  -d '{"variety_code":"rb8888","interval_range":1,"count":5}'

# 探加密行情（OKX 公开行情，无需凭据；注意必须带 UA，否则 403）
curl -s -A sigdesk/0.1 "https://www.okx.com/api/v5/market/candles?instId=BTC-USDT-SWAP&bar=1m&limit=3"

# 加密实时链路验收（WS 跑 N 分钟后用 REST 对拍；M0-B 验收手段）
.venv/bin/python scripts/crypto_live_check.py 6

# 盯盘（M1 入口）：实时行情 -> BarStore -> 规则 -> 推送
.venv/bin/python scripts/watch.py --crypto-only --minutes 10

# Web 面板
.venv/bin/python scripts/watch.py --crypto-only --web 127.0.0.1:8000   # 同进程
.venv/bin/python scripts/serve.py                                      # 独立只读
.venv/bin/python scripts/report.py --horizon 30 --cost-bps 2           # 终端版质量报告

# 质量
ruff check src tests scripts && mypy src
```

## 写规则前必读

21. **YAML 1.1 会把裸键 `on:` 解析成布尔 `True`**（`yes`/`no`/`off` 同理）。
    而规则里条件键就叫 `on:` —— 加载器已同时接受 `"on"` 与 `True` 两种键。
    自己写解析时别忘了这条：M1 时因为有 `timeframe:` 兜底才没暴露，M2 一上来就炸。

- **指标口径见 ADR-0006**（EMA 用 SMA 播种、MACD 柱 = 2×(DIF−DEA) 国内口径、
  BOLL 标准差除以 n、Wilder≠EMA）。阈值是照着看盘软件写的，口径不一致会让规则不触发或乱触发。
- **跨级别引用写 `at('1h', <表达式>)`**（ADR-0011），别给指标加周期参数。
  它是求值器里的特殊形式：第二个参数在切换后的上下文里求值。as-of 由外层视图定死，
  5m 那刻只看得到**已收盘**的 1h。周期必须是字面量。
- **建 BarStore 一律用 `store_timeframes(rules)`**，别手写 DERIVED 常量。
  规则里多一个 `at('4h',...)` 而常量没跟着改 ⇒ 该级别恒为空序列 ⇒ 条件恒"不成立"
  ⇒ **一条信号都不报且毫无提示**。取不到数据时 `_ViewSource` 会当场抛，别把它改成返回空。
- **日线 `1d` 按交易日聚合**（`DayBuilder`），不是墙钟分桶：夜盘归属下一交易日，
  与当日日盘合成同一根。日线只能在**日期键变化时**收盘，比真实收盘晚一根 bar。
  排序用 `Timeframe.rank` 不要用 `seconds`（`D1.seconds` 是 0）。
- **`prev(x, n=1)`** 取"n 根之前"：`prev(close)` 是上一根 bar，
  但 `prev(swing_low(5))` 是**上一个摆动低点**（双底就靠它）。
  `double_bottom/double_top` 是语法糖，额外查颈线 —— 没有反弹的两个相近低点是横盘不是双底。
  **判均线朝向要用 `prev(ema(close,20), 6)` 这类多根回看，别用单根差**：
  单根 EMA 差噪声极大，实测 0.031 个 ATR 的降幅（图上一条平线）就足以被判成"空头趋势"。
  回看上限 `MAX_LOOKBACK`（patterns/context.py，现为 250）；
  超界**报错**，历史不够长（预热）才返回 None —— 两种"取不到"不能都静默成 None。
- **判横盘/振幅用 `range_atr(n)`，别用固定百分比。** `consolidation(n, 0.008)`
  比的是占价格的比例，而各品种波动尺度差好几倍 —— 实测同一段「5 小时振幅」
  铜 0.81%、股指 1.89%，于是 `< 0.8%` 在铜上 49.8% 成立、股指上只有 **0.3%**，
  各品种命中率**极差 170 倍**。规则写着 `universe: CN.*` 却事实上不适用于高波动品种
  （用户在 UR701 上踩到，box 0/123）。换 ATR 归一后极差降到 2.7 倍。
  **`range_atr` 含当前根，`range()` 不含**（后者给 breakout 用，含了就恒为假）—— 别"统一"。
- **预热期是 None 不是 0**，表达式走三值逻辑，None 一律判为"不成立"。
- `config/rules/` 下的文件**会被真加载**，任何一个写坏就是启动失败；
  尚不支持的目标样板放 `docs/examples/`。
- **多级别规则**：`timeframes: {trend: 1h, setup: 15m, trigger: 5m}` + 有序 `conditions`，
  **顺序即链路顺序**，最后一条是扳机。求值只在扳机周期的 bar 收盘时发生。
- **只有 `mode: state` 是持续型**，失效才让链路回退；`window`/`event` 是瞬时的。
  想限制 armed 时长请配 `ttl`（数**扳机 bar 根数**，不接受时长），别指望 window 的 `within`。
- 去重键可用 `{<角色>_bar_close_ts}`，绑在大级别上就是"同一根大级别 bar 内只报一次"。
- 状态机、条件日志、去重表持久化在 `data/runtime.sqlite3`；重启后
  **有存档走 `resume`（补判漏掉的），无存档走 `prime`（只预热不发信号）** —— 这两条路不能混。

## 已知坑（踩过的，别重复）

1. **Quote API 分钟线 `time_stamp` 是「收盘时刻」不是开盘时刻**。
   实测：rb8888 各交易节首根标 09:01/10:31/13:31/21:01，末根标 10:15/11:30/15:00/23:00。
2. **返回数组的最后一根永远是进行中 bar，数值会变**。同一分钟内两次采样 volume 从 3037 变 5190。
   判据：`closed ⟺ time_stamp <= now`。用错就是全局信号重绘。
3. **日线 `time_stamp` 语义与分钟线不同**：日线是「交易日的 UTC 零点」，纯日期编码。必须分路径转换。
4. **`GET /api/v1/varieties/main` 返回 500**（服务端故障）。主力映射用 `POST /api/v1/varieties/main-by-date`。
5. **Quote API 没有加密货币数据**（搜 BTC 命中的是美股/ETF）。加密只能自接交易所 WS + ccxt。
6. **Quote API 没有推送**，期货只能轮询；每根 bar 收盘后 +1.5s 拉，带重叠窗口校验缺口。
7. **TLS 是自签名证书**。不要 `verify=False`（请求头带着 AK，等于放弃 MITM 防护）；
   用 `QUOTE_API_TLS_FINGERPRINT` 做指纹固定。
8. **多周期一律由 1m 自行聚合，绝不直接用接口的高周期线** —— 接口的各周期序列之间自身不自洽。
   铁证（IF2609，08-27 10:00）：数据源 1m close=4565.4，它自己的 1h 线也是 4565.4，
   但它的 5m/15m 线是 4566.0。我们的聚合恒等于 1m 推导值，所以"差异"出在数据源那边。
   实测残差：5 品种 × 3 周期共比对约 2400 根，差异 13 处（0.5%），全为 1 tick / ≤0.1% 量差的单点。
9. **`rb8888` 主力连续数据不可用于回测。** 同一时段 1m 数据 by-count 与 by-timerange
   272/345 根不一致（75 根价格不一致）；真实合约 rb2610 仅 2/345 根微差。
   ⇒ **一切用真实合约**；连续序列自己用 `main-by-date` 拼。8888/9999 只配粗略看图。
10. **两个 K 线接口分工不同，别用错**：
    - `by-count` = 唯一能拿当日盘中数据（≤2000根，无复权）→ 实时轮询用它
    - `by-timerange` = **不含当日数据**（截止当日 00:00 前），支持复权 → 历史回补/权威校正用它
    - 归档以 by-timerange 为准，次日回填校正当日用 by-count 拉的临时数据
11. **多周期分桶 = 纯墙钟对齐 + 跳过空桶**（不是 session 内计数！）：
    `bucket = ceil(close_ts/period)*period`。实测 rb 60m 日盘序列 10:00/11:00/**12:00**/14:00/15:00，
    12:00 桶只含 11:00-11:30 半小时数据，13:00 桶因无交易被跳过。
12. 开发机 2核4G、磁盘剩 ~7.6G：只存 bar 不存 tick；全市场长周期回测搬 VPS 或 Windows。

### 加密（OKX，M0-B 实测）

13. **OKX 的时间语义与期货处处相反，别套用**：`ts` 是**开盘**时刻（Quote API 是收盘时刻）；
    返回顺序是**新→旧降序**（Quote API 是升序）；进行中的那根在**数组首位**（Quote API 在末位）。
14. **收盘看 `confirm` 字段，不要用本地时钟推断**。这是 OKX 比 Quote API 干净的地方：
    WS 与 REST 是同一个字段，天然满足 INV-2。实测 `confirm=1` 在 bar 收盘后 +0.5~1.2s 到达。
15. **candle 频道在 `/ws/v5/business`，不在 `/ws/v5/public`** —— 订错端点会收到 60018 错误事件，
    表现为"连着但永远没数据"，所以错误事件必须抛出不能静默忽略。
    心跳发**裸字符串** `"ping"`、回**裸字符串** `"pong"`（不是 JSON，用 json.loads 解会炸）；
    30s 无往来即断开。
16. **WS 与 REST 数值相同但字符串格式不同**（`"79382.0"` vs `"79382"`，volCcyQuote 有尾随零）。
    ⇒ 任何对拍都必须转 float 比，按字符串比会满屏假差异。夹具 `btcusdt_swap_okx_ws.json` 已留证。
17. **`history-candles` 也含进行中那根**（别以为"历史接口"就都是收盘的）；`limit` 上限 300 且
    **超出静默截断不报错**；`after` = 取更**旧**的、`before` = 取更**新**的（容易搞反）。
    分页锚点取本页最早 ts，实测相邻页首尾正好相接，无重叠无缺口。
18. **OKX REST 拒绝默认 User-Agent（403）**，必须显式带 UA。
19. **Binance 合约 WS（fstream）在本机握手成功但一帧数据都不来**（kline/aggTrade 两次复现），
    而 Binance **现货** WS 正常 —— 网络侧限制，不是代码问题。加密改走 OKX，见 ADR-0005。
20. **volume 取 `volCcy`（币量）不取 `vol`（张数）**：张数依赖 ctVal，跨品种不可比。
    张数 = `volume / Symbol.multiplier`（multiplier 即 ctVal）。归一化**只对 SWAP 成立**，
    OKX 现货的 vol/volCcy 含义不同。

### 主连拼接（实测）

22. **`main-by-date` 的 `main_variety_codes` 要传 9999 指数代码**（`rb9999`），
    传 `rb` 或 `rb8888` 会返回 `{"code":0,"data":null}` —— **成功状态码配空数据，不报错**。
    响应是多品类平铺的一个列表，字段 `main_variety_code/variety_code/start_date/end_date`，
    日期形如 `2026-04-08 00:00:00`。夹具见 `tests/fixtures/main_by_date_rb_i.json`。
23. **换月价差必须拿两个合约的同一根 bar 算**，不能拿"旧合约最后一根"对"新合约第一根" ——
    那两根差着一整夜，价差里混进了隔夜跳空。实测 2026-04-08 rb2605→rb2610 同根价差 +24，
    复权后接缝跳空 −2（正常隔夜），不复权则是 −26 的假跳空，ATR/EMA 会当成真波动。

## 数据落盘路径（踩过）
- **分区粒度随周期而变**（`partition_unit`）：分钟级（1m~4h）按**交易日**，
  日历级（1d/1w/1mon）按**年**。日历级按日分就是**一行一个文件** ——
  实测 998 根日线摊在 975 个文件里读一次 789ms/3005KB，并成一个 3ms/59KB
  （**快 278 倍、小 51 倍**），多出来的全是每个文件的 schema+footer。
  **分钟级绝不能照搬**：那会变成每分钟重写一个几万行的文件。
  存量数据用 `scripts/compact.py` 迁移（先校验逐根一致再删旧文件，幂等，可中断）。
  **跑 compact 前先停 watch.py。**
- **`latest_partition`/`partition_span` 不能假定文件名就是日期**：日历周期的
  文件名是年份，要读 Parquet 的 row-group 统计拿真实首末日（不读行数据）。
  面板的「数据止于 X」和 `backfill_all` 的覆盖判断都依赖它们返回真实日期。
- **`read_range` 先按文件名裁分区再读**，并**按 close_ts 去重**：
  去重是迁移期的兜底（新旧布局并存时同一根 bar 会被读两遍，且完全看不出来）。
  裁剪必须**保守**（两边各留一天余量：分区键是交易日，与 close_ts 的 UTC 日期能差一天）——
  裁猛了就是静默少一段数据。`test_pruning_never_changes_the_result` 拿"不裁剪"的结果逐根对拍。
- **只要某几列就用 `read_close_ts` 这类列裁剪读，别走 `read_range`。**
  Parquet 是列存；`/api/markers` 只用 `close_ts`，却读全部 10 列再造几万个 Bar 对象
  （三万根 152.7ms vs 12.8ms，12 倍）。**别对 markers 用尾读** ——
  窗口外的老信号会被误判成 dropped；列裁剪才是语义不变的那条路。
- **裁剪只对有界区间有用**（面板发的是 `start_ts=0` 无界区间）。所以无界那条路
  走 **`read_tail`**：从最新分区往回读、够了就停；行数用 `count_bars` 从元数据拿。
  1m 由 3 万根降到 `limit + 预热`，实测 226ms → 12ms。
- **均线预热根数由 `warmup_bars` 算，别拍**：SMA 取窗口（窗口外的数学上不影响），
  **EMA 递归要 20 倍窗口**（1x 误差 1.6e-04、10x 1.8e-12）。
  注意**不是逐位相同** —— 滚动累加的舍入路径随喂入长度而变，全量对拍实测
  最大相对误差 1.4e-15（约 6 个机器 epsilon）。会造成"图上上穿了、规则没触发"的是
  **算法或窗口不一致**，不是第 15 位有效数字（引擎自己也在有界窗口上累加）。

- **bar 一律落 `data/bars/`，运行态落 `data/runtime.sqlite3`。**
  六个脚本（backfill/build_continuous/watch/serve/report/paper_run）的 `--data-root`
  默认值必须一致 —— 曾经回补脚本写 `data/bars/`、面板读 `data/`，**回补的数据面板一根都读不到**。
- **`watch.py` 一律落盘**（不再只在 `--web` 时落）。盯盘却丢掉自己收到的行情说不通。
- **建 BarStore 时 `watch.py` 必须取并集** `DEFAULT_TIMEFRAMES ∪ store_timeframes(rules)`：
  它同时喂规则引擎（只要规则用到的周期）和面板图表（要全部可选周期）。
  只按规则派生 ⇒ 其余周期**静默停更** ⇒ 面板上看着就是「行情连不上了」（真踩过）。
  `paper_run`/`replay_check` 同理。只有 `rules/trial.py` 可以只派生规则需要的（它不喂图表）。
- **`api(path)` 只会 GET；带动词必须用 `send(method, path, body)`。** 两个 helper 长得像、
  签名不一样，用混了**两条都是静默失败**：把 path 传成 method 会让 fetch 拿到非法动词、
  请求根本不发出去；给 api 传 `{method:"DELETE"}` 会被静默忽略、发成 GET 撞 405。
  预警组的钉住按钮就这么两条分支各坏各的躺了一版。`api()` 现在多收参数当场抛，
  `test_panel_js.py` 会把两者所有调用点过一遍。
- 面板下拉框列**全部标的**（含主连，标注「主连」），不是 `tradable()`。
  但 `watch.py` **只采集出现在某条规则 `universe` 里的标的** —— 两者不一致，
  所以下拉框要标「**未盯**」（`/api/meta` 的 `watched` 字段），
  空状态也要按"有没有人盯"分开说明。不标的话，用户选中就是空图，
  只会以为"行情连不上"（真发生过）。
- `.chart-empty` 是 flex 容器：**消息内容要裹一层块级元素**，
  否则 `<b>`/`<br>` 会被拆成多个 flex item 排成一行，换行全乱。
  主连可回测/可看图**不可下单**；排除它是预警/下单路径的事。

## 面板可以自己看（别再说"没浏览器验证不了"）
这台开发机**装了 `/usr/bin/google-chrome`**，playwright 的 node 模块随 `@playwright/mcp`
一起装了。两者拼起来就能无头截图：

```bash
.venv/bin/python scripts/serve.py --port 8899 &
node scripts/shoot_panel.mjs            # 截 4 个页面到 docs/design/shots/，并汇总控制台错误
```

坑：playwright 自带的 chromium 缓存版本对不上，**必须显式 `executablePath` 指系统 Chrome**，
否则报 "Executable doesn't exist"。脚本里已经处理了。

改前端**先截图再改**。只靠读代码看不出堆叠、截断、留白这些问题 ——
M3 交付时就是没看，把三个明显的视觉缺陷一起发了出去。
规则页那轮又抓到三个：`[hidden]` 被 `.badge{display:flex}` 盖掉（徽标一直亮）、
0 条信号时把"算不出来"显示成 `0.0%`、信号竖线画在每条轨道上把 1m 那条糊成噪声。
**桩化冒烟保证不白屏，截图保证不难看，两个都要跑**（`tests/panel_smoke.mjs` 支持
`SMOKE_FIX` 环境变量覆盖夹具，用来构造"扫了一堆 bar 但一条没触发"这类场景）。

**表单控件不继承页面的 `color`**：`textarea` 一定要在
`select,input,button` 那条基础样式里，否则就是浏览器默认的纯黑字配深色底。
`shoot_panel.mjs` 现在有对比度检查（<3:1 报错）兜底，`test_panel_js.py` 里还有一道静态断言。

还有第三类：**截图看得见症状、DOM 里却一切正常**。空状态提示被 lightweight-charts 的
canvas（z-index:1/2）整个盖住就是这样 —— 元素在、文字在、visibility 正常，屏幕上一片漆黑。
只有 `elementFromPoint` 命中测试能定位。`shoot_panel.mjs` 现在每张图后都跑一遍**遮挡检查**，
覆盖 `.chart-empty/.empty/.callout/.banner`。

**桩化冒烟看不出 HTML 里少了元素**：DOM 桩对任意选择器都返回节点，
所以 `#grid-toggle` 没插进 index.html 时冒烟照样绿、真实浏览器直接白屏。
冒烟现在会把 app.js 查过的 `#id` 与 index.html 对一遍（`missing_ids`）。

**截图要覆盖到「状态」，不只是「页面」**：规则编辑器的黑字 bug 逃过了所有截图，
因为当时只截了加载已有规则那一瞬（恰好是 `:disabled`，唯一有颜色的状态）。
现在多截一张「新建规则」，专门覆盖编辑器的启用态。注意加了 `pointer-events:none` 的元素
命中测试会穿透，检查会跳过它们 —— 所以**别给这类提示层加 pointer-events:none**。

## Web 面板（M3）
- 前端是**无构建单页**（ADR-0009），不要引入 node 工具链；lightweight-charts 已内置在
  `src/sigdesk/web/static/vendor/`，不依赖 CDN。
- **图上三种形状**：圆点=信号（多绿 B / 空红 S / 中性灰无字母）、方块=开仓、箭头=离场。
  别让信号和成交用同一种形状 —— 同色时完全分不开。
- **量能均线的窗口要跟规则一致**（`volume-spike` 用 `sma(volume,20)`），
  否则看不出它为什么触发。
- **均线由服务端算**（`web/overlay.py` 复用引擎的 SMA/EMA）。前端重算一遍看着一样，
  但口径一偏就会「图上上穿了、规则没触发」。均线要在**完整序列**上算再截取；
  预热期是 `null`，前端跳过不补 0。均线图例**浮在图上**，进头部会把控件挤到换行。
- **信号流筛选框 = 信号里的规则 ∪ 已加载的规则**（带条数、已删的标「已下线」）。
  只列已加载的，来自已删规则的历史信号就永远筛不出来（实测 50 条里 44 条是这种）。
- **凡是能算在服务端的就算在服务端**：写在前端 JS 里的逻辑测不到。
  典型是 K 线标注 —— 由 `/api/markers` 出点（含成交）、前端只画。
- **图上标注文字要克制**：信号标注一律不写字（同规则的文字会叠成一片）；
  成交点**只给当前选中信号的那几笔**写价格 —— 几十笔全写字会互相遮蔽，
  截图里当场看到 5 个标签压在一起。要看某笔的价，点那条信号（还会画出价格横线）。
- 统计口径见 **ADR-0008**（入场取次根开盘、同根同时触及记止损、成本双边、neutral 不进胜率）。
  改口径 = 改结论，动之前先读 ADR。
- 两种模式：`watch.py --web`（同进程，健康与 SSE 有真数据）/ `serve.py`（独立只读）。

## 键盘快捷键
- 键位取**国内期货软件的手感**：`1`–`9` 按周期条从左到右、`PgUp`/`PgDn` 翻标的、
  `Esc` 逐层回退。另有 `Alt+M/H/D/W` 助记、`J`/`K` 翻信号流、`G` 九宫格、
  `T` 换网格模式、`[`/`]` 切预警组市场。
- 三条约束，每条都能单独毁掉这个功能：**输入框里一律不响应**（规则页有 textarea，
  在里面敲 "1d" 会被吃成切周期）；**不碰浏览器占用的** `Ctrl+数字`/`Ctrl+W`/`F5`；
  **必须有 `?` 帮助 + 「⌨」入口** —— 快捷键不可见就等于不存在。
  「⌨」按钮放**顶栏**（与「提醒方式」同级），不是图表控件区 —— 它是全局功能。
- **每个快捷键都点按钮，不许另写一套动作逻辑。** 数字键、`T`、`[`/`]` 一律
  `按钮.onclick()`。上一版 `T` 自己翻 `G.mode` 再 `renderGrid()`，绕过了按钮里的
  `clearZoom()` —— 放大态下按 T 直接灰屏。**同一个动作有两份实现，修一份必漏另一份**。

## 九宫格（盯盘页）
- 同屏九格：`分时/1m/5m`、`15m/30m/1h`、`1d/1w/1mon`，
  **双击**一格放大铺满、再双击还原（单击留给"停在小格看十字线"）。
  排布是独立常量 `GRID_TFS`，不复用 `meta.timeframes`。
- **分时不是周期**，是当日 1m 的画法（`/api/intraday`），不进 `Timeframe` 枚举。
  **均价必须乘合约乘数**（rb 每手 10 吨，漏了会画在十倍高的地方）；
  取"数据里最后一个交易日"而不是墙钟今天，并把日期标出来。
- 曾经做过 15s（OKX **REST 支持 `bar=15s`、WS 没有 `candle15s` 频道**，实测 60018），
  后按用户要求换回分时。**期货最小 1m 且只有轮询（坑#6），秒级要换数据源。**
- bar 少于 20 根的格子不画信号标注 —— 标注尺寸随 bar 宽度缩放，会被撑到半个格子。
- **十字线同步**：悬停即九格同步，`shift+单击` 锁定，`Esc` 返回（先解锁再退放大）。
  **线和数字必须一起走**：被同步过来的格子拿不到 `param.seriesData`
  （`setCrosshairPosition` 触发的回调里是空的），要按 bar 索引自己查（`paintCellAt`）。
- **预警组顶部不显示 OHLC/成交量**：九格是九个品种，一个读数说不清是谁的。
- **预警组放大一格会把该格标的设成 `S.symbol`**（下拉框、标题一起跟）——
  否则切到九周期看到的还是旧标的。
- **行情要定时刷**（30s，`refreshMarket`）：SSE 只推 signal，不推 bar，
  不定时刷的话盘中价格纹丝不动。刷新**必须保住可视区间**、**页面不可见时跳过**。
  跨周期必须对齐到**包含该时刻的那根 bar**（二分找最后一根 `time <= 目标`），
  直接塞原时刻给日线图是错的。设置十字线会再次触发 `crosshairMove`，
  **必须有 `G.syncing` 防回环**，且要在 `finally` 里解锁。
- **`DEFAULT_TIMEFRAMES` 必须含日/周/月**，否则那几格静默停更（同"行情连不上"那类坑）。
- 周线键用 **ISO 年-周**（12-29 属次年第 1 周），月键 `YYYY-MM` 补零 ——
  **分桶键必须字典序单调**，时间倒流检查直接比字符串。
- 排序一律用 `Timeframe.rank`，别用 `seconds`（日/周/月都是 0）。
- lightweight-charts **不会自己跟随容器尺寸**：放大、还原、切页都要显式 `applyOptions`。
- 格子标题类名是 `.cell-tf` 不是 `.tf` —— 后者是顶部周期按钮，撞名会让选择器互相误伤。

## 信号与保留
- **信号只增不减，没有清理策略**（`fill` 同理）。实测每行约 364 字节，
  按当前触发率外推 66 标的 x 4 规则 ≈ 每天 52 条 —— 一年约 6.6 MB、十年 66 MB，
  **磁盘不是问题，界面才是**：所以统计页有时间范围、信号流能往回翻页。
  真要加清理策略之前先想清楚：信号是复盘的原始材料。
- **钉住（`watchlist_pin`）也永久保留**，只有手动取消才消失，重启仍在。
  这是刻意的（"还需要观察"不该由系统替你决定何时结束），但钉多了会一直占预警组前排。
- **时间范围属于统计口径**，要跟报告一起原样带回并显示出来 ——
  换个区间结论就变，一份不写明区间的胜率没有意义。
- **翻页锚点从新往旧数**：从旧往新数的话，新信号一到达锚点就错位。

## 统计与评估
- **判据是超额不是毛收益**，面板与 CLI 共用 `stats/baseline.py` 一份实现。
  区间跨零时**不给涨跌色** —— 颜色本身就是"这个结论站不站得住"的编码。
- **`evaluate_all` 必须二分定位**：`evaluate` 只用 `future[:horizon_bars]`，
  按 close_ts 二分后切一小段。原来每条信号重扫整条序列，O(信号数 × bar 数)，
  把 /api/stats 拖到 6.3 秒（算基准 5795ms → 133ms）。
- **持有期曲线要同时给基准**：基准随持有期单调变化（实测 +0.0002% → +0.035%），
  只看毛期望会把"市场在涨"误读成"规则在长持有期上更好"。
- **桩要维护真实的父子关系**（`append`/`removeChild`/`firstChild`）与 `createElementNS`。
  写成空函数的话，"换数据要清掉上一张图"这条永远测不到。

## 前端改动的视觉一致性（这三条各踩过多次）
- **标题在 flex 行里一律 `flex:none` + `white-space:nowrap`。** 右边控件一多，
  标题会被压到最小宽度、中文变成一个字一行的竖排。踩过三次：chart-head 的
  「九宫格」、cell-head、rail-head 的「信号流」。**让控件换行，别让标题被压扁。**
- **`.chip` 要钉 `min-height`。** 里面可能装数字框/下拉框/复选框/日期框，
  四者默认行高各不相同，实测极差曾达 29.5px（用户一眼看出来）。
- **`.chip` 里的控件要去掉自己那套边框背景**（全局 `select,input,button` 那条给了），
  不去就是盒中盒。原来只覆盖了 `input`，加 `select`/`date` 时露馅。
- **加控件之后必须截图量一遍**（`shoot_panel.mjs`）：这三类问题读代码全看不出来。

## 信号提醒（面板右上角四个开关）
- 弹窗／声音／语音播报／桌面通知，选择存 localStorage。**只有 SSE 推来的新信号才提醒**，
  开机灌进的历史信号一条都不响 —— 改 `connectSSE` 时别把 `queueAlert` 挪到别处。
- **一批同时到达合并成一次提醒**（400ms 窗口）。多标的同刻收盘很常见。
- **AudioContext 必须在用户手势里创建/恢复**，所以建在"声音"开关的 onclick 里。
- 每个浏览器 API 都可能缺席（无痕模式、非 https、拒绝授权、冒烟沙箱）——
  一律 try/catch + 能力探测，缺一个都不能让面板起不来。

## 规则编辑与历史试算（FR-5.3）
```bash
.venv/bin/python scripts/serve.py --port 8899 --allow-edit   # 只有加了才有写端点
```
- **写端点默认关闭**，面板没有鉴权。`--allow-edit` 只允许绑回环地址；
  远程编辑自己开隧道 `ssh -L 8000:127.0.0.1:8000 <host>`。
- **盯盘进程里一律拒绝改规则**（409）：状态机/TTL/去重表都绑在当前这批规则上，
  热替换会静默丢掉已布防的链路。改完**重启盯盘进程**才生效。
- **试算也过同一道门** —— 它会按请求读任意标的全量历史并同步跑引擎，是 CPU 放大器。
- 保存前一定 `load_rule` 编译一遍，**不通过就不落盘**；删除移进 `config/rules/_trash/`
  （`load_rules` 用 `glob` 不是 `rglob`，子目录不会被加载）。
- 试算复用**同一个引擎**（ADR-0001），与实盘/回放逐条一致，有对拍测试钉住。
- 「各级别条件成立区间」是**按需重算**出来的，没有新增持久化。引擎的 `recorder=` 是只读旁路，
  别把它改成能改状态的东西 —— 画的必须与引擎看到的同源。

## 主连拼接（回测用）
```bash
.venv/bin/python scripts/build_continuous.py CN.SHFE.rb.CONT 2025-01-01 2026-08-31 --dry-run
.venv/bin/python scripts/build_continuous.py CN.SHFE.rb.CONT 2026-03-01 2026-05-31
```
- 纯逻辑在 `store/continuous.py`（拼接、锚点、复权），取数在脚本里，可脱网单测。
- **默认后复权价差**（最新一段保留真实价格，历史段整体平移）。平移量逐次记进
  `data/bars/_continuous/<uid>.json` —— 回测出怪结果要能回答"是不是换月造成的"。
- 缺合约数据、无重叠锚点、平移后价格击穿零点，一律 **fail-fast**：
  跳过缺失段等于凭空造一个大跳空。
- 平移后的历史价格**不是当时的真实成交价**，绝对价位无意义，只有形态与价差有意义。

## 交易层（M4）
- **默认关闭**：`config/trading.yaml` 的 `enabled: false`。没人明确打开之前，盯盘就只是盯盘。
- **撮合口径与统计口径共用同一套代码**（ADR-0010）：`risk_distances` 只有一个定义，
  出场判定逐条对齐，并有对拍测试钉住。改任何一边之前先读 ADR-0010。
- **风控闸是兜底，不是日常拦路虎**：定量时就按三个硬上限的最紧者截断
  （单笔名义 / 该品种剩余 / 总敞口剩余）。它天天响 = 定量层算漏了。
- **每根 bar 先撮合再产信号**：反了就是拿收盘后的信息吃开盘价。
- 全部时间取 `bar.close_ts`，**频率限制也不例外** —— 用墙钟会让回放与实盘拒不同的单。
- 纸上撮合**没有盘口、不看深度、bar 内路径不可见**，别拿它当实盘预期。

## 跨平台（Windows 是一等目标）
- **`os.kill(pid, 0)` 只在 POSIX 上是"探测"。** Windows 的 `os.kill` 除
  CTRL_C_EVENT/CTRL_BREAK_EVENT 外走 `TerminateProcess()` —— **它会真的把进程杀掉**。
  存活探测在 Windows 上必须走 `OpenProcess` + `GetExitCodeProcess`
  （见 `runtime_store._alive_windows`）。写任何"跨平台"系统调用前先查它在 Windows 上的真实语义。
- **测试不得依赖开发机的家目录/环境变量。** `~/.signal-desk/.env` 存在与否会让
  测试结果随机器而变，而且会把真实凭据读进进程。`conftest.py` 有 autouse 隔离，别绕过它。
- **`.bat` 必须 CRLF + 无 BOM**，每条退出路径都要 `pause`（否则双击后窗口一闪而过）。
  开发机是 Linux 跑不了 cmd.exe，能静态查的都在 `tests/test_launchers.py` 里。

## 三条不可违反的不变量
- **INV-1** 求值只能通过 `BarStore.view(symbol, as_of)` 取数（物理截断未来数据）。
  已落地于 `store/bar_store.py`：视图在**构造时**定死截断位置，之后 store 再收到新 bar 也看不到。
- **INV-2** 规则默认只消费 `closed=True` 的 bar；盘中预报必须标 `tentative`，不进统计不下单。
- **INV-3** 内部时间统一 UTC 秒级 epoch，Bar 必带 `open_ts` + `close_ts`；期货另带 `trading_day`。

## 工作方式
- 分段交付，一段一验收；ROADMAP 里该段验收清单全绿才开下一段。
- 凭据只进 `.env`（chmod 600 + gitignore），不进代码/日志/文档/提交。
