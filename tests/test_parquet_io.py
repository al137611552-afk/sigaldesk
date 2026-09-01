from __future__ import annotations

import datetime as dt
import pathlib
from dataclasses import replace
from typing import Any

from sigdesk.core.models import Bar, Timeframe
from sigdesk.feed.okx import normalize_candles
from sigdesk.feed.quote_api import normalize_klines
from sigdesk.store.parquet_io import partition_key, partition_path, read_range, write_bars

SYMBOL = "CN.SHFE.rb2610"
FUTURE = 4_102_444_800


def _bars(raw: list[dict[str, Any]]) -> list[Bar]:
    return normalize_klines(raw, symbol=SYMBOL, timeframe=Timeframe.M1, now_ts=FUTURE)


def test_roundtrip_preserves_every_field(
    tmp_path: pathlib.Path, rb2610_archived: dict[str, Any]
) -> None:
    bars = [replace(b, trading_day="2026-08-27") for b in _bars(rb2610_archived["1m"])[:100]]
    write_bars(tmp_path, bars)
    back = read_range(tmp_path, SYMBOL, Timeframe.M1, 0, FUTURE)
    assert back == bars


def test_in_progress_bars_are_not_persisted(tmp_path: pathlib.Path) -> None:
    """INV-2：未收盘的 bar 不得落盘，否则归档会被临时值污染。"""
    closed = Bar(SYMBOL, Timeframe.M1, 0, 60, 1, 2, 0.5, 1.5, 10, trading_day="2026-08-27")
    open_ = Bar(
        SYMBOL, Timeframe.M1, 60, 120, 1, 2, 0.5, 1.5, 10, closed=False, trading_day="2026-08-27"
    )
    write_bars(tmp_path, [closed, open_])
    assert [b.close_ts for b in read_range(tmp_path, SYMBOL, Timeframe.M1, 0, FUTURE)] == [60]


def test_rewrite_is_idempotent_and_newer_wins(tmp_path: pathlib.Path) -> None:
    """次日用 by-timerange 回填校正时，同一根 bar 必须被新值覆盖而不是重复。"""
    old = Bar(SYMBOL, Timeframe.M1, 0, 60, 1, 2, 0.5, 1.5, 10, trading_day="2026-08-27")
    write_bars(tmp_path, [old])
    new = Bar(SYMBOL, Timeframe.M1, 0, 60, 1, 2, 0.5, 1.5, 11, trading_day="2026-08-27")
    write_bars(tmp_path, [new])
    back = read_range(tmp_path, SYMBOL, Timeframe.M1, 0, FUTURE)
    assert len(back) == 1 and back[0].volume == 11


def test_partition_uses_trading_day_so_night_session_stays_together() -> None:
    """夜盘（08-27 晚）与次日日盘同属 08-28 交易日，必须落在同一分区。"""
    night = Bar(SYMBOL, Timeframe.M1, 0, 60, 1, 1, 1, 1, 1, trading_day="2026-08-28")
    assert partition_key(night) == "2026-08-28"


# ---------------------------------------------------------------- 加密（无 trading_day）

CRYPTO_SYMBOL = "CRYPTO.OKX.BTCUSDT.PERP"


def test_crypto_bars_roundtrip_with_null_trading_day(
    tmp_path: pathlib.Path, btc_swap_okx: dict[str, Any]
) -> None:
    """加密 7×24 没有交易日概念，trading_day 为 None —— 落盘读回后必须仍是 None，
    不能变成空串或 "None"，否则分区键会漂移。"""
    bars = normalize_candles(btc_swap_okx["1m"], symbol=CRYPTO_SYMBOL, timeframe=Timeframe.M1)[:50]
    write_bars(tmp_path, bars)

    got = read_range(tmp_path, CRYPTO_SYMBOL, Timeframe.M1, 0, FUTURE)
    assert got == bars
    assert all(b.trading_day is None for b in got)


def test_crypto_partitions_by_utc_calendar_day(btc_swap_okx: dict[str, Any]) -> None:
    """期货按交易日分区（夜盘并入次日），加密退回 UTC 自然日 —— 两条路径都要有据可查。"""
    bar = normalize_candles(btc_swap_okx["1m"], symbol=CRYPTO_SYMBOL, timeframe=Timeframe.M1)[0]
    day = dt.datetime.fromtimestamp(bar.close_ts, dt.UTC).date().isoformat()
    assert partition_key(bar) == day
    path = partition_path(pathlib.Path("/data"), bar.symbol, bar.timeframe, day)
    assert path == pathlib.Path(f"/data/CRYPTO/{CRYPTO_SYMBOL}/1m/{day}.parquet")


def test_both_markets_coexist_under_one_root(
    tmp_path: pathlib.Path, rb2610_archived: dict[str, Any], btc_swap_okx: dict[str, Any]
) -> None:
    """两个市场落在同一个数据根下，按 market 一级分区隔开，互不干扰。"""
    cn = [replace(b, trading_day="2026-08-27") for b in _bars(rb2610_archived["1m"])[:20]]
    crypto = normalize_candles(
        btc_swap_okx["1m"], symbol=CRYPTO_SYMBOL, timeframe=Timeframe.M1
    )[:20]
    write_bars(tmp_path, cn + crypto)

    assert sorted(d.name for d in tmp_path.iterdir()) == ["CN", "CRYPTO"]
    assert len(read_range(tmp_path, SYMBOL, Timeframe.M1, 0, FUTURE)) == 20
    assert len(read_range(tmp_path, CRYPTO_SYMBOL, Timeframe.M1, 0, FUTURE)) == 20
