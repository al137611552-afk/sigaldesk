# ADR-0005 加密行情先行接入 OKX 永续（而非 Binance）

- 日期: 2026-08-28
- 状态: Accepted

## 背景
M0-B 要接加密数据底座。ROADMAP 原文是"binance/okx 择一先行"，`.env` 里预置了
`BINANCE_API_KEY`，`config/symbols.yaml` 里也先注册了 `CRYPTO.BINANCE.BTCUSDT.PERP`，
默认倾向是 Binance。开工前按本项目惯例先实测探接口，结果推翻了这个默认。

## 实测证据（2026-08-28，开发机）

| | Binance 合约 | OKX 永续 |
|---|---|---|
| REST K 线 | ✅ `fapi/v1/klines` 通 | ✅ `/api/v5/market/candles` 通 |
| WS 推送 | ❌ **握手成功但一帧数据都不来** | ✅ 通（`/ws/v5/business`） |
| 收盘标记 | `k.x`（仅 WS 有） | `confirm` 字段（**WS 与 REST 同源同字段**） |

Binance 合约 WS（`wss://fstream.binance.com`）连接能建立，订阅 `btcusdt@kline_1m`
与 `btcusdt@aggTrade` 两次复现、各等 40s+ 均零帧；同机 Binance **现货** WS
（`stream.binance.com:9443`）正常出数据。⇒ 是网络侧对 `fstream` 的限制，不是代码问题。

## 决策
1. **加密行情先接 OKX 永续**（`BTC-USDT-SWAP` / `ETH-USDT-SWAP`），WS 走
   `/ws/v5/business` 的 `candle1m` 频道，历史与断线回补走 `/api/v5/market/history-candles`。
2. `config/symbols.yaml` 中的 `CRYPTO.BINANCE.BTCUSDT.PERP` 替换为
   `CRYPTO.OKX.BTCUSDT.PERP`；`code` 即 OKX `instId`，`multiplier` 即 `ctVal`。
3. `.env` 里的 Binance 凭据保留不动，留给 M5 实盘再议。

## 权衡
- **优点（决定性）**：本机能完整验收。M0-B 的两条验收——"WS 与 REST 同一根 bar 完全一致"
  和"断线重连后自动回补"——若走 Binance 合约，在本机根本跑不了，只能挂着等 VPS。
- **优点**：OKX 的 `confirm` 字段让收盘判定**不依赖本地时钟**，比 Binance 现货那种
  "REST 靠位置推断、WS 靠 `k.x`"的两套判据更干净，天然满足 INV-2。
- **代价**：与 `.env` 里已备的 Binance 凭据错配。可接受——M0-B 只用公开行情，不需要凭据；
  M5 实盘再按届时的可达性重新拍板。
- **代价**：换所要改 symbols.yaml。成本很低，正是 `SymbolRegistry` 映射层存在的意义（ADR-0002）。

## 已知限制
- 结论只对**本开发机的网络位置**成立。VPS 若能连通 fstream，Binance 仍是可选项；
  `feed/okx.py` 与 `feed/okx_ws.py` 的纯逻辑/IO 分层已经把归一化隔开，
  再加一家只需新增一个归一化函数 + 一个 WS 适配，不动 BarStore 与上层。
- `feed/okx.py` 的归一化**只对 SWAP 成立**：它把 `volCcy` 当 volume、`volCcyQuote` 当 money。
  OKX 现货的 `vol`/`volCcy` 含义不同，若日后接现货必须另走一条归一化路径。
- OKX 亦有地域限制，VPS 选址时需一并验证。
