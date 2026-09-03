"""Parquet 落盘。分区 market/symbol/timeframe/<分区键>（ADR-0004）。

只写 closed=True 的 bar —— 进行中的 bar 不落盘，避免归档被临时值污染（INV-2）。
写入是幂等的：同一分区重写会整体替换，便于次日用 by-timerange 做权威回填校正。

**分区粒度随周期而变**（`partition_unit`）：
  - 分钟级（1m~4h）按**交易日**：1m 一天 1440 根，一天一个文件正合适。
  - 日历级（1d/1w/1mon）按**年**：一天才一根，按日分就是**一行一个文件** ——
    实测 998 根日线摊在 975 个文件里，读一次 789ms、占 3005 KB；
    并成一个文件后 3ms、59 KB（**快 278 倍、小 51 倍**）。多出来的全是
    每个文件那份 schema + footer。这是 Parquet 生态里最经典的 small files problem。

    代价是写放大：每天收一根日线要重写整年（约 250 行）。实测 2.2ms
    （原来单行文件 1.2ms），66 标的 x 3 个日历周期每天多花约 0.4 秒 —— 可忽略。
    **分钟级绝不能这么改**：那会变成每分钟重写一个几万行的文件。
"""

from __future__ import annotations

import datetime as dt
import pathlib
from collections import defaultdict

import pyarrow as pa
import pyarrow.parquet as pq

from ..core.models import CST, Bar, Timeframe

SCHEMA = pa.schema(
    [
        ("open_ts", pa.int64()),
        ("close_ts", pa.int64()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.float64()),
        ("money", pa.float64()),
        ("open_interest", pa.float64()),
        ("trading_day", pa.string()),
    ]
)


def partition_day(bar: Bar) -> str:
    """这根 bar 归属的日期。期货用交易日（夜盘归次日），其余用 UTC 自然日。"""
    if bar.trading_day:
        return bar.trading_day
    return dt.datetime.fromtimestamp(bar.close_ts, dt.UTC).date().isoformat()


def partition_unit(timeframe: Timeframe) -> str:
    """这个周期按什么粒度分区：``"day"`` 或 ``"year"``（理由见模块 docstring）。"""
    return "year" if timeframe.is_calendar else "day"


def partition_key(bar: Bar) -> str:
    """分区文件名（不含扩展名）。日历周期是 ``YYYY``，其余是 ``YYYY-MM-DD``。"""
    day = partition_day(bar)
    return day[:4] if partition_unit(bar.timeframe) == "year" else day


def partition_path(root: pathlib.Path, symbol: str, timeframe: Timeframe, day: str) -> pathlib.Path:
    market = symbol.split(".", 1)[0]
    return root / market / symbol / timeframe.value / f"{day}.parquet"


def _days_in(path: pathlib.Path) -> tuple[str, str] | None:
    """一个分区文件覆盖的 (首日, 末日)，**只读 Parquet 统计信息不读数据**。

    统计信息本来就是干这个用的（row group 的 min/max）。按年分区后一个标的
    只有两三个文件，比原来 iterdir 上千个文件还快。

    `trading_day` 对加密是 None（没有交易日概念），这时退回用 close_ts 的 UTC 日期。
    """
    try:
        md = pq.read_metadata(path)
    except Exception:
        return None
    names = md.schema.names
    def stat(col: str) -> tuple[object, object] | None:
        if col not in names:
            return None
        i = names.index(col)
        lo = hi = None
        for g in range(md.num_row_groups):
            st = md.row_group(g).column(i).statistics
            if st is None or st.null_count == st.num_values:
                continue
            lo = st.min if lo is None else min(lo, st.min)
            hi = st.max if hi is None else max(hi, st.max)
        return None if lo is None else (lo, hi)

    if (d := stat("trading_day")) is not None:
        return (str(d[0]), str(d[1]))
    if (t := stat("close_ts")) is not None:
        def to_day(v: object) -> str:
            return dt.datetime.fromtimestamp(int(str(v)), dt.UTC).date().isoformat()
        return (to_day(t[0]), to_day(t[1]))
    return None


def _partition_days(root: pathlib.Path, symbol: str, timeframe: Timeframe) -> list[str]:
    """本地这个 (标的, 周期) 覆盖到的日期，升序去重。

    **按日分区时文件名就是日期，不必打开文件**（面板启动要对每个标的问一遍）。
    按年分区时文件名只有年份，才去读统计信息。
    """
    d = partition_path(root, symbol, timeframe, "x").parent
    try:
        files = [f for f in d.iterdir() if f.suffix == ".parquet"]
    except OSError:
        return []
    if partition_unit(timeframe) != "year":
        return sorted(f.stem for f in files)
    out: list[str] = []
    for f in files:
        if (span := _days_in(f)) is not None:
            out.extend(span)
    return sorted(out)


def partition_span(
    root: pathlib.Path, symbol: str, timeframe: Timeframe
) -> tuple[str, str] | None:
    """本地数据覆盖的 (首日, 末日)，没有数据返回 None。

    **要首尾都给**：只看末日会把"补过近两个月"误判成"已覆盖两年"，
    于是拉长历史的请求被静默跳过 —— 数据看着有、其实短一大截。
    """
    days = _partition_days(root, symbol, timeframe)
    return (days[0], days[-1]) if days else None


def latest_partition(root: pathlib.Path, symbol: str, timeframe: Timeframe) -> str | None:
    """这个标的最后一个有数据的日期（``YYYY-MM-DD``），没有数据返回 None。

    面板拿它当"数据止于何时"。按日分区时直接用文件名，不打开任何文件。
    """
    days = _partition_days(root, symbol, timeframe)
    return days[-1] if days else None


def write_bars(root: pathlib.Path, bars: list[Bar]) -> list[pathlib.Path]:
    """按分区写入，返回写过的文件。同一分区内按 close_ts 排序去重，后来者覆盖。"""
    groups: dict[tuple[str, Timeframe, str], dict[int, Bar]] = defaultdict(dict)
    for b in bars:
        if not b.closed:
            continue  # 进行中的 bar 不落盘
        groups[(b.symbol, b.timeframe, partition_key(b))][b.close_ts] = b

    written: list[pathlib.Path] = []
    for (symbol, tf, day), by_ts in groups.items():
        path = partition_path(root, symbol, tf, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():  # 合并已有分区，新数据覆盖同 close_ts 的旧数据
            for old in read_bars(path, symbol, tf):
                by_ts.setdefault(old.close_ts, old)
        ordered = [by_ts[t] for t in sorted(by_ts)]
        table = pa.table(
            {
                "open_ts": [b.open_ts for b in ordered],
                "close_ts": [b.close_ts for b in ordered],
                "open": [b.open for b in ordered],
                "high": [b.high for b in ordered],
                "low": [b.low for b in ordered],
                "close": [b.close for b in ordered],
                "volume": [b.volume for b in ordered],
                "money": [b.money for b in ordered],
                "open_interest": [b.open_interest for b in ordered],
                "trading_day": [b.trading_day for b in ordered],
            },
            schema=SCHEMA,
        )
        pq.write_table(table, path, compression="zstd")
        written.append(path)
    return written


def read_bars(path: pathlib.Path, symbol: str, timeframe: Timeframe) -> list[Bar]:
    if not path.exists():
        return []
    t = pq.read_table(path, schema=SCHEMA).to_pylist()
    return [
        Bar(
            symbol=symbol,
            timeframe=timeframe,
            open_ts=r["open_ts"],
            close_ts=r["close_ts"],
            open=r["open"],
            high=r["high"],
            low=r["low"],
            close=r["close"],
            volume=r["volume"],
            money=r["money"],
            open_interest=r["open_interest"],
            closed=True,
            trading_day=r["trading_day"],
        )
        for r in t
    ]


def _partition_overlaps(stem: str, timeframe: Timeframe, start_ts: int, end_ts: int) -> bool:
    """这个分区**可能**含有 (start_ts, end_ts] 内的 bar 吗。

    必须**保守**：宁可多读一个文件，也不能漏掉一个 —— 漏读表现为"数据少了一段"，
    图上看着正常、规则静默不触发，是这个项目最危险的失效类型。

    所以两边各留一天余量：分区键用的是**交易日**，而这里只有 close_ts 的 UTC 日期，
    两者能差一天（夜盘 23:00 CST 的 bar 属于次日交易日）。
    """
    if start_ts >= end_ts:
        return False
    day = dt.timedelta(days=1)
    lo = (dt.datetime.fromtimestamp(max(start_ts, 0), dt.UTC) - day).date().isoformat()
    try:
        hi = (dt.datetime.fromtimestamp(end_ts, dt.UTC) + day).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return True          # end_ts 大到溢出（默认就是 2**31）= 不设上界
    if partition_unit(timeframe) == "year":
        return lo[:4] <= stem <= hi[:4]
    return lo <= stem <= hi


def read_range(
    root: pathlib.Path, symbol: str, timeframe: Timeframe, start_ts: int, end_ts: int
) -> list[Bar]:
    """读取 (start_ts, end_ts] 区间的 bar。跨分区拼接，结果按 close_ts 升序。

    **先按文件名裁分区，再读**（partition pruning）。文件名本身就是日期，
    原来却把整个目录全读进来再逐行过滤 —— 要一天的数据也得读完两年。
    裁剪只是少读文件，逐行的时间过滤照旧，所以结果逐根不变（有测试钉住）。

    **按 close_ts 去重**：迁移期同一根 bar 可能同时存在于旧的按日分区和
    新的按年分区里（compact 跑完前）。不去重就会重复计数，而且完全看不出来。
    """
    base = root / symbol.split(".", 1)[0] / symbol / timeframe.value
    if not base.exists():
        return []
    by_ts: dict[int, Bar] = {}
    for path in sorted(base.glob("*.parquet")):
        if not _partition_overlaps(path.stem, timeframe, start_ts, end_ts):
            continue
        for b in read_bars(path, symbol, timeframe):
            if start_ts < b.close_ts <= end_ts:
                by_ts[b.close_ts] = b
    return [by_ts[t] for t in sorted(by_ts)]


def count_bars(root: pathlib.Path, symbol: str, timeframe: Timeframe) -> int:
    """本地一共有多少根，**只读 Parquet 元数据不读行数据**。

    面板标题要显示"共 N 根（显示最近 M 根）"。为了这一个数字把几万根读进内存
    是划不来的 —— 行数本来就写在每个文件的 footer 里。
    """
    base = root / symbol.split(".", 1)[0] / symbol / timeframe.value
    if not base.exists():
        return 0
    total = 0
    for f in base.glob("*.parquet"):
        try:
            total += pq.read_metadata(f).num_rows
        except Exception:
            continue
    return total


def read_tail(root: pathlib.Path, symbol: str, timeframe: Timeframe, n: int) -> list[Bar]:
    """最后 n 根。**从最新的分区往回读，够了就停。**

    面板画一屏只要几百根，却一直是"把全部读进来再截尾" ——
    1m 读 3 万根只为画 220 根（实测 137ms）。分区裁剪救不了它：
    面板发的是 start_ts=0 的无界区间，所有分区都"可能相关"。

    行数从文件元数据拿（不读行数据），所以"够了没有"这个判断本身是廉价的。
    仍按 close_ts 去重 —— 迁移期新旧布局并存时同一根 bar 会出现两次。
    """
    if n <= 0:
        return []
    base = root / symbol.split(".", 1)[0] / symbol / timeframe.value
    if not base.exists():
        return []
    files = sorted(base.glob("*.parquet"))
    picked: list[pathlib.Path] = []
    got = 0
    for f in reversed(files):
        picked.append(f)
        try:
            got += pq.read_metadata(f).num_rows
        except Exception:
            got = 0          # 元数据读不出来就别提前收手，退化成全读
        if got >= n:
            break
    by_ts: dict[int, Bar] = {}
    for f in sorted(picked):
        for b in read_bars(f, symbol, timeframe):
            by_ts[b.close_ts] = b
    return [by_ts[t] for t in sorted(by_ts)][-n:]


def gaps(bars: list[Bar], timeframe: Timeframe) -> list[tuple[int, int]]:
    """相邻 bar 间隔超过一个周期的位置。期货的交易时段间断也会命中 ——
    调用方需结合日历区分"正常休盘"与"真缺口"。"""
    period = timeframe.seconds
    if period <= 0:
        return []
    return [
        (a.close_ts, b.close_ts)
        for a, b in zip(bars, bars[1:], strict=False)
        if b.close_ts - a.close_ts > period
    ]


__all__ = [
    "partition_span",
    "CST",
    "gaps",
    "partition_key",
    "partition_path",
    "count_bars",
    "read_bars",
    "read_range",
    "read_tail",
    "write_bars",
]
