#!/usr/bin/env python
"""历史回补：拉 1m -> 归一化 -> 聚合出高周期 -> 落 Parquet。

用 by-timerange（**不含当日**，是权威归档数据）。当日盘中数据由实时轮询另行处理，
次日再用本脚本回填校正。

用法：
    .venv/bin/python scripts/backfill.py CN.SHFE.rb2610 2026-08-20 2026-08-27
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sigdesk.core.env import load_env  # noqa: E402
from sigdesk.core.models import CST, Timeframe  # noqa: E402
from sigdesk.core.registry import load_registry  # noqa: E402
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
DERIVED = [Timeframe.M5, Timeframe.M15, Timeframe.M30, Timeframe.H1, Timeframe.H4,
           Timeframe.D1, Timeframe.W1, Timeframe.MON1]


def _day_ts(text: str, end: bool = False) -> int:
    d = dt.datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=CST)
    return int((d + dt.timedelta(days=1) if end else d).timestamp())


async def main(uid: str, start: str, end: str) -> int:
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
    if not sym.quote_code:
        print(f"拒绝：{uid} 缺少 quote_code 映射")
        return 2

    now = int(dt.datetime.now(dt.UTC).timestamp())
    async with QuoteApiClient(cfg) as client:
        rows = await client.kline_by_timerange(
            sym.quote_code, Timeframe.M1, _day_ts(start), _day_ts(end, end=True)
        )
    if not rows:
        print("无数据。注意 by-timerange 不含当日；请确认区间不是只覆盖今天。")
        return 1

    m1 = normalize_klines(rows, symbol=uid, timeframe=Timeframe.M1, now_ts=now, calendar=cal)
    data_root = ROOT / "data" / "bars"
    total = len(write_bars(data_root, m1))
    print(f"{uid}  1m: {len(m1)} 根 -> {total} 个分区")

    for tf in DERIVED:
        higher = aggregate_complete(uid, m1, tf)
        n = len(write_bars(data_root, higher))
        print(f"{uid}  {tf.value}: {len(higher)} 根 -> {n} 个分区")
    print(f"落盘目录: {data_root}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3])))
