from __future__ import annotations

import datetime as dt
import pathlib
from dataclasses import replace
from typing import Any

import pytest

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


def test_partition_span_reports_both_ends(tmp_path: pathlib.Path) -> None:
    """**首尾都要给。**

    只看末日的话，"补过近两个月"会被误判成"已覆盖两年"，拉长历史的请求
    就被静默跳过 —— 数据看着有、其实短一大截。实测撞上过：批量回补时
    au/m/cu 三个只有二十几到四十几根日线，却因为末日够新而被跳过。
    """
    from sigdesk.store.parquet_io import partition_span

    root = tmp_path / "bars"
    assert partition_span(root, "CN.SHFE.rb2610", Timeframe.D1) is None, "没数据要给 None"
    # **每根 bar 要有各自的 close_ts**。同一分区内按 close_ts 去重，三根共用一个
    # 时间戳的话按年分区会把它们并成一根 —— 那是夹具不真实（真实数据里
    # 同一 (标的, 周期) 的 close_ts 唯一），不是分区方案有问题。
    for i, (day, close) in enumerate((("2026-01-05", 10.0), ("2026-03-09", 11.0),
                                      ("2026-02-02", 12.0))):
        ts = 1_767_000_000 + i * 86_400
        write_bars(root, [Bar("CN.SHFE.rb2610", Timeframe.D1, ts - 86_400, ts, close, close,
                              close, close, 1.0, trading_day=day)])
    assert partition_span(root, "CN.SHFE.rb2610", Timeframe.D1) == ("2026-01-05", "2026-03-09")


def test_partition_span_never_reads_bar_data(tmp_path: pathlib.Path) -> None:
    """按日分区时**一个文件都不打开**；按年分区时只读统计信息，绝不读行数据。

    面板启动要对每个标的问一遍，读行数据会让 /api/meta 慢到肉眼可见。
    这里用行为证明而不是断言源码文本：**把文件内容写成垃圾**，
    按日分区仍应答得出来（因为它只看文件名）。
    """
    from sigdesk.store.parquet_io import _partition_days, partition_span

    root = tmp_path / "bars"
    for i, day in enumerate(("2026-01-05", "2026-01-06")):
        ts = 1_767_000_000 + i * 86_400
        write_bars(root, [Bar("CN.SHFE.rb2610", Timeframe.M5, ts - 300, ts, 1.0, 1.0, 1.0,
                              1.0, 1.0, trading_day=day)])
    d = root / "CN" / "CN.SHFE.rb2610" / "5m"
    for f in d.glob("*.parquet"):
        f.write_bytes(b"not a parquet file")
    assert partition_span(root, "CN.SHFE.rb2610", Timeframe.M5) == ("2026-01-05", "2026-01-06")

    src = pathlib.Path("src/sigdesk/store/parquet_io.py").read_text(encoding="utf-8")
    fn = src[src.index("def _partition_days("):]
    fn = fn[: fn.index("\n\n\ndef ")]
    assert "read_bars" not in fn, "取日期范围绝不能读行数据"
    assert _partition_days(root, "CN.SHFE.rb2610", Timeframe.M5)


def test_partition_span_on_yearly_partitions_still_gives_real_dates(
    tmp_path: pathlib.Path,
) -> None:
    """日历周期按年分区后文件名只有 ``2026``，但对外仍要给**真实日期** ——
    面板的"数据止于 X"和回补的覆盖判断都靠它。靠 Parquet 统计信息拿。"""
    from sigdesk.store.parquet_io import latest_partition, partition_span

    root = tmp_path / "bars"
    for i, day in enumerate(("2026-01-05", "2026-02-02", "2026-03-09")):
        ts = 1_767_000_000 + i * 86_400
        write_bars(root, [Bar("CN.SHFE.rb2610", Timeframe.D1, ts - 86_400, ts, 1.0, 1.0, 1.0,
                              1.0, 1.0, trading_day=day)])
    assert [f.name for f in (root / "CN" / "CN.SHFE.rb2610" / "1d").iterdir()] == ["2026.parquet"]
    assert partition_span(root, "CN.SHFE.rb2610", Timeframe.D1) == ("2026-01-05", "2026-03-09")
    assert latest_partition(root, "CN.SHFE.rb2610", Timeframe.D1) == "2026-03-09"


def test_crypto_has_no_trading_day_but_still_gets_dates(tmp_path: pathlib.Path) -> None:
    """加密没有交易日概念（trading_day 是 None），要退回用 close_ts 的 UTC 日期。"""
    from sigdesk.store.parquet_io import latest_partition

    root = tmp_path / "bars"
    ts = 1_767_225_600  # 2026-01-01 00:00 UTC
    write_bars(root, [Bar("CRYPTO.OKX.BTCUSDT.PERP", Timeframe.D1, ts - 86_400, ts,
                          1.0, 1.0, 1.0, 1.0, 1.0)])
    assert latest_partition(root, "CRYPTO.OKX.BTCUSDT.PERP", Timeframe.D1) == "2026-01-01"


def _seed(root: pathlib.Path, uid: str, tf: Timeframe, n: int, step: int) -> list[Bar]:
    """铺 n 根跨多个分区的 bar。"""
    base = 1_767_225_600
    bars = [
        Bar(uid, tf, base + i * step - step, base + i * step, 1.0, 2.0, 0.5, 1.5, 10.0,
            trading_day=dt.datetime.fromtimestamp(base + i * step, dt.UTC).date().isoformat())
        for i in range(n)
    ]
    write_bars(root, bars)
    return bars


@pytest.mark.parametrize("tf,step", [(Timeframe.M5, 300), (Timeframe.D1, 86_400)])
def test_pruning_never_changes_the_result(
    tmp_path: pathlib.Path, tf: Timeframe, step: int
) -> None:
    """**分区裁剪只能少读文件，不能少给 bar。**

    裁猛了就是"数据少了一段"：图上看着正常、规则静默不触发，
    是这个项目最危险的失效类型。所以拿"不裁剪"的结果当基准逐根对拍。
    """
    from sigdesk.store.parquet_io import read_bars, read_range

    root = tmp_path / "bars"
    uid = "CN.SHFE.rb2610"
    bars = _seed(root, uid, tf, 400, step)
    base_dir = root / "CN" / uid / tf.value

    def unpruned(start: int, end: int) -> list[Bar]:
        by_ts = {}
        for f in sorted(base_dir.glob("*.parquet")):
            for b in read_bars(f, uid, tf):
                if start < b.close_ts <= end:
                    by_ts[b.close_ts] = b
        return [by_ts[t] for t in sorted(by_ts)]

    lo, hi = bars[0].close_ts, bars[-1].close_ts
    spans = [
        (0, 2**31),                       # 全量（默认参数）
        (lo - 1, hi),                     # 恰好全覆盖
        (lo + 50 * step, lo + 60 * step),  # 中间一小段
        (hi - step, hi),                  # 只要最后一根
        (lo - 10 * step, lo + step),      # 左边界外
        (hi, hi + 10 * step),             # 右边界外（应为空）
    ]
    for start, end in spans:
        got = read_range(root, uid, tf, start, end)
        assert got == unpruned(start, end), f"{tf.value} 区间 ({start}, {end}] 裁剪后结果变了"


def test_read_range_dedupes_across_old_and_new_layouts(tmp_path: pathlib.Path) -> None:
    """迁移期同一根 bar 会同时躺在按日和按年两种分区里，**不能读成两根**。

    compact 还没跑完就重启面板，或者 compact 跑一半中断，都会出现这个状态。
    不去重的话看不出任何异常 —— 只是每根 bar 算了两遍。
    """
    from sigdesk.store.parquet_io import read_range

    root = tmp_path / "bars"
    uid = "CN.SHFE.rb2610"
    bars = _seed(root, uid, Timeframe.D1, 5, 86_400)      # 写成 2026.parquet
    d = root / "CN" / uid / "1d"
    assert [f.name for f in d.iterdir()] == ["2026.parquet"]

    # 手工再摆一份旧布局（按日）的同样数据
    import pyarrow.parquet as pq
    tbl = pq.read_table(d / "2026.parquet")
    for i, b in enumerate(bars):
        pq.write_table(tbl.slice(i, 1), d / f"{b.trading_day}.parquet", compression="zstd")
    assert len(list(d.iterdir())) == 6

    got = read_range(root, uid, Timeframe.D1, 0, 2**31)
    assert len(got) == len(bars), f"重复读出 {len(got)} 根，应为 {len(bars)}"
    assert [b.close_ts for b in got] == sorted(b.close_ts for b in bars)


def test_read_tail_equals_the_tail_of_read_range(tmp_path: pathlib.Path) -> None:
    """**尾读只是少读文件，不能少给 bar。**

    面板画一屏只要几百根，原来把几万根全读进来再截尾。改成从最新分区往回读，
    结果必须与"全读再截尾"逐根相同 —— 否则就是图上少一段而毫无提示。
    """
    from sigdesk.store.parquet_io import read_range, read_tail

    root = tmp_path / "bars"
    uid = "CN.SHFE.rb2610"
    for tf, step in ((Timeframe.M5, 300), (Timeframe.D1, 86_400)):
        bars = _seed(root, uid, tf, 400, step)
        full = read_range(root, uid, tf, 0, 2**31)
        assert len(full) == len(bars)
        for n in (1, 7, 220, 399, 400, 500, 10_000):
            assert read_tail(root, uid, tf, n) == full[-n:], f"{tf.value} n={n}"
    assert read_tail(root, uid, Timeframe.M5, 0) == []
    assert read_tail(root, "CN.SHFE.nope", Timeframe.M5, 10) == []


def test_count_bars_matches_without_reading_rows(tmp_path: pathlib.Path) -> None:
    """行数从元数据拿。为了标题上的一个数字读几万根不划算。"""
    from sigdesk.store.parquet_io import count_bars

    root = tmp_path / "bars"
    uid = "CN.SHFE.rb2610"
    bars = _seed(root, uid, Timeframe.M5, 250, 300)
    assert count_bars(root, uid, Timeframe.M5) == len(bars)
    assert count_bars(root, "CN.SHFE.nope", Timeframe.M5) == 0

    src = pathlib.Path("src/sigdesk/store/parquet_io.py").read_text(encoding="utf-8")
    fn = src[src.index("def count_bars("):]
    fn = fn[: fn.index("\n\n\ndef ")]
    assert "read_bars" not in fn, "算行数不该读行数据"


def test_read_close_ts_matches_read_range_but_reads_one_column(
    tmp_path: pathlib.Path,
) -> None:
    """列裁剪只是少读列，**给出的 close_ts 必须与全量读逐个相同**。

    Parquet 是列存，只读要用的那一列是它的看家本领（projection pushdown）。
    /api/markers 只需要"有哪些 bar"，却一直读全部 10 列再造几万个 Bar 对象。
    """
    from sigdesk.store.parquet_io import read_close_ts, read_range

    root = tmp_path / "bars"
    uid = "CN.SHFE.rb2610"
    for tf, step in ((Timeframe.M5, 300), (Timeframe.D1, 86_400)):
        _seed(root, uid, tf, 300, step)
        full = [b.close_ts for b in read_range(root, uid, tf, 0, 2**31)]
        assert read_close_ts(root, uid, tf) == full, f"{tf.value} 列裁剪后结果变了"
    assert read_close_ts(root, "CN.SHFE.nope", Timeframe.M5) == []

    src = pathlib.Path("src/sigdesk/store/parquet_io.py").read_text(encoding="utf-8")
    fn = src[src.index("def read_close_ts("):]
    fn = fn[: fn.index("\n\n\ndef ")]
    assert 'columns=["close_ts"]' in fn, "没有真的做列裁剪"
    assert "read_bars" not in fn, "又去造 Bar 对象了，等于没裁"


def test_read_close_ts_dedupes_across_layouts(tmp_path: pathlib.Path) -> None:
    """迁移期新旧布局并存时同一根 bar 会出现两次，去重要和 read_range 一致。"""
    import pyarrow.parquet as pq

    from sigdesk.store.parquet_io import read_close_ts

    root = tmp_path / "bars"
    uid = "CN.SHFE.rb2610"
    bars = _seed(root, uid, Timeframe.D1, 5, 86_400)
    d = root / "CN" / uid / "1d"
    tbl = pq.read_table(d / "2026.parquet")
    for i, b in enumerate(bars):
        pq.write_table(tbl.slice(i, 1), d / f"{b.trading_day}.parquet", compression="zstd")
    assert read_close_ts(root, uid, Timeframe.D1) == sorted(b.close_ts for b in bars)
