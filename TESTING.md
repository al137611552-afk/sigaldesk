# signal-desk Windows 测试说明

## 一句话

**大部分可以交给 agent，一条命令跑完出结论。** 只有三件事必须你本人看。

---

## 给 agent 的（照抄即可）

```
任务：在这台 Windows 机器上验收 signal-desk。

0. 如果这台机器有 Node.js，先跑一次 npm i -g playwright
   （截图组要它；浏览器用系统 Chrome/Edge，**不要**跑 playwright install）。
   没有 Node.js 就跳过这步，截图组会自动记为「跳过」而不是失败。
1. 解压后进入 signal-desk 目录
2. 运行 run_acceptance.bat
3. 把完整输出贴回来（也会存在 acceptance-log.txt）。
   看末尾三行统计：
     · 「N/N 项通过」且没有失败项 → 通过
     · 有 ❌ 失败项 → 连同它下面那行说明一起报告，**不要改代码去凑通过**
     · 有 ⏭️ 跳过项 → 那是环境缺东西，不是缺陷，照报即可
4. 截图组通过的话，打开 docs\design\shots\ 看刚生成的 8 张图，
   报告有没有：文字被遮挡、元素重叠、明显留白、数字显示成 0 或乱码

不要做：不要联网取行情，不要填任何凭据，不要开实盘交易
（config\trading.yaml 的 enabled 必须保持 false）。
```

`run_acceptance.bat` 会**自己找 Python 3.12+**（系统默认的 `python` 常常是 3.11，
脚本会依次试 `py -3.14 / -3.13 / -3.12 / -3`、`python3`、`python`），建虚拟环境、
装依赖、跑完 5 组检查。全程离线，包里带了 3 个月的真实期货样本数据
（rb 主连，跨一次换月）。

**默认不 `pause`** —— 在 agent / CI 的非交互 shell 里 `pause` 会永远挂住。
双击运行想让窗口留住，用 `run_acceptance.bat --pause`。

---

## 验收脚本查什么（49 项）

| 组 | 内容 |
|---|---|
| 代码质量 | 624 项单测、ruff、mypy strict |
| 数据与日历 | 2026 节假日表生效、周末/国庆休市、节前不开夜盘、样本数据完整、主连换月平移量有记录 |
| 规则引擎 | 跨级别 `at('1d',...)` + 日线 + 双底一起编译、试算出信号、**同一批输入结果可复现**、预热期是「未知」不是「不成立」 |
| 面板 API | 全部端点、日线 bar、日线标注不除零、**写端点默认 403** |
| 规则读写 | 校验/新建/读回/更新/试算/删除全流程、沙箱逃逸被拒、参数个数错误被拒、**删除是归档不是真删** |
| 面板视觉 | 桩化冒烟不白屏、无头截图跑通、**无元素遮挡**、控制台无错误 |

单跑某一组：`python scripts\acceptance.py --only api`
没装 node/浏览器：`python scripts\acceptance.py --no-visual`
（不加也行 —— 缺 node 或缺 playwright 时视觉组会自动记为**跳过**，不算失败）

失败与跳过是两回事：**❌ 是真问题，⏭️ 是环境缺东西**。退出码只看失败项。

---

## 只能人工确认的（三件）

### 1. 信号点是不是你想要的进场点 ⭐ 最重要

这条谁也替不了你。步骤：

```bat
.venv\Scripts\python scripts\serve.py --port 8000 --allow-edit
```

浏览器开 http://127.0.0.1:8000

1. 「盯盘」页 → 标的选 **rb.CONT · 主连** → 周期 **1d**，确认日线画得出来、
   4 月 8 号换月处**没有假跳空**（后复权生效）
2. 「规则」页 → 新建 → 把下面这条贴进去 → 「历史试算」

```yaml
id: kan-da-zuo-xiao
description: 看大做小
universe: [CN.SHFE.rb.CONT]
timeframes: {daily: 1d, trend: 1h, setup: 5m, box: 5m}
conditions:
  - {on: daily, mode: state, when: "close > ema(close, 20)"}
  - {on: trend, mode: state, when: "abs(close - ema(close, 20)) / close < 0.006
      and abs(close - at('1d', ema(close, 20))) / close < 0.02"}
  - {on: setup, mode: event, when: "double_bottom(5, 0.002) or cross_up(macd_dif(), macd_dea())"}
  - {on: box,   mode: state, when: "consolidation(12, 0.004)"}
emit:
  direction: long
  ttl: 48 bars
  dedup_key: "{symbol}:{rule}:{trend_bar_close_ts}"
```

3. 回「盯盘」页，rb.CONT + 5m，**看图上那些箭头**：
   落的位置是不是你实际会进场的地方？太早？太晚？追高了？

**这一步的结论决定下一段做什么**，比任何测试都重要。参数（0.006 / 0.02 / 0.002 /
consolidation 的 12 与 0.004）都可以直接在页面上改了重新试算，不用改代码。

### 2. 推送真投递

`.env` 里的 `TELEGRAM_BOT_TOKEN` / `BARK_URL` 一直是空的，**推送从来没真发出去过**。
要验就得填你自己的凭据 —— 是否交给 agent 你自己决定，我的建议是**别给**，
自己填完跑一次 `scripts\watch.py --crypto-only --minutes 5` 看手机收不收得到。

### 3. 期货实时轮询跑满一个交易日（含夜盘）

要 `QUOTE_API_KEY` + 一整个交易日的时间。这是 M0-A 唯一没验的一条。
同样涉及凭据，你自己决定给不给 agent。

---

## 包里没有什么

- **没有 `.env`**（凭据不进分发包）。要用行情 API 自己复制 `.env.example` 填。
- 没有实盘交易配置。`config\trading.yaml` 的 `enabled: false`，**保持原样**。
- 没有设计稿画布源文件（2.5MB，与测试无关）。

## 环境要求

- **Python 3.12+**。`run_acceptance.bat` 会自己找（依次试 `py -3.14/-3.13/-3.12/-3`、
  `python3`、`python`），找不到会明确报错而不是用 3.11 硬跑。
- 视觉组另需 Node.js 与 Chrome/Edge。截图脚本会自动找浏览器；
  找不到就设 `SIGDESK_CHROME` 指向 exe。playwright 用
  `npm i -g playwright` 装（不用 `playwright install`，用系统浏览器）。
- **控制台编码**：脚本输出含 ✅ 这类字符，`run_acceptance.bat` 已经设了
  `PYTHONUTF8=1`；手动跑请先 `set PYTHONUTF8=1`，否则 cp936 控制台会
  `UnicodeEncodeError`。
