#!/usr/bin/env python
"""信号质量报告（终端版）。与面板的 /api/stats 走同一套统计代码。

一条形态到底赚不赚钱 —— 这是 PRD 的第二个目标。口径见 ADR-0008，报告会把口径原样打出来。

用法：
    .venv/bin/python scripts/report.py
    .venv/bin/python scripts/report.py --rule-id volume-spike --horizon 40 --cost-bps 5
    .venv/bin/python scripts/report.py --json > report.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sigdesk.core.env import load_env  # noqa: E402
from sigdesk.core.models import Timeframe  # noqa: E402
from sigdesk.rules.model import Direction, Signal  # noqa: E402
from sigdesk.stats.outcome import OutcomeParams, evaluate_all  # noqa: E402
from sigdesk.stats.report import build_report, format_report  # noqa: E402
from sigdesk.store.parquet_io import read_range  # noqa: E402
from sigdesk.store.runtime_store import RuntimeStore  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 脚本自己读 .env：`set -a; . ./.env` 是 bash 专有写法，Windows 上没有对应物。
# 查找顺序 SIGDESK_ENV -> ./.env -> ~/.signal-desk/.env（换新包也不用重配）。
ENV = load_env(ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description="信号质量报告")
    ap.add_argument("--state-db", type=pathlib.Path, default=ROOT / "data" / "runtime.sqlite3")
    ap.add_argument("--data-root", type=pathlib.Path, default=ROOT / "data" / "bars")
    ap.add_argument("--rule-id", default=None)
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--horizon", type=int, default=20, help="固定持有期（扳机周期的根数）")
    ap.add_argument("--stop-pct", type=float, default=0.5, help="止损百分比")
    ap.add_argument("--target-pct", type=float, default=1.0, help="止盈百分比")
    ap.add_argument("--cost-bps", type=float, default=0.0, help="**单边**成本基点；0 = 毛收益")
    ap.add_argument(
        "--atr-key", default=OutcomeParams().atr_key,
        help="用信号快照里的该 ATR 值替代百分比止损；传空串强制走百分比",
    )
    ap.add_argument("--entry-on-signal-close", action="store_true",
                    help="用信号那根的收盘价入场（偏乐观，见 ADR-0008）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    with RuntimeStore(args.state_db) as rs:
        rows = rs.signals(rule_id=args.rule_id, symbol=args.symbol)
    if not rows:
        print("库里没有信号。先跑 scripts/watch.py 或 scripts/replay_check.py 生成一些。")
        return 1

    signals = [
        Signal(
            rule_id=r["rule_id"], symbol=r["symbol"], direction=Direction(r["direction"]),
            timeframe=Timeframe(r["timeframe"]), fired_at=int(r["fired_at"]),
            trigger_price=float(r["trigger_price"]), dedup_key=r["dedup_key"],
            context=dict(r.get("context") or {}), role_bars=dict(r.get("role_bars") or {}),
            trading_day=r.get("trading_day"),
        )
        for r in rows
    ]
    needed = {s.symbol: s.timeframe for s in signals}
    bars = {sym: read_range(args.data_root, sym, tf, 0, 2**31) for sym, tf in needed.items()}
    missing = [s for s, b in bars.items() if not b]
    if missing:
        print(f"⚠️  这些标的在 {args.data_root} 下没有行情，其信号将标为无法评价: {missing}")

    params = OutcomeParams(
        horizon_bars=args.horizon,
        stop_pct=args.stop_pct / 100,
        target_pct=args.target_pct / 100,
        cost_bps=args.cost_bps,
        entry_on_next_open=not args.entry_on_signal_close,
        atr_key=args.atr_key or None,
    )
    report = build_report(
        evaluate_all(signals, bars, params),
        {
            "horizon_bars": params.horizon_bars, "stop_pct": params.stop_pct,
            "target_pct": params.target_pct, "cost_bps": params.cost_bps,
            "entry_on_next_open": params.entry_on_next_open, "atr_key": params.atr_key,
        },
    )
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=1))
    else:
        print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
