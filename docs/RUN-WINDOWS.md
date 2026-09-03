# 在 Windows 上跑起来

面向**自己用**（不是验收）。验收看 [TESTING.md](../TESTING.md)。

全程 PowerShell。所有路径用反斜杠，虚拟环境的 Python 在 `.venv\Scripts\python.exe`
（Linux 是 `.venv/bin/python`，README 里的命令要照着换）。

---

## 一、装

### 1. Python 3.12 或更高

```powershell
py -0p          # 列出这台机器上所有 Python
```

看有没有 3.12+。没有就去 python.org 装，**安装时勾上 "Add python.exe to PATH"**。

> `python` 常常指向 3.11 甚至更老。本项目 `requires-python = ">=3.12"`，
> 3.11 会在装依赖时报错。下面一律用 `py -3.12` 显式指定。

### 2. 取代码

```powershell
cd $HOME
git clone https://github.com/al137611552-afk/sigaldesk.git signal-desk
cd signal-desk
```

### 3. 建虚拟环境、装依赖

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -U pip
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

装完自检（**不需要任何凭据、不联网**）：

```powershell
.venv\Scripts\python.exe -m pytest -q
```

应当是全部通过（当前 798 项，会随开发增加）。到这一步就说明代码在这台机器上是好的。

---

## 二、配凭据

**加密行情（OKX）不需要任何凭据**，想先看效果可以跳过这一节，直接到「三」的加密那条。

期货行情要自有 Quote API 的 key。用交互式脚本写进**用户级**目录，
以后换目录、换版本都不用重配：

```powershell
.venv\Scripts\python.exe scripts\setup_env.py
```

它写到 `C:\Users\<你>\.signal-desk\.env`，**配一次就够** ——
以后换目录、换分支、换新版本的包都自动读到它，不用重配。
填完地址和 key 它会问要不要顺手把 TLS 指纹抓了，回车即可。

之后随时查状态（**只显示键名，不显示值**）：

```powershell
.venv\Scripts\python.exe scripts\setup_env.py --show
```

指纹单独重抓（换了证书之后要做一次）：

```powershell
.venv\Scripts\python.exe scripts\pin_tls.py --write
```

> 三个坑：
> - **不要**把 key 写进任何 `.ps1` / `.bat` / 提交里。`.env` 已在 `.gitignore`。
> - **不要**为了图省事关掉证书校验。请求头带着 key，关掉等于放弃 MITM 防护。
> - 推送（TG / Bark）留空就只打控制台，不影响盯盘。

---

## 三、双击就能跑（推荐）

项目根目录有两个 `.bat`，**双击即可**，不用记命令：

| 文件 | 干什么 |
|---|---|
| **`启动面板.bat`** | 启动盯盘 + 落盘 + 面板，约 8 秒后自动打开浏览器。**关掉窗口就是停止。** |
| **`更新程序.bat`** | 拉最新代码 + 更新依赖 + 跑一遍自检。**必须先关掉「启动面板」那个窗口。** |

两个脚本都会在出错时停住并把原因写在窗口里（不会一闪而过）。
它们各自会先检查：虚拟环境在不在、8000 端口是不是已经被占用、
（更新时）本地有没有未提交的改动、是不是 git 仓库。任何一条不对都会拦下来并说明怎么办。

> **想开机自动跑**请看下面「七、开机自动跑」——用任务计划程序，别把 `.bat` 丢进启动项。

### 为什么不做成 exe

打包成单个 exe（PyInstaller）在这个项目上是净亏：体积 150–300MB、
杀毒软件误报是常态、**每次改代码都要重新打包**（`git pull` 更新流程就废了）、
崩溃栈指向临时目录很难排查。而且它**一点也不会让面板变快** ——
实测切一次标的 677ms 里，浏览器渲染只占 59ms，其余是服务端往返；
exe 打的是同一个 CPython，Electron 用的是同一个浏览器内核。

## 三之二、手动跑（要传参数时用）

**长期运行的只有两个入口**，其余脚本都是跑完就退的工具（见 README 的「工具箱」）：

| 想干什么 | 跑这个 |
|---|---|
| 日常盯盘 | `scripts\watch.py --web 127.0.0.1:8000` |
| 已经有一个在盯了，只想再看一眼 | `scripts\serve.py`（只读）|

**别同时跑两个 `watch.py`** —— 状态机和去重表在同一个 SQLite 里，
同一根 bar 会被判两次、重复报警。它自己会检测并拒绝，但正确做法是用 `serve.py`。

### 只看加密（最快，不需要凭据）

```powershell
.venv\Scripts\python.exe scripts\watch.py --crypto-only --web 127.0.0.1:8000
```

浏览器开 http://127.0.0.1:8000 。第一次会先拉几百根历史预热，
预热期间**不发信号**（那些是过去的行情，报了也没用）。

### 期货 + 加密

先回补历史（**首次必须做**：没有历史就没有均线、没有形态，规则一条都不会触发）：

```powershell
.venv\Scripts\python.exe scripts\backfill.py CN.SHFE.rb2610 2026-08-01 2026-09-01
.venv\Scripts\python.exe scripts\backfill.py CN.SHFE.au2610 2026-08-01 2026-09-01
```

**一条命令补全部**（注册表里有 60 多个品种，逐个敲不现实）：

```powershell
.venv\Scripts\python.exe scripts\backfill_all.py --timeframe 1d --start 2024-01-01
```

一个品种失败不影响其余，末尾汇总；中断后重跑很便宜（已补好的会跳过）。
实测 64 个品种约 5 分钟。

**想看长历史的日线/周线/月线，别去回补长区间的 1m。** 高周期原本全靠 1m 聚合，
回补三个月 1m 只得到 45 根日线、10 根周线、3 根月线；而拉两年 1m 每个品种约 12 万根。
接口原生支持日线，直接拉：

```powershell
.venv\Scripts\python.exe scripts\backfill.py CN.SHFE.rb2610 2024-01-01 2026-09-01 --timeframe 1d
```

两种模式**别对同一区间都跑**：接口日线是交易所口径，1m 聚合出的日线是本项目的
交易日归属（夜盘归下一交易日），对夜盘品种可能不同，同一分区后写覆盖先写。
正确用法是分段不重叠 —— 远期用 `--timeframe 1d`，近期用默认的 1m。

然后：

```powershell
.venv\Scripts\python.exe scripts\watch.py --web 127.0.0.1:8000
```

### 只看历史、不碰行情

```powershell
.venv\Scripts\python.exe scripts\serve.py
```

想在面板里改规则、跑历史试算，加 `--allow-edit`
（**只允许绑回环地址** —— 面板没有鉴权）：

```powershell
.venv\Scripts\python.exe scripts\serve.py --allow-edit
```

停止：在窗口里按 `Ctrl+C`。运行态存在 `data\runtime.sqlite3`，
重启后自动恢复状态机、补判停机期间漏掉的信号，**不丢报也不重报**。

---

## 四、面板怎么用

| 位置 | 东西 |
|---|---|
| 右侧 | 信号流（跨标的混排），点一条切到那个标的 |
| 顶部「九宫格」 | 打开网格，默认落在**预警组**：九个标的同屏、整组一个周期 |
| 网格里的模式切换 | 预警组（九标的×一周期） ↔ 九周期（一标的×九周期）|
| 格子上的钉图标 | 悬停出现。钉住 = 「还需要观察」，不会被新信号挤掉，**并且会开始采集它** |
| 双击一格 | 放大，`Esc` 返回 |
| shift + 单击 | 锁定该时刻，九格十字线对齐 |

看大做小的路子：预警组横着扫 → 双击进九周期定方向 → 回单图找买点。

---

## 五、更新到最新版

**不用重新下载压缩包。** 你是 `git clone` 装的，`git pull` 就够了。

```powershell
cd $HOME\signal-desk

# 1. 先停掉盯盘进程（Ctrl+C，或任务计划里停掉那个任务）
#    正在写盘的时候更新代码/搬数据都会出事

# 2. 拉代码
git pull

# 3. 依赖有变动时才需要（pyproject.toml 改了就跑一次，跑了也没坏处）
.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 4. 自检
.venv\Scripts\python.exe -m pytest -q

# 5. 重新启动
.venv\Scripts\python.exe scripts\watch.py --web 127.0.0.1:8000
```

**你的数据不会被 `git pull` 碰到。** `data\`、`*.parquet`、`*.sqlite3` 都在
`.gitignore` 里，凭据在 `C:\Users\<你>\.signal-desk\.env`（也在仓库外）。
所以更新代码不会丢历史行情、不会丢信号、不会要你重配凭据。

### 数据格式变了的那次：跑一次 compact

**只需要跑这一次**（2026-09-03 之后拉的代码）。日/周/月线的分区方式改了 ——
原来每个交易日一个文件，日线就成了**一行一个文件**（实测 998 根日线摊在 975 个文件里，
读一次 789ms）。现在按年分，读同样的数据只要 3ms。

```powershell
# 盯盘进程必须是停着的
.venv\Scripts\python.exe scripts\compact.py --dry-run   # 先看要动什么
.venv\Scripts\python.exe scripts\compact.py             # 真跑
```

它**先写新分区、逐根校验一致、才删旧文件**；校验不过就一个都不删。
幂等 —— 再跑一次会说「没有需要合并的分区」。中途断了也不会坏数据，再跑一次即可。

不跑也能用（读的时候会自动去重兜底），只是快不起来。

### 怎么知道该不该更新

```powershell
git log --oneline -5 HEAD..origin/main   # 先 git fetch，再看差了哪些提交
```

CHANGELOG.md 里记着每次改了什么、哪些需要额外动作。

## 六、备份

要备份的只有两样：

```
data\runtime.sqlite3      信号、状态机、纸上账户、预警组的钉住
data\bars\                行情 Parquet（可以重新回补，但很花时间）
```

**`runtime.sqlite3` 必须连 `-wal` 一起拷**，只拷主文件会丢最近的信号
（SQLite 用的是 WAL 模式，新写入先进 `-wal`）。最省心的办法是**在进程停着的时候**
整个目录一起拷，或者用 SQLite 自带的备份：

```powershell
.venv\Scripts\python.exe -c "import sqlite3,sys; s=sqlite3.connect('data/runtime.sqlite3'); d=sqlite3.connect('backup.sqlite3'); s.backup(d)"
```

凭据在 `C:\Users\<你>\.signal-desk\.env`，**单独存，别跟代码放一起**。

## 七、开机自动跑（可选）

用「任务计划程序」，别用 `.bat` 双击 —— 关窗口就断了。

1. 任务计划程序 → 创建任务
2. 常规：勾「不管用户是否登录都要运行」、勾「使用最高权限运行」
3. 触发器：登录时 / 启动时
4. 操作：
   - 程序：`C:\Users\<你>\signal-desk\.venv\Scripts\python.exe`
   - 参数：`scripts\watch.py --web 127.0.0.1:8000`
   - 起始于：`C:\Users\<你>\signal-desk`

凭据从 `C:\Users\<你>\.signal-desk\.env` 读，任务计划里不用配环境变量。

---

## 八、出问题先看这几条

| 现象 | 多半是 |
|---|---|
| `pip install` 报 Python 版本 | 用成 3.11 了。`py -0p` 确认，用 `py -3.12` |
| 面板打得开但图是空的 | 没回补历史，或那个标的没有规则盯着（格子里会写明）|
| 期货连不上、加密正常 | key 或 TLS 指纹没配 / 证书换了指纹对不上。先 `setup_env.py --show` 看键在不在，再 `pin_tls.py --write` 重抓 |
| 一直没有信号 | 正常。规则是多级别链路，几小时才一条很常见；「运行健康」页看行情有没有在进 |
| 日/周/月线只有几根 | 高周期由 1m 聚合，1m 回补多长就只有多少根。用 `--timeframe 1d` 直接拉长历史 |
| 下拉框里的标的点进去是空图 | 看它的标注：`无数据`（没回补/行情没接入）、`未盯`（没规则盯它，不采）、`数据止于 X`（停更）|
| 提示"已有一个盯盘进程在用同一个运行态" | 就是字面意思。想再看一眼用 `serve.py`，别起第二个 `watch.py` |
| 预警组里钉不动 | 面板绑了非回环地址。钉住会触发采集，那时会被拒绝 |
| 端口被占 | `--web 127.0.0.1:8010`，或 `serve.py --port 8010` |

诊断命令（**只打印键名，不打印值**）：

```powershell
.venv\Scripts\python.exe scripts\setup_env.py --show
.venv\Scripts\python.exe scripts\acceptance.py --no-visual
```
