"""核心数据模型。全部为不可变值对象，不含任何 IO。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

CST: Final = dt.timezone(dt.timedelta(hours=8))  # 中国期货市场本地时区


class Market(StrEnum):
    CN_FUTURES = "CN"
    CRYPTO = "CRYPTO"


class Timeframe(StrEnum):
    """周期。value 为内部标识，同时是 Parquet 分区名。"""

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"
    MON1 = "1mon"

    @property
    def seconds(self) -> int:
        """周期长度（秒）。日/周/月都不是固定长度，此处返回 0 表示**不可墙钟分桶**。"""
        return _TF_SECONDS[self]

    @property
    def is_intraday(self) -> bool:
        return self.seconds > 0

    @property
    def is_calendar(self) -> bool:
        """按自然日历切分（日/周/月）。长度不固定，只能靠"日期键变了"来收盘。"""
        return not self.is_intraday

    @property
    def rank(self) -> int:
        """排序用的"周期长短"。日/周/月的 seconds 都是 0（不可墙钟分桶），
        直接按 seconds 排会把它们排到 1m 前面 —— 用这个属性排序，别用 seconds。"""
        return _TF_SECONDS[self] or _TF_RANK[self]


_TF_SECONDS: Final[dict[Timeframe, int]] = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.M30: 1800,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
    Timeframe.D1: 0,
    Timeframe.W1: 0,
    Timeframe.MON1: 0,
}

# 日历周期的排序权重（它们没有固定长度，但先后次序是确定的）
_TF_RANK: Final[dict[Timeframe, int]] = {
    Timeframe.D1: 24 * 3600,
    Timeframe.W1: 7 * 24 * 3600,
    Timeframe.MON1: 31 * 24 * 3600,
}

# Quote API 的 interval_range 编码（见 docs/ARCHITECTURE.md §3.2）
QUOTE_API_INTERVAL: Final[dict[Timeframe, int]] = {
    Timeframe.M1: 1,
    Timeframe.M5: 5,
    Timeframe.M15: 15,
    Timeframe.M30: 30,
    Timeframe.H1: 60,
    Timeframe.D1: 101,
}


@dataclass(frozen=True, slots=True)
class Bar:
    """一根 K 线。

    时间语义（INV-3）：同时携带 open_ts 与 close_ts，不依赖单一时间戳的隐含含义 ——
    Quote API 的分钟线给的是收盘时刻，日线给的是交易日编码，两者不可混用。

    closed（INV-2）：数据源返回的最后一根永远是进行中的 bar，其 OHLCV 还会变。
    规则引擎默认只消费 closed=True 的 bar。
    """

    symbol: str  # 内部 symbol uid，如 CN.SHFE.rb2610
    timeframe: Timeframe
    open_ts: int  # 秒级 UTC epoch，bar 覆盖区间的左端（开）
    close_ts: int  # 秒级 UTC epoch，bar 覆盖区间的右端（闭）
    open: float
    high: float
    low: float
    close: float
    volume: float
    money: float = 0.0
    open_interest: float = 0.0
    closed: bool = True
    trading_day: str | None = None  # 期货交易日 YYYY-MM-DD，与自然日解耦

    def __post_init__(self) -> None:
        if self.close_ts <= self.open_ts:
            raise ValueError(f"close_ts 必须晚于 open_ts: {self.open_ts} -> {self.close_ts}")
        if self.low > self.high:
            raise ValueError(f"low({self.low}) > high({self.high})")


@dataclass(frozen=True, slots=True)
class Symbol:
    """标的及其三方代码映射。SymbolRegistry 是唯一事实源（ADR-0002）。"""

    uid: str  # CN.SHFE.rb2610 / CRYPTO.BINANCE.BTCUSDT.PERP
    market: Market
    exchange: str
    code: str  # 交易所原始代码
    calendar: str  # 交易日历 id
    quote_code: str | None = None  # Quote API 代码
    ctp_code: str | None = None  # CTP 交易代码
    ccxt_symbol: str | None = None  # ccxt 符号
    price_tick: float = 0.0
    multiplier: float = 1.0
    product: str | None = None  # 品类，如 rb（用于主力换月查询）
    # 主连拼接用的指数代码，如 rb9999。**只用于查 main-by-date 换月区间，不用于拉 K 线**
    # —— 数据源自己的主连序列口径不可复现（CLAUDE.md 坑#9）。
    main_code: str | None = None
    # True = 8888/9999 之类的合成序列。这类数据在两个接口间口径不一致，
    # 禁止进入回测与统计（见 CLAUDE.md 坑#9），仅供粗略看图。
    is_continuous: bool = False
