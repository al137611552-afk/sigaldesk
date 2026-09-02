# Signal Desk

多级别技术形态盯盘预警系统 —— 中国期货 + 加密货币；同一套信号引擎向下延伸到回测与自动交易。

## 文档
- 需求：[docs/PRD.md](docs/PRD.md) ｜ 架构：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 路线：[docs/ROADMAP.md](docs/ROADMAP.md) ｜ 日志：[docs/DEVLOG.md](docs/DEVLOG.md)
- 决策：[docs/adr/](docs/adr/) ｜ **开发须知与已知坑：[CLAUDE.md](CLAUDE.md)**
- 在 Windows 上跑：[docs/RUN-WINDOWS.md](docs/RUN-WINDOWS.md)

## 我该跑哪个

`scripts/` 下有十几个文件，但**会长期运行的只有两个**，其余都是跑完就退的工具。

| 想干什么 | 跑这个 |
|---|---|
| **日常盯盘**（采行情 + 跑规则 + 推送 + 面板） | `scripts/watch.py --web 127.0.0.1:8000` |
| 已经有一个在盯了，只想再看一眼 | `scripts/serve.py`（**只读**，不采行情、不写任何东西）|
| 首次装机 / 换了机器 | `scripts/setup_env.py` 配凭据 → `scripts/backfill.py` 回补历史 |

**写者只能有一个**：状态机、去重表、冷却都在同一个 SQLite 里，跑两个 `watch.py`
同一根 bar 会被判两次、重复报警。`watch.py` 启动时会检测并拒绝，
但"想再看一眼"的正确做法始终是 `serve.py`。

## 工具箱（一次性，跑完就退）

| 脚本 | 干什么 |
|---|---|
| `setup_env.py` | 配凭据到用户级目录，**只配一次**；`--show` 查状态（不显示值）|
| `pin_tls.py --write` | 抓 TLS 指纹并写回；证书轮换后重跑一次 |
| `backfill.py` | 回补单个标的（`--timeframe 1d` 拉长历史；加密自动走 OKX）|
| `backfill_all.py` | **批量回补**注册表里的全部标的，按标的降级、可续跑 |
| `build_continuous.py` | 拼主连（派生序列，不随盘更新）|
| `report.py` | 终端版信号质量报告 |
| `paper_run.py` | 纸上回测 |
| `acceptance.py` | 离线验收（不需要凭据、不联网）|
| `check_calendars.py` | **用真实成交时段核对交易日历**。换月/加品种/改日历后都该跑 |
| `sync_symbols.py` | 从行情接口同步国内期货标的到 `symbols.yaml`（`--with-options` 只要有期权的品种）。**换月后重跑它**|
| `make_package.py` | 打 Windows 测试包（装什么由 `git ls-files` 决定，所以 `.env`/数据库天然不进包）|
| `crosscheck.py` / `crypto_live_check.py` / `replay_check.py` | 三个对拍验收 |

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env        # 填入 QUOTE_API_KEY

set -a; . ./.env; set +a
.venv/bin/python scripts/pin_tls.py        # 抓证书指纹，写回 .env（只需一次）

# 历史回补：拉 1m -> 聚合 5m/15m/1h -> 落 Parquet
.venv/bin/python scripts/backfill.py CN.SHFE.rb2610 2026-08-20 2026-08-27

# 对拍验收：本地聚合 vs 接口原生高周期线，逐根比对
.venv/bin/python scripts/crosscheck.py CN.SHFE.rb2610 CN.SHFE.au2610

# 加密（OKX 公开行情，不需凭据）：WS 跑 6 分钟后与 REST 逐字段对拍
.venv/bin/python scripts/crypto_live_check.py 6

# 盯盘：实时行情 -> 指标/形态 -> 多级别规则 -> 推送（未配 TG/Bark 时只打控制台）
# 运行态存 data/runtime.sqlite3，重启后自动恢复状态机并补判停机期间漏掉的信号
.venv/bin/python scripts/watch.py --crypto-only --minutes 10

# M2 红线验收：live 落 Parquet -> ReplayFeed 回放 -> 信号逐条对拍
.venv/bin/python scripts/replay_check.py --offline --history-bars 500

# Web 面板：与引擎同进程（实时信号流 + 健康）
.venv/bin/python scripts/watch.py --crypto-only --web 127.0.0.1:8000
# 或独立只读（只看历史信号与统计，不碰行情）
.venv/bin/python scripts/serve.py

# 终端版信号质量报告（口径见 ADR-0008）
.venv/bin/python scripts/report.py --horizon 30 --cost-bps 2

# 纸上回测：用历史行情跑完整链路 Signal -> Intent -> RiskGate -> PaperBroker
.venv/bin/python scripts/paper_run.py --force-enable --write-db data/runtime.sqlite3

.venv/bin/python -m pytest -q && .venv/bin/ruff check src tests scripts && .venv/bin/mypy
```

## 当前状态

**M0-A 期货数据底座**：Quote API 客户端（TLS 指纹固定）、交易日历、标的注册表、
1m→高周期聚合、Parquet 落盘、实时轮询 Feed 均已实现并通过单测与实盘对拍。

**M0-B 加密数据底座**：OKX 永续 REST + WS Feed（断线重连自动回补）、
统一 `BarStore` as-of 视图（两个市场共用）已实现并通过单测与实时对拍。
交易所选型见 [ADR-0005](docs/adr/ADR-0005-crypto-exchange-okx.md)。

**M1 指标 + 形态 + 单级别预警**：增量指标（口径见 [ADR-0006](docs/adr/ADR-0006-indicator-conventions.md)）、
白名单 AST 表达式引擎与三档形态（表达式 / 结构原语 / Python 插件）、单级别规则引擎与推送，
已通过单测与实时行情真跑验证。

**M2 多级别规则引擎**（核心里程碑）：有序条件链路 + 状态机（推进/回退/TTL/冷却/去重）、
SQLite 运行态持久化与重启补判、ReplayFeed 回放。
红线"replay 与 live 逐条一致"已由实时真跑验收（见 [ROADMAP](docs/ROADMAP.md)）。

**M3 Web 只读面板 + 信号质量统计**：信号流 / K 线回看（信号标注、多周期联动）/ 运行健康 /
质量报告（胜率、期望收益、假信号率、分品种分时段）。前端是**无构建单页**（[ADR-0009](docs/adr/ADR-0009-panel-without-build-step.md)），
统计口径见 [ADR-0008](docs/adr/ADR-0008-signal-quality-metrics.md)。

**M4 策略层 + 纸上撮合**：Signal → Intent → RiskGate → PaperBroker，
撮合口径见 [ADR-0010](docs/adr/ADR-0010-paper-fill-conventions.md)（与统计口径共用同一套代码）。
图上买卖点的折叠口径与信号优先级见 [ADR-0012](docs/adr/ADR-0012-marker-collapse-and-priority.md)。
预警组（九标的同屏）的组装口径见 [ADR-0013](docs/adr/ADR-0013-watchlist-group.md)。
**默认关闭**，在 `config/trading.yaml` 里打开。

下一段是 **M5 实盘**（SimNow / 加密小仓）。

## 写一条规则

规则是 `config/rules/` 下的 YAML，加载时就会编译校验（函数名打错、语法错误都在启动时报出来）：

```yaml
id: volume-spike
universe: [CRYPTO.OKX.BTCUSDT.PERP, CN.SHFE.rb2610]
timeframe: 1m
conditions:
  - on: 1m
    mode: event                 # 边沿：上一根不成立、这一根成立
    when: "volume > sma(volume, 20) * 2.5 and close > open"
context:                        # 触发时快照，推送正文与事后统计都读它
  vol_ratio: "volume / sma(volume,20)"
  rsi14: "rsi(14)"
emit:
  direction: neutral
  cooldown: 5m
```

表达式走**白名单 AST**（属性访问、下标、lambda、`**` 等一律编译期拒绝），
可用函数含指标（`sma/ema/rsi/macd_*/boll_*/atr/kdj_*/std`）、
结构原语（`range/breakout/swing_high/swing_low/gap/engulfing/pin_bar/inside_bar/consolidation`）
与穿越（`cross_up/cross_down`）。指标预热期为 `None`，整条表达式按三值逻辑判为"不成立"。
