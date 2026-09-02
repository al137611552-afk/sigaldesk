#!/usr/bin/env python
"""历史回补：拉 1m 或 1d -> 归一化 -> 聚合出高周期 -> 落 Parquet。

用 by-timerange（**不含当日**，是权威归档数据）。当日盘中数据由实时轮询另行处理，
次日再用本脚本回填校正。

    # 默认：拉 1m，聚合出 5m ~ 1mon（近期用，一天约 500 根）
    python scripts/backfill.py CN.SHFE.rb2610 2026-08-20 2026-08-27

    # 加密同一条命令（走 OKX 公开接口，**不需要凭据**）
    python scripts/backfill.py CRYPTO.OKX.BTCUSDT.PERP 2024-01-01 2026-09-01 --timeframe 1d

    # 长历史：**直接拉日线**，只聚合出周线月线
    python scripts/backfill.py CN.SHFE.rb2610 2024-01-01 2026-09-01 --timeframe 1d

**为什么要有 --timeframe 1d**：日线/周线/月线原本全靠 1m 聚合，于是"能看多少根
日线"完全取决于回补了多长的 1m。回补三个月 1m 只得到 45 根日线、10 根周线、
3 根月线 —— 想看两年日线就得拉两年 1m（每品种约 12 万根），既慢又浪费。
接口原生支持日线（interval_range=101），直接拉便宜得多。

**两种模式别对同一区间都跑**：接口的日线是交易所口径，1m 聚合出的日线是本项目
的交易日归属（夜盘归下一交易日），两者对夜盘品种可能不同。同一个分区后写的覆盖
先写的，混用会得到一份来源不明的日线。正确用法是**分段不重叠**：
远期用 `--timeframe 1d`，近期用默认的 1m。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sigdesk.core.env import load_env  # noqa: E402
from sigdesk.core.models import (  # noqa: E402
    CST,
    QUOTE_API_INTERVAL,
    Market,
    Symbol,
    Timeframe,
)
from sigdesk.core.registry import load_registry  # noqa: E402
from sigdesk.feed.okx import OkxRestClient, OkxRestConfig  # noqa: E402
from sigdesk.feed.quote_api import (  # noqa: E402
    QuoteApiClient,
    QuoteApiConfig,
    normalize_klines,
)
from sigdesk.store.bar_builder import aggregate_complete  # noqa: E402
from sigdesk.store.parquet_io import write_bars  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 脚本自己读 .env：`set -a; . ./.env` 是 bash 专有写法，Windows 上没有对应物。
# 查找顺序 SIGDESK_ENV -> ./.env -> ~/.signal-desk/.env（换新包也不用重配）。
ENV = load_env(ROOT)
# 含日线：日线按**交易日**聚合（夜盘归属下一交易日），不是自然日。
# 注意 aggregate_complete 会 flush 末桶，所以回补区间必须是完整闭合的
# —— by-timerange 不含当日，天然满足。
CHUNK_DAYS = 7   # 单次 by-timerange 的最大跨度，再大就会读超时
DERIVED = [Timeframe.M5, Timeframe.M15, Timeframe.M30, Timeframe.H1, Timeframe.H4,
           Timeframe.D1, Timeframe.W1, Timeframe.MON1]


async def _crypto(sym: Symbol, uid: str, start: str, end: str,
                  timeframe: Timeframe) -> int:
    """加密走 OKX 公开接口（**不需要凭据**），其余流程与期货一致。

    统一进这个脚本而不是另写一个：两个市场的"回补历史"是同一件事，
    分成两条命令的话，用户每次都得先想"这个标的属于哪边"。
    """
    # OKX 的 instId 是 `code`（BTC-USDT-SWAP），不是 `ccxt_symbol`（BTC/USDT:USDT）——
    # 后者是给 ccxt 用的另一套写法。watch.py 里也是用 code 调的，保持一致。
    if not sym.code:
        print(f"拒绝：{uid} 缺少 code 映射")
        return 2
    start_ts, end_ts = _day_ts(start), _day_ts(end, end=True)
    async with OkxRestClient(OkxRestConfig()) as client:
        bars = await client.fetch_range(sym.code, uid, timeframe, start_ts, end_ts)
    if not bars:
        print("无数据。OKX 的历史深度有限，起始日期太早会取不到。")
        return 1
    data_root = ROOT / "data" / "bars"
    print(f"{uid}  {timeframe.value}: {len(bars)} 根 -> "
          f"{len(write_bars(data_root, bars))} 个分区")
    for tf in [t for t in DERIVED if t.rank > timeframe.rank]:
        higher = aggregate_complete(uid, bars, tf)
        print(f"{uid}  {tf.value}: {len(higher)} 根 -> "
              f"{len(write_bars(data_root, higher))} 个分区")
    print(f"落盘目录: {data_root}")
    return 0


def _day_ts(text: str, end: bool = False) -> int:
    d = dt.datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=CST)
    return int((d + dt.timedelta(days=1) if end else d).timestamp())


async def main(uid: str, start: str, end: str, timeframe: Timeframe = Timeframe.M1) -> int:
    # 日线一根覆盖一天，按周分段没必要，反而把请求数放大 100 倍。
    # 1m 才需要小分段（跨度一大就读超时）。
    chunk = CHUNK_DAYS if timeframe is Timeframe.M1 else 365
    cfg = QuoteApiConfig(
        base_url=os.environ["QUOTE_API_BASE"],
        api_key=os.environ["QUOTE_API_KEY"],
        tls_fingerprint=os.environ.get("QUOTE_API_TLS_FINGERPRINT", ""),
        allow_insecure_tls=os.environ.get("QUOTE_API_ALLOW_INSECURE") == "1",
    )
    reg = load_registry(ROOT / "config")
    sym = reg.symbol(uid)
    cal = reg.calendar_of(uid)
    if sym.is_continuous:
        print(f"拒绝：{uid} 是主连/指数序列，口径不可复现，不得入库（CLAUDE.md 坑#9）")
        return 2
    # 加密走 OKX，**在检查 quote_code 之前分派** —— quote_code 是期货行情 API 的
    # 代码映射，加密标的本来就没有，先检查会把它挡在门外。
    if sym.market is Market.CRYPTO:
        return await _crypto(sym, uid, start, end, timeframe)
    if not sym.quote_code:
        print(f"拒绝：{uid} 缺少 quote_code 映射")
        return 2

    now = int(dt.datetime.now(dt.UTC).timestamp())
    # **按周分段拉**：by-timerange 跨度一大就会读超时（实测两个月必挂、一周稳过），
    # 而超时是在流读取阶段抛的，看起来像"连不上"，非常误导。
    # 分段是幂等的：同一根 bar 重复拿到只是覆盖。
    rows: list[dict[str, object]] = []
    async with QuoteApiClient(cfg) as client:
        cur = dt.date.fromisoformat(start)
        last = dt.date.fromisoformat(end)
        while cur <= last:
            stop = min(cur + dt.timedelta(days=chunk - 1), last)
            got = await client.kline_by_timerange(
                sym.quote_code, timeframe,
                _day_ts(cur.isoformat()), _day_ts(stop.isoformat(), end=True),
            )
            rows.extend(got)
            print(f"    {cur} ~ {stop}: {len(got)} 行")
            cur = stop + dt.timedelta(days=1)
    if not rows:
        print("无数据。注意 by-timerange 不含当日；请确认区间不是只覆盖今天。")
        return 1

    base = normalize_klines(rows, symbol=uid, timeframe=timeframe, now_ts=now, calendar=cal)
    data_root = ROOT / "data" / "bars"
    total = len(write_bars(data_root, base))
    print(f"{uid}  {timeframe.value}: {len(base)} 根 -> {total} 个分区")

    # 只聚合**比基础周期更大**的。拉日线时 5m/15m/… 无从聚合，跳过而不是报错。
    # **用 rank 不用 seconds**：日历周期（日/周/月）的 seconds 是 0，
    # 拿它比较会把 5m~4h 全判成"更大"（第一版就是这么错的）。
    for tf in [t for t in DERIVED if t.rank > timeframe.rank]:
        higher = aggregate_complete(uid, base, tf)
        n = len(write_bars(data_root, higher))
        print(f"{uid}  {tf.value}: {len(higher)} 根 -> {n} 个分区")
    print(f"落盘目录: {data_root}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="历史回补", epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol")
    ap.add_argument("start", help="YYYY-MM-DD")
    ap.add_argument("end", help="YYYY-MM-DD（不含当日）")
    ap.add_argument("--timeframe", default="1m", choices=[t.value for t in QUOTE_API_INTERVAL],
                    help="拉哪个周期。长历史用 1d，近期用 1m（默认）")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.symbol, a.start, a.end, Timeframe(a.timeframe))))
