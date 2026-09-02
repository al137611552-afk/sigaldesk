from __future__ import annotations

import datetime as dt

import pytest

from sigdesk.core.calendar import MarketCalendar, Session
from sigdesk.core.models import CST

RB = MarketCalendar.from_config(
    "cn_futures_rb", ["09:00-10:15", "10:30-11:30", "13:30-15:00", "21:00-23:00"], []
)
AU = MarketCalendar.from_config("cn_futures_au", ["09:00-11:30", "13:30-15:00", "21:00-02:30"], [])


def ts(text: str) -> int:
    return int(dt.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=CST).timestamp())


def test_session_parses_overnight() -> None:
    s = Session.parse("21:00-02:30")
    assert s.crosses_midnight and s.end_min == 26 * 60 + 30


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        ("2026-08-27 09:30", True),
        ("2026-08-27 10:20", False),  # 10:15-10:30 休盘
        ("2026-08-27 12:00", False),  # 午休
        ("2026-08-27 22:00", True),
        ("2026-08-27 23:30", False),  # rb 夜盘 23:00 收
    ],
)
def test_in_session(moment: str, expected: bool) -> None:
    assert RB.in_session(ts(moment)) is expected


def test_overnight_session_membership() -> None:
    """au 夜盘到次日 02:30，凌晨时段应判为在盘中。"""
    assert AU.in_session(ts("2026-08-28 01:00")) is True
    assert AU.in_session(ts("2026-08-28 03:00")) is False


def test_holiday_shifts_night_session_attribution() -> None:
    """节假日表缺失会算错夜盘归属 —— 这里钉住配了假期时的正确行为。"""
    cal = MarketCalendar.from_config("t", ["21:00-23:00"], ["2026-10-01", "2026-10-02"])
    assert cal.trading_day(ts("2026-09-30 21:30")) == "2026-10-05"  # 跳过假期与周末


# ---- 回归：in_session 必须看日期，不能只看时刻 ----------------------------


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        ("2026-09-05 10:00", False),  # 周六上午
        ("2026-09-06 14:00", False),  # 周日下午
        ("2026-09-04 21:30", True),  # 周五夜盘照开（归属下周一）
        ("2026-09-05 21:30", False),  # 周六没有夜盘
    ],
)
def test_in_session_rejects_weekend(moment: str, expected: bool) -> None:
    """曾经只比对时分不看日期：周末整个被判成盘中，健康面板每逢周末全报「滞后」。"""
    assert RB.in_session(ts(moment)) is expected


def test_weekend_overnight_tail_follows_its_own_night() -> None:
    """跨零点节的凌晨部分归属**前一自然日**开的那场夜盘。"""
    assert AU.in_session(ts("2026-09-05 01:00")) is True  # 周六凌晨 = 周五夜盘的尾巴
    assert AU.in_session(ts("2026-09-07 01:00")) is False  # 周一凌晨：周日没开夜盘


HOLIDAY_CAL = MarketCalendar.from_config(
    "t", ["09:00-15:00", "21:00-23:00"], ["2026-10-01", "2026-10-02"]
)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        ("2026-09-30 10:00", True),  # 节前最后一个交易日，日盘照常
        ("2026-09-30 21:30", False),  # 但夜盘不开：它归属 10-01，那天休市
        ("2026-10-01 10:00", False),
        ("2026-10-02 10:00", False),
    ],
)
def test_holiday_closes_market_and_preceding_night(moment: str, expected: bool) -> None:
    assert HOLIDAY_CAL.in_session(ts(moment)) is expected


def test_trades_on_weekends_only_waives_weekends() -> None:
    """`trades_on_weekends` 只豁免周末；显式写进表里的节假日照样休市。

    加密日历的 holidays 本就是空的，所以这条只是钉住语义边界：
    显式条目 > 市场类型，免得日后给某个 7×24 市场配维护窗口时被静默忽略。
    """
    cal = MarketCalendar.from_config(
        "c", ["00:00-23:59"], ["2026-10-01"], trades_on_weekends=True
    )
    assert cal.in_session(ts("2026-09-05 10:00")) is True  # 周六照开
    assert cal.in_session(ts("2026-10-01 10:00")) is False  # 但显式假期休市


def test_crypto_calendar_has_no_holidays() -> None:
    """真配置里加密日历不该被期货那张节假日表污染。"""
    import pathlib

    from sigdesk.core.registry import load_calendars

    btc = load_calendars(pathlib.Path("config/calendars/cn_futures.yaml"))["crypto_24x7"]
    assert btc.holidays == frozenset()
    assert btc.in_session(ts("2026-10-01 10:00")) is True


def test_holidays_accept_yaml_date_objects() -> None:
    """**静默失效防线**：PyYAML 把不带引号的 `2026-10-01` 解析成 date 对象，
    而内部按字符串比对 —— 不归一的话整张节假日表写了等于没写，且不报任何错。
    """
    cal = MarketCalendar.from_config(
        "t", ["09:00-15:00"], [dt.date(2026, 10, 1), "2026-10-02"]
    )
    assert cal.holidays == frozenset({"2026-10-01", "2026-10-02"})
    assert cal.in_session(ts("2026-10-01 10:00")) is False


def test_shipped_calendar_config_actually_takes_effect() -> None:
    """钉住真配置：光有 holidays 条目不算数，得真的让国庆休市。"""
    import pathlib

    from sigdesk.core.registry import load_calendars

    cals = load_calendars(pathlib.Path("config/calendars/cn_futures.yaml"))
    rb = cals["cn_night_23"]
    assert len(rb.holidays) >= 15, "2026 节假日表疑似未配全"
    assert all(isinstance(h, str) for h in rb.holidays)
    assert rb.in_session(ts("2026-10-01 10:00")) is False
    assert rb.in_session(ts("2026-09-30 21:30")) is False
    assert rb.in_session(ts("2026-09-30 10:00")) is True


# ---- 回归：跨零点夜盘的凌晨段，跨周末时必须与前半场同属一个交易日 ----


AU = MarketCalendar.from_config(
    "au", ["09:00-11:30", "13:30-15:00", "21:00-02:30"], ["2026-10-01", "2026-10-02"]
)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        # 平日：周四夜盘 + 周五凌晨，同属周五
        ("2026-08-27 22:00", "2026-08-28"),
        ("2026-08-28 01:00", "2026-08-28"),
        # **跨周末**：周五夜盘 + 周六凌晨，同属下周一
        ("2026-08-28 23:59", "2026-08-31"),
        ("2026-08-29 00:00", "2026-08-31"),
        ("2026-08-29 02:29", "2026-08-31"),
        # 日盘照旧
        ("2026-08-31 10:00", "2026-08-31"),
    ],
)
def test_overnight_tail_belongs_to_the_same_trading_day(moment: str, expected: str) -> None:
    """**一场夜盘从晚上开到次日凌晨，整场同属一个交易日。**

    曾经凌晨那段直接返回当天日期：平日恰好等价（周四夜盘的周五凌晨 -> 周五），
    但周五夜盘的周六凌晨会返回"周六"，而同一场的 23:59 返回"下周一" ——
    同一场夜盘被劈成两个交易日。后果是日线聚合当场抛"时间倒流"、
    Parquet 也会分到两个分区。真跑 au2610 时直接把 watch.py 崩在启动阶段。
    """
    assert AU.trading_day(ts(moment)) == expected


def test_overnight_tail_skips_holidays_too() -> None:
    """节假日同理：9/30 夜盘不开（见 opens_night），但若某市场开，
    其凌晨段也该跟着跳过整个假期。"""
    # 10/01、10/02 是假期，10/03、10/04 是周末 -> 下一个交易日是 10/05
    assert AU.trading_day(ts("2026-09-30 23:59")) == "2026-10-05"
    assert AU.trading_day(ts("2026-10-01 01:00")) == "2026-10-05"


def test_trading_day_agrees_with_in_session_on_the_overnight_tail() -> None:
    """两个判定必须同源：in_session 一直是按"前一日是否开夜盘"判的，
    trading_day 以前不是 —— 不一致就会出现"在盘中但归错交易日"。"""
    assert AU.in_session(ts("2026-08-29 01:00")) is True   # 周五夜盘的尾巴
    assert AU.trading_day(ts("2026-08-29 01:00")) == "2026-08-31"
    assert AU.in_session(ts("2026-08-31 01:00")) is False  # 周日没开夜盘
