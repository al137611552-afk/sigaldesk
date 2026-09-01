#!/usr/bin/env python
"""盯盘：实时行情 -> BarStore -> 规则引擎 -> 推送。M1 的入口。

链路：
    OKX WS（加密） ┐
                   ├─> BarStore（as-of 视图） -> RuleEngine -> Notifier(控制台/TG/Bark)
    Quote API 轮询（期货，仅在交易时段）┘

启动时先用 REST/历史接口**预热**：把指标喂饱、把 event 模式的"上一根"喂饱，
但**不发信号** —— 否则等于把历史行情当实时报一遍。

用法：
    .venv/bin/python scripts/watch.py                 # 一直跑
    .venv/bin/python scripts/watch.py --minutes 10    # 跑 10 分钟后退出（验收用）
    .venv/bin/python scripts/watch.py --crypto-only
    .venv/bin/python scripts/watch.py --rules-dir /tmp/myrules   # 试跑另一套规则

推送渠道由 .env 决定（TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID、BARK_URL）；
一个都没配就只打控制台。渠道失败只计数，绝不打断行情处理。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import functools
import os
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sigdesk.core.env import load_env  # noqa: E402
from sigdesk.core.models import CST, Bar, Market, Symbol, Timeframe  # noqa: E402
from sigdesk.core.registry import Registry, load_registry  # noqa: E402
from sigdesk.feed.okx import OkxRestClient  # noqa: E402
from sigdesk.feed.okx_ws import OkxWsFeed  # noqa: E402
from sigdesk.feed.polling import PollingFeed  # noqa: E402
from sigdesk.feed.quote_api import QuoteApiClient, QuoteApiConfig, normalize_klines  # noqa: E402
from sigdesk.rules.engine import RuleEngine  # noqa: E402
from sigdesk.rules.loader import load_rules  # noqa: E402
from sigdesk.rules.model import Rule, Signal, store_timeframes  # noqa: E402
from sigdesk.sinks.notify import (  # noqa: E402
    BarkNotifier,
    MultiNotifier,
    Notifier,
    TelegramNotifier,
    format_signal,
)
from sigdesk.store.bar_store import DEFAULT_TIMEFRAMES, BarStore  # noqa: E402
from sigdesk.store.parquet_io import write_bars  # noqa: E402
from sigdesk.store.runtime_store import RuntimeStore  # noqa: E402
from sigdesk.trade.desk import TradeDesk  # noqa: E402
from sigdesk.trade.loader import load_trading  # noqa: E402
from sigdesk.web.api import ServiceState, create_app  # noqa: E402
from sigdesk.web.health import HealthMonitor  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 脚本自己读 .env：`set -a; . ./.env` 是 bash 专有写法，Windows 上没有对应物。
# 查找顺序 SIGDESK_ENV -> ./.env -> ~/.signal-desk/.env（换新包也不用重配）。
ENV = load_env(ROOT)
# 预热多少根 1m：够 1h 周期上的 ema(60) 成形（60×60），再留些余量
WARMUP_BARS = 4000
DEFAULT_STATE_DB = ROOT / "data" / "runtime.sqlite3"
# bar 落在 data/bars/，运行态落在 data/ —— 两者必须与回补脚本一致，
# 否则 backfill/build_continuous 落的盘面板一根都读不到（踩过）。
DEFAULT_DATA_ROOT = ROOT / "data" / "bars"


class ConsoleNotifier:
    """控制台渠道。永远启用 —— 没配任何推送时它就是唯一出口，也是验收时的观察窗。"""

    name = "console"

    async def send(self, text: str) -> bool:
        print("\n" + "─" * 60 + f"\n{text}\n" + "─" * 60)
        return True


def build_notifier() -> MultiNotifier:
    channels: list[Notifier] = [ConsoleNotifier()]
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        channels.append(TelegramNotifier(token=token, chat_id=chat))
    bark = os.environ.get("BARK_URL")
    if bark:
        channels.append(BarkNotifier(base_url=bark))
    names = ", ".join(c.name for c in channels)
    print(f"推送渠道: {names}"
          + ("" if len(channels) > 1 else "（未配置 TG/Bark，仅控制台）"))
    return MultiNotifier(channels)


def wanted_symbols(rules: list[Rule], registry: Registry) -> list[Symbol]:
    uids = sorted({uid for r in rules for uid in r.universe})
    out: list[Symbol] = []
    for uid in uids:
        try:
            out.append(registry.symbol(uid))
        except KeyError:
            print(f"⚠️  规则引用了未注册的标的 {uid}，已跳过")
    return out


def hhmm(ts: int, symbol: str) -> str:
    if symbol.startswith(Market.CRYPTO.value):
        return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%H:%M:%SZ")
    return dt.datetime.fromtimestamp(ts, CST).strftime("%H:%M:%S")


async def fetch_crypto_history(
    rest: OkxRestClient, symbols: list[Symbol], bars: int
) -> list[Bar]:
    now = int(dt.datetime.now(dt.UTC).timestamp())
    end = now // 60 * 60
    out: list[Bar] = []
    for sym in symbols:
        history = await rest.fetch_range(sym.code, sym.uid, Timeframe.M1, end - bars * 60, end)
        print(f"  取历史 {sym.uid}: {len(history)} 根 1m")
        out.extend(history)
    return out


async def fetch_futures_history(
    client: QuoteApiClient, registry: Registry, symbols: list[Symbol], bars: int
) -> list[Bar]:
    now = int(dt.datetime.now(dt.UTC).timestamp())
    out: list[Bar] = []
    for sym in symbols:
        assert sym.quote_code is not None
        rows = await client.kline_by_count(sym.quote_code, Timeframe.M1, min(bars, 2000))
        history = [
            b
            for b in normalize_klines(
                rows, symbol=sym.uid, timeframe=Timeframe.M1, now_ts=now,
                calendar=registry.calendar_of(sym.uid),
            )
            if b.closed
        ]
        print(f"  取历史 {sym.uid}: {len(history)} 根 1m")
        out.extend(history)
    return out


def apply_history(
    engine: RuleEngine, store: BarStore, history: list[Bar], *, restored: bool
) -> list[Signal]:
    """把历史交给引擎。**首启与重启走的路不同，这一点必须显式**：

    - 首启（没有存档）：``prime`` —— 只喂指标与条件日志，**不发信号**。
      否则等于把几十小时的历史行情当实时报一遍。
    - 重启（有存档）：``resume`` —— 游标之前的只用于重建状态，
      游标之后的（= 停机期间漏掉的）补判并补报。这就是"不丢报"。

    注意：重启时若新增了规则，它在游标之前的历史不会被记账，因此要等积够新 bar 才可能触发。
    这是刻意的保守做法 —— 总比一上线就按旧数据补报一堆信号好。
    """
    history = sorted(history, key=lambda b: (b.close_ts, b.symbol))
    if restored:
        missed = engine.resume(history)
        print(f"  重启补判：{len(history)} 根历史，补回 {len(missed)} 条停机期间的信号")
        return missed
    derived: list[Bar] = []
    for bar in history:
        derived.extend(store.push(bar))
    engine.prime(derived)
    extra = len(derived) - len(history)
    print(f"  首次预热：{len(history)} 根 1m -> 派生 {extra} 根高周期，不发信号")
    return []


async def pump(
    feed: object, queue: asyncio.Queue[Bar | None], label: str,
    health: HealthMonitor | None = None,
) -> None:
    """把一个 Feed 的产出灌进公共队列。异常不静默 —— 断流必须看得见（面板上也要看得见）。"""
    if health:
        health.on_feed_event(label, connected=True)
    try:
        async for bar in feed.stream():  # type: ignore[attr-defined]
            if health:
                health.observe_feed(label, feed)
            await queue.put(bar)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"❌ {label} 数据流中断: {type(e).__name__}: {e}")
        if health:
            health.on_feed_event(label, connected=False, error=f"{type(e).__name__}: {e}")
    finally:
        if health:
            health.on_feed_event(label, connected=False)
        await queue.put(None)


async def serve_panel(
    state: ServiceState, host: str, port: int
) -> tuple[Any, asyncio.Task[None]]:
    """与引擎**同进程**起 Web（ARCHITECTURE §7）：信号经内存队列直接推给 SSE，
    不落盘再轮询，也就没有额外延迟。

    返回 server 与 task 两样：退出时要 ``should_exit`` 优雅关停，
    直接 cancel 会让 uvicorn 的 lifespan 任务抛 CancelledError ——
    每次正常退出都打一段 ERROR 回溯，会训练人忽略真正的报错。
    """
    import uvicorn

    config = uvicorn.Config(create_app(state), host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    print(f"面板: http://{host}:{port}")
    return server, asyncio.create_task(server.serve())


async def run(
    minutes: float | None,
    crypto_only: bool,
    warmup_bars: int,
    rules_dir: pathlib.Path,
    state_db: pathlib.Path,
    web: tuple[str, int] | None = None,
    data_root: pathlib.Path | None = None,
) -> int:
    registry = load_registry(ROOT / "config")
    rules = load_rules(rules_dir)
    if not rules:
        print("没有启用的规则（config/rules/ 为空？）")
        return 1
    symbols = wanted_symbols(rules, registry)
    crypto = [s for s in symbols if s.market is Market.CRYPTO]
    futures = [] if crypto_only else [s for s in symbols if s.market is Market.CN_FUTURES]

    print(f"规则 {len(rules)} 条（来自 {rules_dir}）: {', '.join(r.id for r in rules)}")
    print(f"标的: 加密 {[s.code for s in crypto]} / 期货 {[s.code for s in futures]}")

    # **必须取并集**：watch.py 同时喂两个东西 —— 规则引擎（只要规则用到的周期）
    # 和面板图表（要全部可选周期）。只按规则派生的话，规则没用到的周期就**静默停更**，
    # 面板上看着就是"行情连不上了"（今天真踩过）。
    # store_timeframes 保证 at() 跨级别引用到的周期也在里面。
    tfs = sorted(set(DEFAULT_TIMEFRAMES) | set(store_timeframes(rules)), key=lambda t: t.rank)
    store = BarStore(timeframes=tfs)
    engine = RuleEngine(rules, store)
    notifier = build_notifier()
    health = HealthMonitor(
        calendars=registry.calendars,
        symbol_calendars={s.uid: s.calendar for s in symbols},
    )
    health.started_at = int(dt.datetime.now(dt.UTC).timestamp())

    desk = TradeDesk(symbols, load_trading(ROOT / "config" / "trading.yaml"))
    print(f"纸上交易台: {'已启用' if desk.params.enabled else '未启用'}"
          f"（config/trading.yaml）" + (f"，初始资金 {desk.params.initial_cash:,.0f}"
                                        if desk.params.enabled else ""))
    queue: asyncio.Queue[Bar | None] = asyncio.Queue()
    tasks: list[asyncio.Task[None]] = []
    fired = 0
    errors = 0

    web_server: Any = None
    web_task: asyncio.Task[None] | None = None
    async with contextlib.AsyncExitStack() as stack:
        runtime = stack.enter_context(RuntimeStore(state_db))
        restored = engine.restore(runtime.load_state())
        desk.restore(runtime.load_trade_state())
        print(f"运行态: {state_db}（恢复 {restored} 个规则实例）"
              if restored else f"运行态: {state_db}（首次启动，无存档）")

        history: list[Bar] = []
        # **行情一律落盘，不管开不开 --web**。以前只在 --web 时落，
        # 后果是「盯盘进程跑得好好的，面板却一直是几天前的数据」——
        # 看起来就像行情连不上了。write_bars 是幂等的，重复写同一根只是覆盖。
        root = data_root or DEFAULT_DATA_ROOT

        if crypto:
            rest = await stack.enter_async_context(OkxRestClient())
            print("取加密历史…")
            try:
                history += await fetch_crypto_history(rest, crypto, warmup_bars)
            except Exception as e:  # noqa: BLE001
                # **一个市场挂了不该拖垮另一个。** 加密连不上时期货照样能盯，反之亦然。
                # 只有两边都起不来才算真的起不来（见下面的 if not started）。
                print(f"  ⚠️  加密历史取失败，本次不盯加密: {type(e).__name__}: {e}")
                crypto = []
        client: QuoteApiClient | None = None
        if futures:
            missing = [k for k in ("QUOTE_API_BASE", "QUOTE_API_KEY") if not os.environ.get(k)]
            if missing:
                # 缺凭据是最常见的"启动失败"，要说清楚缺哪个、怎么配，别抛一个 KeyError
                print(f"  ⚠️  缺少 {'、'.join(missing)}，本次不盯期货"
                      f"（用 `python scripts/setup_env.py` 配一次即可）")
                futures = []
            else:
                try:
                    cfg = QuoteApiConfig(
                        base_url=os.environ["QUOTE_API_BASE"],
                        api_key=os.environ["QUOTE_API_KEY"],
                        tls_fingerprint=os.environ.get("QUOTE_API_TLS_FINGERPRINT", ""),
                    )
                    client = await stack.enter_async_context(QuoteApiClient(cfg))
                    print("取期货历史…")
                    history += await fetch_futures_history(
                        client, registry, futures, warmup_bars)
                except Exception as e:  # noqa: BLE001
                    print(f"  ⚠️  期货历史取失败，本次不盯期货: {type(e).__name__}: {e}")
                    futures = []
                    client = None

        missed = apply_history(engine, store, history, restored=bool(restored))
        for signal in missed:
            fired += 1
            await deliver(notifier, signal, rules)
        runtime.append_signals(missed)
        runtime.save_state(engine.snapshot())

        # 预热/补判之后才给 Feed 播种：不播种的话首轮轮询会把重叠窗口当新数据重发
        seeds = store.resume_map()
        if crypto:
            tasks.append(asyncio.create_task(
                pump(OkxWsFeed(crypto, rest=rest, resume_from=seeds), queue, "OKX WS", health)
            ))
        if futures and client is not None:
            tasks.append(asyncio.create_task(
                pump(
                    PollingFeed(client, futures, registry.calendars, resume_from=seeds),
                    queue,
                    "Quote API 轮询",
                    health,
                )
            ))

        dumped = 0
        for sym in symbols:
            view = store.view(sym.uid, as_of=2**31)
            for tf in (Timeframe.M1, *store.timeframes):
                series = list(view.bars(tf))
                if series:
                    write_bars(root, series)
                    dumped += len(series)
        print(f"  行情落盘: {dumped} 根 -> {root}（周期 "
              f"{', '.join(t.value for t in (Timeframe.M1, *store.timeframes))}）")

        service: ServiceState | None = None
        if web is not None:
            service = ServiceState(
                runtime=runtime, data_root=root, registry=registry, rules=rules,
                health=health, live=True, rules_dir=rules_dir,
                engine=engine,   # 链路状态条要读引擎的内存状态；不传的话面板只会显示"未接入"
            )
            web_server, web_task = await serve_panel(service, *web)
        if not tasks:
            # 两边都没起来才算真的起不来。此时要说清楚是"没配标的"还是"都连不上"。
            print("\n没有任何行情源可用 —— 上面的 ⚠️ 说明了原因。")
            print("排查：`python scripts/setup_env.py --show` 看凭据，")
            print("      加密走 OKX 公开接口（连不上多半是本机 DNS，见 CLAUDE.md）。")
            return 1
        live_markets = ("加密" if crypto else "") + ("、" if crypto and futures else "") \
            + ("期货" if futures else "")
        print(f"\n本次盯盘的市场: {live_markets}")

        limit = f"（{minutes:.0f} 分钟后退出）" if minutes else ""
        print(f"\n开始盯盘{limit}…\n")
        deadline = None if minutes is None else asyncio.get_running_loop().time() + minutes * 60
        try:
            while True:
                timeout = None if deadline is None else deadline - asyncio.get_running_loop().time()
                if timeout is not None and timeout <= 0:
                    break
                try:
                    bar = await asyncio.wait_for(queue.get(), timeout=timeout)
                except TimeoutError:
                    break
                if bar is None:
                    continue
                print(f"  [bar] {bar.symbol} {bar.timeframe} {hhmm(bar.close_ts, bar.symbol)} "
                      f"c={bar.close}")
                health.on_bar(bar)
                try:
                    # 整批交给引擎：同刻收盘的大级别必须先记账，扳机才读得到这一根
                    derived = store.push(bar)
                    # 顺序不能反：先用这根撮合（挂单按开盘成交、持仓判出场），
                    # 再产出信号挂新单等下一根 —— 反了就是拿收盘后的信息吃开盘价。
                    trade_fills = desk.on_bars(derived)
                    signals = engine.on_bars(derived)
                    desk.on_signals(signals)
                    if trade_fills:
                        runtime.append_fills(trade_fills)
                        for f in trade_fills:
                            print(f"  [成交] {f.symbol} {f.side} {f.qty:g} @ {f.price:.6g}"
                                  f" ({f.kind}"
                                  + (f", 盈亏 {f.realized:+.2f}" if f.realized else "") + ")")
                    write_bars(root, derived)  # 面板要能画出刚收的这根
                    for signal in signals:
                        fired += 1
                        health.signals_fired += 1
                        await deliver(notifier, signal, rules)
                        if service is not None:
                            service.broadcaster.publish(signal.as_dict())
                    if signals:
                        runtime.append_signals(signals)
                    runtime.save_state(engine.snapshot())
                    if desk.params.enabled:
                        runtime.save_trade_state(desk.snapshot())
                except Exception as e:  # noqa: BLE001
                    # 单根 bar 的异常（如上游乱序导致的时间倒流）不该让盯盘半夜整个退出；
                    # 但必须吼出来，否则就成了静默吞错。
                    errors += 1
                    health.bar_errors += 1
                    print(f"❌ 处理 {bar.symbol} {hhmm(bar.close_ts, bar.symbol)} 出错，"
                          f"已跳过该根: {type(e).__name__}: {e}")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if web_server is not None and web_task is not None:
                web_server.should_exit = True  # 优雅关停，别 cancel
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(web_task, timeout=5.0)

    print(f"\n共触发 {fired} 条信号；跳过 {errors} 根异常 bar；"
          f"推送失败计数: {notifier.failures or '无'}")
    print(f"运行态已存入 {state_db}（下次启动会从这里恢复，不丢报不重报）")
    if desk.params.enabled:
        summary = desk.summary()
        print(f"纸上账户: 权益 {summary['equity']:,.2f}"
              f"（{summary['return_pct']:+.2%}）· 已实现 {summary['realized']:+,.2f}"
              f" · 手续费 {summary['fees']:,.2f} · 持仓 {len(summary['positions'])} 笔")
    return 0


async def deliver(notifier: MultiNotifier, signal: Signal, rules: list[Rule]) -> None:
    description = next((r.description for r in rules if r.id == signal.rule_id), "")
    await notifier.send(format_signal(signal, description))


def main() -> int:
    ap = argparse.ArgumentParser(description="signal-desk 盯盘")
    ap.add_argument("--minutes", type=float, default=None, help="跑多久后退出（默认一直跑）")
    ap.add_argument("--crypto-only", action="store_true", help="只订阅加密")
    ap.add_argument("--warmup-bars", type=int, default=WARMUP_BARS, help="预热的 1m 根数")
    ap.add_argument("--rules-dir", type=pathlib.Path, default=ROOT / "config" / "rules",
                    help="规则目录（默认 config/rules）")
    ap.add_argument("--state-db", type=pathlib.Path, default=DEFAULT_STATE_DB,
                    help="运行态 SQLite 路径；重启后据此恢复状态机与去重表")
    ap.add_argument("--web", nargs="?", const="127.0.0.1:8000", default=None,
                    metavar="HOST:PORT", help="同时起只读面板（默认 127.0.0.1:8000）")
    ap.add_argument("--data-root", type=pathlib.Path, default=DEFAULT_DATA_ROOT,
                    help="行情 Parquet 根目录；面板的 K 线从这里读")
    args = ap.parse_args()
    try:
        web = None
        if args.web:
            host, _, port = args.web.rpartition(":")
            web = (host or "127.0.0.1", int(port))
        return asyncio.run(
            run(args.minutes, args.crypto_only, args.warmup_bars, args.rules_dir,
                args.state_db, web, args.data_root)
        )
    except KeyboardInterrupt:
        print("\n已停止")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
