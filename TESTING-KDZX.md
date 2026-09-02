# 「看大做小」全品种真机测试

规则文件：`config/rules/kdzx-long.yaml` / `kdzx-short.yaml`（7 个品种：rb/au/cu/IF/m + BTC/ETH）

## 一、先配凭据（只需一次）

```bat
python scripts\setup_env.py
```
写进 `%USERPROFILE%\.signal-desk\.env`，**以后换新版本的包都不用再配**。
只想确认配没配：`python scripts\setup_env.py --show`（不显示值）。

## 二、回补历史 —— 这一步不能省

规则用 1h 的 `ema(20)`，**没有历史就永远预热不完、一条都不报**。

```bat
.venv\Scripts\python scripts\backfill.py CN.SHFE.rb2610  2026-07-01 2026-08-31
.venv\Scripts\python scripts\backfill.py CN.CFFEX.IF2609 2026-07-01 2026-08-31
.venv\Scripts\python scripts\backfill.py CN.DCE.m2701    2026-07-01 2026-08-31
:: au / cu 数据量大（夜盘到 02:30），分月拉，一次两个月会超时
.venv\Scripts\python scripts\backfill.py CN.SHFE.au2610  2026-07-01 2026-07-31
.venv\Scripts\python scripts\backfill.py CN.SHFE.au2610  2026-08-01 2026-08-31
.venv\Scripts\python scripts\backfill.py CN.SHFE.cu2610  2026-07-01 2026-07-31
.venv\Scripts\python scripts\backfill.py CN.SHFE.cu2610  2026-08-01 2026-08-31
```
加密不用回补，`watch.py` 启动时会自己取 4000 根。

## 三、盯盘

```bat
.venv\Scripts\python scripts\watch.py --web 127.0.0.1:8000
```
浏览器开 http://127.0.0.1:8000 ，右上角把「声音」「播报」点开（点开会响一声示范）。

启动时看这两行：
- `本次盯盘的市场: 加密、期货` —— 少了哪个，上面会有 ⚠️ 说明原因
- `首次预热：N 根 1m -> 派生 M 根高周期` —— N 应该是 2 万上下

## 四、看什么（按重要性排）

1. **信号点落的位置对不对** —— 这条最重要，只有你能判断。
   图上 **绿圆 B = 买点，红圆 S = 卖点**。点一条信号会画出触发价横线。
   太早？太晚？追高了？结论决定下一步调什么。
2. **参数在面板上直接调** —— 「规则」页改完点「历史试算」立刻看结果，不用重启：
   - `zone` 的 `atr(14) * 0.8` —— 调大 = 允许离均线更远 = 信号更多
   - `trend` 的 `ema(close, 20)` —— 调大 = 更看长期方向 = 信号更少
   - `box` 的 `consolidation(12, 0.005)` —— 调大 = 对箱体要求更松
3. **九宫格对照** —— 双击放大、悬停九格同步十字线、shift+单击锁定、Esc 返回。
4. **提醒真不真的响** —— 弹窗/声音/播报是即时的；要手机收推送得先填 TG/Bark 凭据。

## 五、这套规则目前的历史表现（别当结论看）

7 个品种、2026-07-01~08-31、持有 24 根 5m、止损 0.4%/止盈 0.8%、单边 1bp：

| | 信号 | 胜率 | 期望/条 | 盈亏比 |
|---|---:|---:|---:|---:|
| **多头** | 211 | 51.2% | +0.030% | 1.19 |
| **空头** | 128 | 34.4% | −0.067% | 1.21 |

- **多头略正、空头为负**，样本区间大概率偏多头行情，别据此认定"空头规则没用"。
- 分品种差异很大：m2701/BTC/ETH 多头正，rb 多头负（−0.136%）；IF 空头最差（14.3% 胜率）。
- **两个月、单一参数、没做样本外验证** —— 这是**基线**，不是结论。

## 六、想换更大的级别

把 `timeframes` 里的 `trend: 1h` 改成 `4h` 或 `1d` 即可，但**先确认那个周期有足够历史**
（1h 用 ema(20) 需要约 3.5 个交易日；4h 需要约 10 个；1d 需要 20 个交易日），
否则一条都不报。九宫格里对应格子有多少根 bar，一眼就能看出来够不够。
