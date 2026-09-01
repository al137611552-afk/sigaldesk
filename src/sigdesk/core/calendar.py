"""交易日历。纯逻辑，配置从 YAML 载入后传入。

两件事：
1. **交易日归属**：夜盘属于下一个交易日（08-27 21:00 的 bar 属于 08-28 交易日）。
2. **session 归属**：判断某时刻是否在交易时段内，用于轮询调度与数据完整性自检。

注意：分桶**不需要**日历（见 core/timeframes.py，纯墙钟对齐）。日历只用于交易日归属、
轮询调度和缺口检测。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from .models import CST


@dataclass(frozen=True, slots=True)
class Session:
    """交易节。以北京时间的「当日分钟数」表示；跨零点的节 end_min 会 >= 1440。"""

    start_min: int
    end_min: int

    @staticmethod
    def parse(text: str) -> Session:
        """'21:00-23:00' / '21:00-02:30'（跨日）。"""
        a, b = text.split("-")
        s = _hhmm(a)
        e = _hhmm(b)
        if e <= s:
            e += 1440  # 跨零点
        return Session(s, e)

    @property
    def crosses_midnight(self) -> bool:
        return self.end_min > 1440

    @property
    def is_night(self) -> bool:
        """夜盘：20:00 之后开的那一节。它归属**下一个**交易日，也因此有独立的休市规则。"""
        return self.start_min >= 20 * 60


def _as_iso(value: object) -> str:
    """把 YAML 可能给出的 date / datetime / str 统一成 YYYY-MM-DD。"""
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value).strip()


def _hhmm(text: str) -> int:
    h, m = text.strip().split(":")
    return int(h) * 60 + int(m)


@dataclass(frozen=True, slots=True)
class MarketCalendar:
    """一个品种（或一组品种）的交易时段定义。"""

    calendar_id: str
    sessions: tuple[Session, ...]
    holidays: frozenset[str] = frozenset()  # YYYY-MM-DD，节假日（不开盘的自然日）
    # 7×24 市场（加密）置 True。**默认 False** —— 大多数市场周末休市，
    # 忘了配的后果是周末误判成休市，比反过来安全。
    trades_on_weekends: bool = False

    @staticmethod
    def from_config(
        calendar_id: str,
        session_texts: list[str],
        holidays: Sequence[object],
        trades_on_weekends: bool = False,
    ) -> MarketCalendar:
        """从配置构造。

        **holidays 一律归一成 ISO 字符串**：PyYAML 会把不带引号的 `2026-01-01`
        解析成 `datetime.date` 对象，而内部一律按字符串比对 —— 不归一的话
        整张节假日表会静默失效（写了等于没写，且没有任何报错）。
        与 M2 那个 `on:` 被解析成布尔 True 是同一类坑。
        """
        return MarketCalendar(
            calendar_id=calendar_id,
            sessions=tuple(Session.parse(t) for t in session_texts),
            holidays=frozenset(_as_iso(h) for h in holidays),
            trades_on_weekends=trades_on_weekends,
        )

    @property
    def has_night_session(self) -> bool:
        return any(s.start_min >= _hhmm("20:00") for s in self.sessions)

    def is_trading_date(self, date: dt.date) -> bool:
        """该自然日是否开市。周末与法定节假日都不开。

        注意**调休补班的周末对期货不适用** —— 国务院把某个周六算作上班日，
        期货市场照样休市。所以这里只看"是不是周末"和"在不在节假日表里"，
        不需要补班日清单。
        """
        if not self.trades_on_weekends and date.weekday() >= 5:
            return False
        return date.isoformat() not in self.holidays

    def opens_night(self, date: dt.date) -> bool:
        """该自然日的**晚上**是否开夜盘。

        规则：法定节假日前最后一个交易日不开夜盘（夜盘归属下一交易日，那天不开市）。
        周末不适用 —— 周五夜盘照开，它归属下周一。
        """
        if not self.has_night_session or not self.is_trading_date(date):
            return False
        d = date + dt.timedelta(days=1)
        for _ in range(30):
            if d.isoformat() in self.holidays:
                return False  # 与下一个交易日之间夹着法定节假日 ⇒ 今晚不开
            if self.trades_on_weekends or d.weekday() < 5:
                return True
            d += dt.timedelta(days=1)
        return True

    def in_session(self, ts: int) -> bool:
        """该时刻是否落在某个交易节内（左闭右闭，与 bar 收盘时刻语义一致）。

        **要看日期，不只是看时刻**：周六上午十点、国庆节上午十点都不在交易时段内。
        原来只比对时分，后果是健康面板每个周末把期货全报成「数据滞后」、
        轮询整个周末空转。
        """
        local = dt.datetime.fromtimestamp(ts, CST)
        minute = local.hour * 60 + local.minute
        date = local.date()
        for s in self.sessions:
            # 跨零点节的凌晨部分：它属于**前一自然日**开的那场夜盘
            if s.crosses_midnight and minute <= s.end_min - 1440:
                return self.opens_night(date - dt.timedelta(days=1))
            if s.start_min <= minute <= min(s.end_min, 1440):
                return self.opens_night(date) if s.is_night else self.is_trading_date(date)
        return False

    def trading_day(self, ts: int) -> str:
        """bar 收盘时刻 -> 所属交易日 YYYY-MM-DD。

        规则：夜盘（>= 20:00）归属**下一个**交易日；跨零点后的凌晨时段（<= 04:00）
        归属当日（因为它本就是前一晚夜盘的延续，而那晚夜盘已归属当日）。
        """
        local = dt.datetime.fromtimestamp(ts, CST)
        minute = local.hour * 60 + local.minute
        date = local.date()
        if minute >= _hhmm("20:00"):
            date = self.next_trading_date(date)
        return date.isoformat()

    def next_trading_date(self, date: dt.date) -> dt.date:
        """下一个开盘的自然日（跳过周末与节假日）。"""
        d = date + dt.timedelta(days=1)
        for _ in range(30):
            if self.is_trading_date(d):
                return d
            d += dt.timedelta(days=1)
        raise ValueError(f"{date} 之后 30 天内找不到交易日，holidays 配置可能有误")


__all__ = ["MarketCalendar", "Session"]
