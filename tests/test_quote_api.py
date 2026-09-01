"""Quote API 归一化的纯逻辑单测（不触网）。"""

from __future__ import annotations

import datetime as dt

import pytest

from sigdesk.core.calendar import MarketCalendar
from sigdesk.core.models import CST, Timeframe
from sigdesk.feed.quote_api import QuoteApiError, normalize_klines, unwrap_payload


def ts(text: str) -> int:
    return int(dt.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=CST).timestamp())


RB = MarketCalendar.from_config(
    "cn_futures_rb", ["09:00-10:15", "10:30-11:30", "13:30-15:00", "21:00-23:00"], []
)


def row(t: int, **kw: float) -> dict[str, float]:
    base = {"time_stamp": t, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0}
    base.update(kw)
    return base


def test_minute_timestamp_is_close_not_open() -> None:
    """实测语义：分钟线 time_stamp = 收盘时刻。open_ts 必须回推一个周期。"""
    t = ts("2026-08-27 09:05")
    (bar,) = normalize_klines([row(t)], symbol="X", timeframe=Timeframe.M5, now_ts=t)
    assert bar.close_ts == t
    assert bar.open_ts == t - 300
    assert dt.datetime.fromtimestamp(bar.open_ts, CST).strftime("%H:%M") == "09:00"


def test_last_bar_marked_open_by_clock_not_position() -> None:
    """INV-2：closed 由 close_ts 与当前时间比较得出，不靠"是不是最后一根"。"""
    a, b = ts("2026-08-27 09:05"), ts("2026-08-27 09:10")
    now = b - 30  # 第二根尚未收盘
    bars = normalize_klines([row(a), row(b)], symbol="X", timeframe=Timeframe.M5, now_ts=now)
    assert [x.closed for x in bars] == [True, False]


def test_daily_timestamp_is_trading_day_not_close_time() -> None:
    """实测语义：日线 time_stamp = 交易日的 UTC 零点，与分钟线不同路径。"""
    day_ts = int(dt.datetime(2026, 8, 28, tzinfo=dt.UTC).timestamp())
    (bar,) = normalize_klines(
        [row(day_ts)], symbol="X", timeframe=Timeframe.D1, now_ts=day_ts + 86400
    )
    assert bar.trading_day == "2026-08-28"
    assert bar.close_ts - bar.open_ts == 86400


def test_night_bar_belongs_to_next_trading_day() -> None:
    """08-27（周四）21:05 的夜盘 bar 属于 08-28 交易日。"""
    t = ts("2026-08-27 21:05")
    (bar,) = normalize_klines([row(t)], symbol="X", timeframe=Timeframe.M5, now_ts=t, calendar=RB)
    assert bar.trading_day == "2026-08-28"


def test_friday_night_bar_skips_weekend() -> None:
    """08-28 是周五，其夜盘属于下周一 08-31。"""
    t = ts("2026-08-28 21:05")
    (bar,) = normalize_klines([row(t)], symbol="X", timeframe=Timeframe.M5, now_ts=t, calendar=RB)
    assert bar.trading_day == "2026-08-31"


def test_day_session_bar_keeps_its_own_date() -> None:
    t = ts("2026-08-27 14:05")
    (bar,) = normalize_klines([row(t)], symbol="X", timeframe=Timeframe.M5, now_ts=t, calendar=RB)
    assert bar.trading_day == "2026-08-27"


def test_unwrap_single_and_batch_shapes() -> None:
    assert unwrap_payload({"code": 0, "data": [{"time_stamp": 1}]}) == [{"time_stamp": 1}]
    batch = {"code": 0, "data": [{"code": "rb2610", "klines": [{"time_stamp": 2}]}]}
    assert unwrap_payload(batch, requested_code="rb2610") == [{"time_stamp": 2}]


def test_unwrap_null_data_is_empty_not_error() -> None:
    """by-timerange 查当日区间会返回 data:null，这是合法的"无数据"。"""
    assert unwrap_payload({"code": 0, "data": None}) == []


def test_unwrap_raises_on_error_code() -> None:
    with pytest.raises(QuoteApiError, match="code=500"):
        unwrap_payload({"code": 500, "msg": "boom"})


async def test_client_refuses_to_start_without_pinned_fingerprint() -> None:
    """ADR-0002：默认拒绝无指纹连接 —— 请求头带着 AK，不能裸奔在自签名证书上。

    （已实测：错误指纹会在握手期被拒，正确指纹正常取数。）
    """
    from sigdesk.feed.quote_api import QuoteApiClient, QuoteApiConfig

    cfg = QuoteApiConfig(base_url="https://example.invalid", api_key="k")
    with pytest.raises(QuoteApiError, match="未配置 QUOTE_API_TLS_FINGERPRINT"):
        async with QuoteApiClient(cfg):
            pass
