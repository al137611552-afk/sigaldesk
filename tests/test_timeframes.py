"""分桶规则单测。基准是实测出来的接口约定：纯墙钟对齐 + 跳过空桶。"""

from __future__ import annotations

import datetime as dt

import pytest

from sigdesk.core.models import CST, Timeframe
from sigdesk.core.timeframes import bucket_close_ts, bucket_open_ts, is_closed


def ts(text: str) -> int:
    """'2026-08-27 09:01' (北京时间) -> UTC epoch"""
    return int(dt.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=CST).timestamp())


def hhmm(t: int) -> str:
    return dt.datetime.fromtimestamp(t, CST).strftime("%H:%M")


@pytest.mark.parametrize(
    ("bar_close", "tf", "expected"),
    [
        # 日盘开盘：09:00-09:01 这根 1m 落进收于 09:05 的 5m 桶
        ("2026-08-27 09:01", Timeframe.M5, "09:05"),
        ("2026-08-27 09:05", Timeframe.M5, "09:05"),  # 边界右闭
        ("2026-08-27 09:06", Timeframe.M5, "09:10"),
        # 10:15 休盘后 10:30 复盘 —— 实测 5m 首根标 10:35，15m 首根标 10:45
        ("2026-08-27 10:31", Timeframe.M5, "10:35"),
        ("2026-08-27 10:31", Timeframe.M15, "10:45"),
        ("2026-08-27 10:15", Timeframe.M15, "10:15"),
        # 60m：实测日盘序列 10:00 / 11:00 / 12:00 / 14:00 / 15:00
        ("2026-08-27 09:01", Timeframe.H1, "10:00"),
        ("2026-08-27 10:31", Timeframe.H1, "11:00"),
        ("2026-08-27 11:30", Timeframe.H1, "12:00"),  # 11:00-11:30 半根，仍标 12:00
        ("2026-08-27 13:31", Timeframe.H1, "14:00"),  # 13:00 桶因无交易被跳过
        ("2026-08-27 15:00", Timeframe.H1, "15:00"),
        # 夜盘
        ("2026-08-26 21:01", Timeframe.H1, "22:00"),
        ("2026-08-26 23:00", Timeframe.H1, "23:00"),
    ],
)
def test_bucket_close_matches_measured_convention(
    bar_close: str, tf: Timeframe, expected: str
) -> None:
    assert hhmm(bucket_close_ts(ts(bar_close), tf)) == expected


def test_bucket_open_is_nominal_period_start() -> None:
    close = ts("2026-08-27 12:00")
    assert hhmm(bucket_open_ts(close, Timeframe.H1)) == "11:00"


def test_daily_rejects_wallclock_bucketing() -> None:
    with pytest.raises(ValueError, match="不是固定长度周期"):
        bucket_close_ts(ts("2026-08-27 15:00"), Timeframe.D1)


def test_is_closed_boundary() -> None:
    """INV-2：收盘时刻已到即为已收盘；末根进行中 bar 必须判为未收盘。"""
    t = ts("2026-08-27 09:05")
    assert is_closed(t, t) is True
    assert is_closed(t, t - 1) is False
