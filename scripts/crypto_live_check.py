#!/usr/bin/env python
"""加密实时链路验收：WS 连续跑 N 分钟，再用 REST 对拍。

这是 M0-B 的验收手段，也是日后换交易所/改归一化时的回归工具。
检查四件事：
  1. WS 产出的每根 bar 与 REST 权威值逐字段一致（价格零容差）
  2. 无重复产出（同一 close_ts 只出现一次）
  3. 无缺口（加密 7×24，1m 序列必须连续）
  4. `--drop-after N`：第 N 秒强制断线并压住 75s（跨过一根 bar 收盘），
     验证真实重连 + REST 回补 —— 不压住的话重连太快，根本没缺口可补

用法：
    .venv/bin/python scripts/crypto_live_check.py 6            # 跑 6 分钟
    .venv/bin/python scripts/crypto_live_check.py 6 --drop-after 60   # 第 60s 强制断线 75s
    .venv/bin/python scripts/crypto_live_check.py 6 CRYPTO.OKX.BTCUSDT.PERP
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import functools
import pathlib
import sys
import time
from collections.abc import AsyncIterator
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sigdesk.core.env import load_env  # noqa: E402
from sigdesk.core.models import Bar, Timeframe  # noqa: E402
from sigdesk.core.registry import load_registry  # noqa: E402
from sigdesk.feed.okx import OkxRestClient, normalize_candles  # noqa: E402
from sigdesk.feed.okx_ws import OkxWsFeed, WsConnection, default_connector  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 脚本自己读 .env：`set -a; . ./.env` 是 bash 专有写法，Windows 上没有对应物。
# 查找顺序 SIGDESK_ENV -> ./.env -> ~/.signal-desk/.env（换新包也不用重配）。
ENV = load_env(ROOT)
FIELDS = ("open", "high", "low", "close", "volume", "money")
# 断线演练时压住重连多久：必须跨过一根 1m 收盘，否则重连太快就没缺口可补
OUTAGE_S = 75.0


def flaky_connector(drop_after_s: float) -> Any:
    """包一层真实连接器：到点强制断开，并在停摆期内拒绝重连 —— 制造真实缺口。"""
    real = default_connector()
    state = {"drop_at": time.monotonic() + drop_after_s, "resume_at": 0.0, "done": False}

    @contextlib.asynccontextmanager
    async def connect() -> AsyncIterator[WsConnection]:
        # 演练已触发但停摆期未过 -> 拒绝重连，让缺口真正张开
        if state["done"] and time.monotonic() < state["resume_at"]:
            raise ConnectionError("演练：停摆中，拒绝重连")
        async with real() as conn:
            yield conn if state["done"] else _DroppingConn(conn, state)

    return connect


class _DroppingConn:
    """到点抛 ConnectionError，模拟对端掉线（只演练一次）。"""

    def __init__(self, inner: WsConnection, state: dict[str, Any]) -> None:
        self._inner = inner
        self._state = state

    async def send_str(self, data: str) -> None:
        await self._inner.send_str(data)

    async def receive_str(self, timeout: float) -> str:
        if not self._state["done"] and time.monotonic() >= self._state["drop_at"]:
            self._state["done"] = True
            self._state["resume_at"] = time.monotonic() + OUTAGE_S
            print(f"  [演练] 强制断线，压住 {OUTAGE_S:.0f}s 以制造缺口")
            raise ConnectionError("演练：强制断线")
        return await self._inner.receive_str(timeout)


def hhmm(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%H:%M:%SZ")


async def collect(feed: OkxWsFeed, seconds: float) -> list[Bar]:
    got: list[Bar] = []

    async def run() -> None:
        async for bar in feed.stream():
            got.append(bar)
            print(f"  [WS] {bar.symbol} {hhmm(bar.close_ts)} c={bar.close} v={bar.volume}")

    task = asyncio.create_task(run())
    await asyncio.sleep(seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return got


async def main(minutes: float, uids: list[str], drop_after: float | None) -> int:
    reg = load_registry(ROOT / "config")
    symbols = [reg.symbol(u) for u in uids]
    print(f"订阅 {[s.code for s in symbols]}，采集 {minutes} 分钟…"
          + (f"（第 {drop_after:.0f}s 强制断线演练）" if drop_after else ""))

    async with OkxRestClient() as rest:
        connector = flaky_connector(drop_after) if drop_after else None
        feed = OkxWsFeed(symbols, rest=rest, connect=connector) if connector else \
            OkxWsFeed(symbols, rest=rest)
        bars = await collect(feed, minutes * 60)

        if not bars:
            print("✗ 一根 bar 都没收到")
            return 1
        print(f"\n共收到 {len(bars)} 根，重连 {feed.reconnects} 次，回补 {len(feed.backfilled)} 段")
        for uid, lo, hi in feed.backfilled:
            print(f"   回补 {uid} ({hhmm(lo)}, {hhmm(hi)}]")

        failures = 0
        if drop_after is not None:
            drilled = feed.reconnects > 0 and bool(feed.backfilled)
            print(f"{'✅' if drilled else '❌'} 断线演练：重连 {feed.reconnects} 次、"
                  f"回补 {len(feed.backfilled)} 段")
            failures += 0 if drilled else 1
        for sym in symbols:
            mine = [b for b in bars if b.symbol == sym.uid]
            if not mine:
                print(f"✗ {sym.uid}: 未收到任何 bar")
                failures += 1
                continue
            ts = [b.close_ts for b in mine]

            dups = len(ts) - len(set(ts))
            gaps = [(a, b) for a, b in zip(ts, ts[1:], strict=False) if b - a != 60]
            lo, hi = min(ts) - 1, max(ts)
            rows = await rest.history_candles(sym.code, Timeframe.M1, after_ms=(hi + 1) * 1000)
            auth = {
                b.close_ts: b
                for b in normalize_candles(rows, symbol=sym.uid, timeframe=Timeframe.M1)
                if lo < b.close_ts <= hi
            }
            diffs = [
                f"{hhmm(b.close_ts)} {f}: ws={getattr(b, f)} rest={getattr(auth[b.close_ts], f)}"
                for b in mine
                if b.close_ts in auth
                for f in FIELDS
                if getattr(b, f) != getattr(auth[b.close_ts], f)
            ]
            missing = sorted(set(auth) - set(ts))

            ok = not (dups or gaps or diffs or missing)
            print(f"\n{'✅' if ok else '❌'} {sym.uid}: {len(mine)} 根 "
                  f"{hhmm(min(ts))}..{hhmm(max(ts))}")
            print(f"   重复={dups} 缺口={len(gaps)} REST覆盖={len(auth)} 漏收={len(missing)} "
                  f"字段差异={len(diffs)}")
            for d in diffs[:10]:
                print(f"     - {d}")
            for t in missing[:10]:
                print(f"     - 漏收 {hhmm(t)}")
            failures += 0 if ok else 1

    print("\n" + ("✅ 全部通过" if not failures else f"❌ {failures} 个标的未通过"))
    return 1 if failures else 0


if __name__ == "__main__":

    argv = sys.argv[1:]
    drop: float | None = None
    if "--drop-after" in argv:
        i = argv.index("--drop-after")
        drop = float(argv[i + 1])
        del argv[i : i + 2]
    mins = float(argv[0]) if argv else 6.0
    ids = argv[1:] or ["CRYPTO.OKX.BTCUSDT.PERP", "CRYPTO.OKX.ETHUSDT.PERP"]
    raise SystemExit(asyncio.run(main(mins, ids, drop)))
