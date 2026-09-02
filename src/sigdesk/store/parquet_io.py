"""Parquet 落盘。分区 market/symbol/timeframe/date（ADR-0004）。

只写 closed=True 的 bar —— 进行中的 bar 不落盘，避免归档被临时值污染（INV-2）。
写入是幂等的：同一分区重写会整体替换，便于次日用 by-timerange 做权威回填校正。
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


def partition_key(bar: Bar) -> str:
    """分区日期。期货用交易日（夜盘与次日日盘落在同一分区），其余用 UTC 自然日。"""
    if bar.trading_day:
        return bar.trading_day
    return dt.datetime.fromtimestamp(bar.close_ts, dt.UTC).date().isoformat()


def partition_path(root: pathlib.Path, symbol: str, timeframe: Timeframe, day: str) -> pathlib.Path:
    market = symbol.split(".", 1)[0]
    return root / market / symbol / timeframe.value / f"{day}.parquet"


def partition_span(
    root: pathlib.Path, symbol: str, timeframe: Timeframe
) -> tuple[str, str] | None:
    """本地数据覆盖的 (首日, 末日)，没有数据返回 None。只列目录名，不读文件。

    **要首尾都给**：只看末日会把"补过近两个月"误判成"已覆盖两年"，
    于是拉长历史的请求被静默跳过 —— 数据看着有、其实短一大截。
    """
    d = partition_path(root, symbol, timeframe, "x").parent
    try:
        days = sorted(f.stem for f in d.iterdir() if f.suffix == ".parquet")
    except OSError:
        return None
    return (days[0], days[-1]) if days else None


def latest_partition(root: pathlib.Path, symbol: str, timeframe: Timeframe) -> str | None:
    """这个标的最后一个分区日（``YYYY-MM-DD``），没有数据返回 None。

    **只列目录名，不打开任何文件** —— 面板启动时要对每个标的问一遍，
    读文件会让 /api/meta 慢到肉眼可见。分区名本身就是日期（期货是交易日），
    拿它当"数据止于何时"足够准，也不需要解析 Parquet。
    """
    d = partition_path(root, symbol, timeframe, "x").parent
    try:
        days = [f.stem for f in d.iterdir() if f.suffix == ".parquet"]
    except OSError:
        return None
    return max(days) if days else None


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


def read_range(
    root: pathlib.Path, symbol: str, timeframe: Timeframe, start_ts: int, end_ts: int
) -> list[Bar]:
    """读取 (start_ts, end_ts] 区间的 bar。跨分区拼接，结果按 close_ts 升序。"""
    base = root / symbol.split(".", 1)[0] / symbol / timeframe.value
    if not base.exists():
        return []
    out: list[Bar] = []
    for path in sorted(base.glob("*.parquet")):
        out.extend(b for b in read_bars(path, symbol, timeframe) if start_ts < b.close_ts <= end_ts)
    out.sort(key=lambda b: b.close_ts)
    return out


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
    "read_bars",
    "read_range",
    "write_bars",
]
