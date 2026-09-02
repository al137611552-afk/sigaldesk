#!/usr/bin/env python
"""构建期货主连序列：main-by-date 换月区间 + 真实合约数据 -> 拼接 -> 落 Parquet。

为什么不用数据源的 rb8888：口径不可复现（CLAUDE.md 坑#9）。跨换月的回测只能自己拼。

用法：
    .venv/bin/python scripts/build_continuous.py CN.SHFE.rb.CONT 2025-01-01 2026-08-31
    .venv/bin/python scripts/build_continuous.py CN.SHFE.rb.CONT 2025-01-01 2026-08-31 --adjust none
    .venv/bin/python scripts/build_continuous.py CN.SHFE.rb.CONT 2025-01-01 2026-08-31 --dry-run

拼接方式与每次换月的平移量会写进产物元数据 `data/bars/_continuous/<uid>.json` ——
回测出了怪结果要能回答"是不是换月平移造成的"。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
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
from sigdesk.store.continuous import (  # noqa: E402
    AdjustMode,
    MainSegment,
    StitchError,
    contracts_needed,
    parse_main_segments,
    stitch,
)
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
# 新合约在成为主力之前就已经在交易。多拉这些自然日，是为了拿到换月锚点
# ——「两个合约都有成交的最后一根」。拉不够就算不出价差，脚本会明确报错。
CHUNK_DAYS = 7   # 单次 by-timerange 的最大跨度，再大就会读超时
OVERLAP_DAYS = 15


def _day_ts(text: str, end: bool = False) -> int:
    d = dt.datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=CST)
    return int((d + dt.timedelta(days=1) if end else d).timestamp())


def _minus_days(day: str, n: int) -> str:
    return (dt.date.fromisoformat(day) - dt.timedelta(days=n)).isoformat()


def _clip(segments: list[MainSegment], start: str, end: str) -> list[MainSegment]:
    """把换月区间裁到请求窗口内，丢掉完全落在窗口外的段。"""
    out = []
    for s in segments:
        lo, hi = max(s.start_day, start), min(s.end_day, end)
        if lo <= hi:
            out.append(MainSegment(s.contract, lo, hi))
    return out


async def build(uid: str, start: str, end: str, adjust: AdjustMode, dry_run: bool) -> int:
    reg = load_registry(ROOT / "config")
    sym = reg.symbol(uid)
    cal = reg.calendar_of(uid)
    if not sym.is_continuous:
        print(f"拒绝：{uid} 不是主连标的。真实合约请用 scripts/backfill.py")
        return 2
    if not sym.main_code:
        print(f"拒绝：{uid} 缺少 main_code（如 rb9999），无法查换月区间")
        return 2

    cfg = QuoteApiConfig(
        base_url=os.environ["QUOTE_API_BASE"],
        api_key=os.environ["QUOTE_API_KEY"],
        tls_fingerprint=os.environ.get("QUOTE_API_TLS_FINGERPRINT", ""),
        allow_insecure_tls=os.environ.get("QUOTE_API_ALLOW_INSECURE") == "1",
    )
    now = int(dt.datetime.now(dt.UTC).timestamp())

    async with QuoteApiClient(cfg) as client:
        rows = await client.main_by_date([sym.main_code], start, end)
        segments = _clip(parse_main_segments(rows, sym.main_code), start, end)
        if not segments:
            print(f"{start}~{end} 内没有 {sym.main_code} 的主力区间")
            return 1
        print(f"{sym.main_code} 在 {start}~{end} 共 {len(segments)} 段：")
        for s in segments:
            print(f"  {s.contract:10s} {s.start_day} ~ {s.end_day}")
        if dry_run:
            print("\n--dry-run：只看换月排布，不拉数据。")
            return 0

        bars_by_contract = {}
        for contract in contracts_needed(segments):
            seg = next(s for s in segments if s.contract == contract)
            # 往前多拉 OVERLAP_DAYS 是为了换月锚点，多出来的部分 stitch 会自行裁掉
            frm = _minus_days(seg.start_day, OVERLAP_DAYS)
            # 与 backfill.py 同一个坑：by-timerange 跨度一大就读超时（分段是幂等的）
            raw: list[dict[str, object]] = []
            cur = dt.date.fromisoformat(frm)
            stop_day = dt.date.fromisoformat(seg.end_day)
            while cur <= stop_day:
                chunk_end = min(cur + dt.timedelta(days=CHUNK_DAYS - 1), stop_day)
                raw.extend(await client.kline_by_timerange(
                    contract, Timeframe.M1,
                    _day_ts(cur.isoformat()), _day_ts(chunk_end.isoformat(), end=True),
                ))
                cur = chunk_end + dt.timedelta(days=1)
            bars = normalize_klines(
                raw, symbol=contract, timeframe=Timeframe.M1, now_ts=now, calendar=cal
            )
            bars_by_contract[contract] = bars
            print(f"  取 {contract}: {len(bars)} 根 1m（含 {frm} 起的换月重叠段）")

    try:
        result = stitch(segments, bars_by_contract, symbol=uid, adjust=adjust)
    except StitchError as e:
        print(f"\n拼接失败：{e}")
        return 1

    print(f"\n拼接 {len(result.bars)} 根 1m，{len(result.rollovers)} 次换月（{adjust.value}）：")
    for r in result.rollovers:
        print(
            f"  {r.at_day} {r.from_contract} -> {r.to_contract}  "
            f"价差 {r.diff:+.1f}  累计平移 {r.cum_offset:+.1f}"
        )

    data_root = ROOT / "data" / "bars"
    print(f"\n1m: {len(result.bars)} 根 -> {len(write_bars(data_root, result.bars))} 个分区")
    for tf in DERIVED:
        higher = aggregate_complete(uid, result.bars, tf)
        print(f"{tf.value}: {len(higher)} 根 -> {len(write_bars(data_root, higher))} 个分区")

    meta_dir = data_root / "_continuous"
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "symbol": uid,
        "main_code": sym.main_code,
        "range": [start, end],
        "adjust": adjust.value,
        "built_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "segments": [
            {"contract": s.contract, "start_day": s.start_day, "end_day": s.end_day}
            for s in segments
        ],
        "rollovers": [
            {
                "at_day": r.at_day,
                "from": r.from_contract,
                "to": r.to_contract,
                "anchor_ts": r.anchor_ts,
                "diff": r.diff,
                "cum_offset": r.cum_offset,
            }
            for r in result.rollovers
        ],
    }
    meta_path = meta_dir / f"{uid}.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"元数据: {meta_path}")
    print(
        "\n提醒：平移后的历史价格不是当时的真实成交价，绝对价位无意义；"
        "该序列**不可下单**，只用于跨换月回测。"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("uid")
    ap.add_argument("start")
    ap.add_argument("end")
    ap.add_argument(
        "--adjust", choices=[m.value for m in AdjustMode], default=AdjustMode.BACK_DIFF.value
    )
    ap.add_argument("--dry-run", action="store_true", help="只打印换月排布，不拉数据不落盘")
    a = ap.parse_args()
    return asyncio.run(build(a.uid, a.start, a.end, AdjustMode(a.adjust), a.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
