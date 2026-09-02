"""只读 Web API 与运行健康单测。不起真服务器，用 TestClient 直接打。

覆盖 M3 的三条验收：
1. 图上标注的信号点与 SignalStore 记录逐条对得上；
2. 同一批数据两次统计结果完全可复现；
3. 数据缺口/断连在健康面板可见。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sigdesk.core.calendar import MarketCalendar
from sigdesk.core.models import Bar, Timeframe
from sigdesk.core.registry import load_registry
from sigdesk.feed.okx import normalize_candles
from sigdesk.rules.engine import RuleEngine
from sigdesk.rules.loader import load_rule
from sigdesk.store.bar_store import BarStore
from sigdesk.store.parquet_io import write_bars
from sigdesk.store.runtime_store import RuntimeStore
from sigdesk.web.api import ServiceState, SignalBroadcaster, create_app
from sigdesk.web.health import HealthMonitor

BTC = "CRYPTO.OKX.BTCUSDT.PERP"
RB = "CN.SHFE.rb2610"

RULE: dict[str, Any] = {
    "id": "web-demo",
    "universe": [BTC],
    "timeframes": {"trend": "15m", "trigger": "1m"},
    "conditions": [
        {"on": "trend", "mode": "state", "when": "close > ema(close,5)"},
        {"on": "trigger", "mode": "event", "when": "cross_up(close, ema(close,10))"},
    ],
    "context": {"atr14": "atr(14)"},
    "emit": {"direction": "long", "dedup_key": "{symbol}:{rule}:{bar_close_ts}"},
}


@pytest.fixture
def wired(tmp_path: pathlib.Path, btc_swap_okx: dict[str, Any]) -> tuple[TestClient, ServiceState]:
    """跑一遍规则，把 bar 落 Parquet、信号落 SQLite，再把面板接上去。"""
    bars = normalize_candles(btc_swap_okx["1m"], symbol=BTC, timeframe=Timeframe.M1)
    store = BarStore(timeframes=[Timeframe.M5, Timeframe.M15])
    engine = RuleEngine([load_rule(RULE)], store)
    persisted: list[Bar] = []
    signals = []
    for b in bars:
        derived = store.push(b)
        persisted.extend(derived)
        signals.extend(engine.on_bars(derived))
    write_bars(tmp_path / "data", persisted)

    runtime = RuntimeStore(tmp_path / "runtime.sqlite3")
    runtime.append_signals(signals)
    state = ServiceState(runtime=runtime, data_root=tmp_path / "data", live=False)
    return TestClient(create_app(state)), state


# ---------------------------------------------------------------- 基本端点


def test_endpoints_are_reachable(wired: tuple[TestClient, ServiceState]) -> None:
    client, _ = wired
    for path in ("/api/meta", "/api/health", "/api/signals", "/api/stats"):
        assert client.get(path).status_code == 200, path


def test_index_and_static_are_served(wired: tuple[TestClient, ServiceState]) -> None:
    client, _ = wired
    assert client.get("/").status_code == 200
    assert "lightweight-charts" in client.get("/").text
    js = client.get("/static/app.js")
    assert js.status_code == 200 and "drawMarkers" in js.text
    vendor = client.get("/static/vendor/lightweight-charts.standalone.production.js")
    assert vendor.status_code == 200, "图表库要内置在仓库里，不依赖 CDN"


def test_bars_endpoint_rejects_unknown_timeframe(
    wired: tuple[TestClient, ServiceState],
) -> None:
    client, _ = wired
    assert client.get("/api/bars?symbol=X&timeframe=7m").status_code == 400


def test_signals_can_be_filtered(wired: tuple[TestClient, ServiceState]) -> None:
    client, _ = wired
    assert client.get("/api/signals?rule_id=nope").json()["total"] == 0
    assert client.get(f"/api/signals?symbol={BTC}").json()["total"] > 0


# ------------------------------------------- 验收 1：标注与记录逐条对得上


@pytest.mark.parametrize("tf", ["1m", "5m", "15m"])
def test_markers_match_the_signal_store_exactly(
    wired: tuple[TestClient, ServiceState], tf: str
) -> None:
    """**M3 验收**：图上标注的信号点与 SignalStore 记录逐条对得上。

    标注在服务端算（前端只负责画），所以这条能被真正测到：
    每条记录要么有一个标注、要么在 dropped 里，**不重不漏**；
    且每个标注的分桶必须确实存在于该周期的 bar 序列中。

    密集处会把同一根 bar 同方向的多条折成一枚「×N」，所以逐条对账要看 ``members``
    而不是顶层代表 —— 折叠**只改画法，不改账**：一条记录都不许在折叠中消失。
    """
    client, _ = wired
    recorded = client.get(f"/api/signals?symbol={BTC}&limit=5000").json()["signals"]
    data = client.get(f"/api/markers?symbol={BTC}&timeframe={tf}").json()
    bars = client.get(f"/api/bars?symbol={BTC}&timeframe={tf}&limit=5000").json()["bars"]

    assert data["signals"] == len(recorded)
    members = [x for m in data["markers"] for x in m["members"]]
    marked = {x["dedup_key"] for x in members}
    dropped = {d["dedup_key"] for d in data["dropped"]}
    assert len(marked) == len(members), "同一条记录出现在多枚标注里"
    assert marked | dropped == {s["dedup_key"] for s in recorded}, "有记录既没标注也没进 dropped"
    assert not (marked & dropped), "同一条记录既标注又被丢弃"
    assert sum(m["count"] for m in data["markers"]) == len(marked), "count 与成员数对不上"

    stamps = {b["close_ts"] for b in bars}
    period = {"1m": 60, "5m": 300, "15m": 900}[tf]
    by_key = {s["dedup_key"]: s for s in recorded}
    for m in data["markers"]:
        assert m["bucket_ts"] in stamps, "标注落在了一根不存在的 bar 上"
        assert m["dedup_key"] == m["members"][0]["dedup_key"], "代表必须是排在最前的成员"
        for x in m["members"]:
            src = by_key[x["dedup_key"]]
            assert m["bucket_ts"] == -(-src["fired_at"] // period) * period, "分桶与引擎不一致"
            assert x["trigger_price"] == src["trigger_price"]
            assert x["fired_at"] == src["fired_at"]
            assert src["direction"] == m["direction"], "折叠把不同方向的信号并到了一起"


def test_markers_do_not_snap_to_the_nearest_bar(
    wired: tuple[TestClient, ServiceState],
) -> None:
    """落不到任何一根上的信号必须如实进 dropped，不能就近吸附 ——
    吸附会让标注落在一根它并不属于的 bar 上，图看着好看、结论是错的。"""
    client, state = wired
    far = client.get(f"/api/signals?symbol={BTC}").json()["signals"][0]
    # 造一条时间上远在数据之外的信号
    state.runtime._conn.execute(  # noqa: SLF001
        "INSERT INTO signal (rule_id, symbol, direction, timeframe, fired_at, trigger_price,"
        " dedup_key, context, role_bars, tentative, priority) "
        "VALUES ('x', ?, 'long', '1m', 1, 1.0, 'orphan', '{}', '{}', 0, 'normal')",
        (BTC,),
    )
    state.runtime._conn.commit()  # noqa: SLF001

    data = client.get(f"/api/markers?symbol={BTC}&timeframe=1m").json()

    assert "orphan" in {d["dedup_key"] for d in data["dropped"]}
    assert "orphan" not in {m["dedup_key"] for m in data["markers"]}
    assert far["dedup_key"] in {m["dedup_key"] for m in data["markers"]}


def test_markers_accept_daily_timeframe(wired: tuple[TestClient, ServiceState]) -> None:
    """日线没有墙钟桶边界，标注改落到"收盘不早于信号"的第一根日线上。
    这里数据里没有日线，所以应当是 200 + 全部计入 dropped，而不是 400，
    更不能因为 period=0 而除零。"""
    client, _ = wired
    resp = client.get(f"/api/markers?symbol={BTC}&timeframe=1d")
    assert resp.status_code == 200
    body = resp.json()
    assert body["markers"] == []
    assert len(body["dropped"]) == body["signals"], "落不到 bar 上的信号必须如实计入 dropped"


# ------------------------------------------- 验收 2：统计可复现


def test_stats_are_reproducible_across_calls(wired: tuple[TestClient, ServiceState]) -> None:
    """**M3 验收**：同一条规则跑两次，统计结果完全可复现。"""
    client, _ = wired
    q = "/api/stats?horizon_bars=20&stop_pct=0.005&target_pct=0.01&cost_bps=2"
    first, second = client.get(q).json(), client.get(q).json()
    assert first == second
    assert first["overall"]["signals"] > 0, "没有信号可统计，这条验收就没有说服力"


def test_stats_params_change_the_result_and_are_echoed(
    wired: tuple[TestClient, ServiceState],
) -> None:
    """口径必须原样带回 —— 一份不写明口径的胜率没有意义。"""
    client, _ = wired
    cheap = client.get("/api/stats?horizon_bars=20&cost_bps=0").json()
    pricey = client.get("/api/stats?horizon_bars=20&cost_bps=50").json()
    assert cheap["params"]["cost_bps"] == 0.0
    assert pricey["params"]["cost_bps"] == 50.0
    assert pricey["overall"]["avg_return"] < cheap["overall"]["avg_return"], "成本没被扣"


def test_stats_outcomes_line_up_with_signals(wired: tuple[TestClient, ServiceState]) -> None:
    client, _ = wired
    sigs = client.get("/api/signals?limit=5000").json()["signals"]
    rep = client.get("/api/stats").json()
    assert len(rep["outcomes"]) == len(sigs)
    assert [o["fired_at"] for o in rep["outcomes"]] == [s["fired_at"] for s in sigs]


# ------------------------------------------- 验收 3：缺口与断连可见


def test_health_reports_gaps_and_disconnects() -> None:
    """**M3 验收**：数据缺口/断连在健康面板可见。"""
    mon = HealthMonitor()
    mon.started_at = 1000
    for ts in (1060, 1120):
        mon.on_bar(Bar(BTC, Timeframe.M1, ts - 60, ts, 1, 1, 1, 1, 1))
    mon.on_bar(Bar(BTC, Timeframe.M1, 1300, 1360, 1, 1, 1, 1, 1))  # 跳了 4 分钟
    mon.on_feed_event("OKX WS", connected=False, reconnects=3, error="网络抖动")

    snap = mon.snapshot(now_ts=1400)

    assert snap["total_gaps"] == 1
    assert snap["symbols"][0]["gaps"] == [{"from": 1120, "to": 1360}]
    assert snap["feeds"][0]["connected"] is False
    assert snap["feeds"][0]["reconnects"] == 3
    assert snap["feeds"][0]["last_error"] == "网络抖动"
    assert snap["healthy"] is False
    assert "OKX WS" in snap["problems"]


def test_health_flags_stale_data() -> None:
    mon = HealthMonitor()
    mon.on_bar(Bar(BTC, Timeframe.M1, 940, 1000, 1, 1, 1, 1, 1))
    assert mon.is_stale(BTC, 1100) is False, "不到 2.5 个周期不算滞后"
    assert mon.is_stale(BTC, 1300) is True
    assert mon.snapshot(1300)["healthy"] is False


def test_closed_market_is_not_reported_as_stale() -> None:
    """期货午休两小时没数据是**正常**的。拿 7×24 的尺子量会满屏假告警。"""
    cal = MarketCalendar.from_config("rb", ["09:00-11:30", "13:30-15:00"], [])
    mon = HealthMonitor(calendars={"rb": cal}, symbol_calendars={RB: "rb"})

    def cst(h: int, m: int) -> int:
        return int(dt.datetime(2026, 8, 28, h, m, tzinfo=dt.timezone(dt.timedelta(hours=8)))
                   .timestamp())

    mon.on_bar(Bar(RB, Timeframe.M1, cst(11, 29), cst(11, 30), 1, 1, 1, 1, 1))

    assert mon.is_stale(RB, cst(12, 30)) is False, "午休被误报成数据滞后"
    assert mon.snapshot(cst(12, 30))["symbols"][0]["in_session"] is False
    assert mon.is_stale(RB, cst(13, 40)) is True, "开盘后仍无数据就该报滞后"


def test_health_endpoint_says_so_when_not_live(
    wired: tuple[TestClient, ServiceState],
) -> None:
    """独立只读模式要如实说"未接入实时引擎"，而不是假装一切正常。"""
    client, _ = wired
    body = client.get("/api/health").json()
    assert body["live"] is False
    assert body["healthy"] is None
    assert "未接入" in body["note"]


def test_health_endpoint_reports_live_snapshot(
    wired: tuple[TestClient, ServiceState],
) -> None:
    client, state = wired
    state.health = HealthMonitor()
    state.health.started_at = 100
    state.live = True
    state.health.on_feed_event("OKX WS", connected=True, reconnects=1)

    body = client.get("/api/health").json()

    assert body["live"] is True and body["healthy"] is True
    assert body["feeds"][0]["reconnects"] == 1
    assert "sse_subscribers" in body


# ---------------------------------------------------------------- SSE


async def test_broadcaster_drops_instead_of_blocking() -> None:
    """面板卡住时丢它自己的旧消息，绝不能反压到行情处理上 ——
    一个开着不管的浏览器标签页不该拖垮盯盘。"""
    b = SignalBroadcaster(maxsize=2)
    q = b.subscribe()
    for i in range(5):
        b.publish({"i": i})
    assert q.qsize() == 2
    assert b.dropped == 3
    b.unsubscribe(q)
    b.publish({"i": 99})  # 没有订阅者也不该炸
    assert b.subscribers == 0


async def test_broadcaster_fans_out_to_every_subscriber() -> None:
    b = SignalBroadcaster()
    a, c = b.subscribe(), b.subscribe()
    b.publish({"x": 1})
    assert await asyncio.wait_for(a.get(), 1) == '{"x": 1}'
    assert await asyncio.wait_for(c.get(), 1) == '{"x": 1}'


def test_sse_route_is_registered(wired: tuple[TestClient, ServiceState]) -> None:
    """SSE 是一条**无限**流，用 TestClient 消费它会把测试挂死（试过）。

    这里只验证路由存在；真实的 SSE 连通性用 curl 对着跑起来的服务端到端验（见 DEVLOG）。
    流内容的正确性由上面两条 broadcaster 测试覆盖 —— 那才是真正会出错的部分。
    """
    client, _ = wired
    paths = {r.path for r in client.app.routes}  # type: ignore[attr-defined]
    assert "/api/events" in paths


def test_feed_counters_reach_the_health_panel() -> None:
    """**M3 验收（接线部分）**：Feed 上的重连/缺口/回补计数要真能到达面板。

    这段接线原本写在 scripts/watch.py 里 —— 那样"缺口能不能被看见"就只能靠肉眼看日志。
    挪进 HealthMonitor.observe_feed 之后才测得到。
    """

    class _Feed:  # 与 OkxWsFeed / PollingFeed 暴露的字段一致
        reconnects = 7
        gaps_detected = [("s", 1, 2), ("s", 3, 4)]
        backfilled = [("s", 1, 2)]

    mon = HealthMonitor()
    mon.observe_feed("OKX WS", _Feed())
    snap = mon.snapshot(now_ts=100)

    assert snap["feeds"][0] == {
        "name": "OKX WS", "connected": True, "reconnects": 7,
        "gaps": 2, "backfills": 1, "last_error": "",
    }

    mon.observe_feed("OKX WS", _Feed(), connected=False)
    assert mon.snapshot(100)["healthy"] is False


def test_feed_without_counters_does_not_crash() -> None:
    """ReplayFeed 没有这些字段 —— 缺字段要退化成 0，不能抛。"""
    mon = HealthMonitor()
    mon.observe_feed("replay", object())
    assert mon.snapshot(100)["feeds"][0]["reconnects"] == 0


# ---------------------------------------------------------------- 链路状态


def _chain_engine() -> tuple[RuleEngine, BarStore]:
    raw: dict[str, Any] = {
        "id": "chain-demo",
        "universe": [BTC],
        "timeframes": {"trend": "5m", "setup": "1m", "trigger": "1m"},
        "conditions": [
            {"on": "trend", "mode": "state", "when": "close > 100"},
            {"on": "setup", "mode": "window", "within": 3, "when": "volume > 50"},
            {"on": "trigger", "mode": "event", "when": "close > open"},
        ],
        "emit": {"direction": "long", "ttl": "4 bars", "cooldown": "5m"},
    }
    store = BarStore(timeframes=[Timeframe.M5])
    return RuleEngine([load_rule(raw)], store), store


def _bar(ts: int, close: float, volume: float = 10.0) -> Bar:
    return Bar(BTC, Timeframe.M1, ts - 60, ts, close, close + 0.5, close - 0.5, close, volume)


def test_trade_endpoint_says_so_without_a_desk(
    wired: tuple[TestClient, ServiceState],
) -> None:
    """交易台默认关闭，独立只读模式也没有它 —— 要如实说，并且**历史成交仍然能看**
    （复盘不需要引擎在跑）。"""
    client, _ = wired
    body = client.get("/api/trade").json()
    assert body["live"] is False and body["summary"] is None
    assert "未接入" in body["note"]
    assert body["fills"] == [] and body["total_fills"] == 0


def test_trade_endpoint_serves_desk_summary_and_persisted_fills(
    wired: tuple[TestClient, ServiceState],
) -> None:
    from sigdesk.core.models import Market, Symbol
    from sigdesk.trade.desk import DeskParams, TradeDesk
    from sigdesk.trade.paper import FillParams
    from sigdesk.trade.risk import RiskParams
    from sigdesk.trade.strategy import StrategyParams

    client, state = wired
    sym = Symbol(uid=BTC, market=Market.CRYPTO, exchange="OKX", code="BTC-USDT-SWAP",
                 calendar="crypto_24x7", multiplier=0.01)
    desk = TradeDesk([sym], DeskParams(
        enabled=True, initial_cash=50_000.0,
        strategy=StrategyParams(mode="fixed", fixed_qty=1.0),
        risk=RiskParams(max_symbol_exposure=10.0, max_total_exposure=10.0,
                        max_risk_per_trade=1.0, daily_loss_limit=0.0, max_orders_per_window=0),
        fills=FillParams(fee_bps=0.0, slippage_bps=0.0),
    ))
    from sigdesk.rules.model import Direction, Signal
    s = Signal(rule_id="r1", symbol=BTC, direction=Direction.LONG, timeframe=Timeframe.M1,
               fired_at=600, trigger_price=100.0, dedup_key="k1")
    desk.on_bars([Bar(BTC, Timeframe.M1, 540, 600, 100.0, 100.1, 99.9, 100.0, 1.0)])
    desk.on_signals([s])
    fills = desk.on_bars([Bar(BTC, Timeframe.M1, 600, 660, 100.0, 100.2, 99.9, 100.1, 1.0)])
    state.runtime.append_fills(fills)
    state.desk = desk

    body = client.get("/api/trade").json()

    assert body["live"] is True
    assert body["summary"]["initial_cash"] == 50_000.0
    assert body["summary"]["positions"][0]["symbol"] == BTC
    assert body["total_fills"] == 1
    assert body["fills"][0]["kind"] == "entry"
    assert body["fills"][0]["signal_key"] == "k1", "成交要标明来源信号"


def test_fills_are_deduped_by_signal_kind_and_bar(tmp_path: pathlib.Path) -> None:
    """重启补喂会重复产出同一批 bar，账不能被重复计。"""
    from sigdesk.trade.model import Fill, FillKind, Side

    f = Fill(signal_key="k1", symbol=BTC, side=Side.BUY, qty=1.0, price=100.0,
             ts=660, kind=FillKind.ENTRY, fee=0.1)
    with RuntimeStore(tmp_path / "r.sqlite3") as rs:
        assert rs.append_fills([f]) == 1
        assert rs.append_fills([f]) == 0
        assert rs.count_fills() == 1


def test_trade_state_round_trips(tmp_path: pathlib.Path) -> None:
    with RuntimeStore(tmp_path / "r.sqlite3") as rs:
        assert rs.load_trade_state() == {}
        rs.save_trade_state({"account": {"cash": 1.0}, "gate": {"seen": ["a"]}})
        rs.save_trade_state({"account": {"cash": 2.0}, "gate": {"seen": ["a", "b"]}})
        got = rs.load_trade_state()
    assert got["account"]["cash"] == 2.0, "整体覆盖写，不是追加"
    assert got["gate"]["seen"] == ["a", "b"]


def test_meta_lists_continuous_symbols_flagged(tmp_path: pathlib.Path) -> None:
    """主连是给回测和看图用的。把它挡在下拉框外面，等于自己拼出来的连续序列
    在面板上根本选不到 —— 排除主连是「预警/下单」路径的事（tradable() 仍排除）。"""
    from sigdesk.core.registry import load_registry

    reg = load_registry(pathlib.Path("config"))
    state = ServiceState(
        runtime=RuntimeStore(tmp_path / "r.sqlite3"), data_root=tmp_path, registry=reg
    )
    body = TestClient(create_app(state)).get("/api/meta").json()
    by_uid = {s["uid"]: s for s in body["symbols"]}
    cont = [s for s in reg.symbols.values() if s.is_continuous]
    assert cont, "夹具前提：config 里应有主连标的"
    for sym in cont:
        assert sym.uid in by_uid, f"{sym.uid} 没出现在面板下拉框里"
        assert by_uid[sym.uid]["is_continuous"] is True, "前端要靠这个标出主连"
    assert all(not s.is_continuous for s in reg.tradable()), "tradable() 仍须排除主连"


def test_meta_offers_the_daily_timeframe(tmp_path: pathlib.Path) -> None:
    state = ServiceState(runtime=RuntimeStore(tmp_path / "r.sqlite3"), data_root=tmp_path)
    assert "1d" in TestClient(create_app(state)).get("/api/meta").json()["timeframes"]


def test_markers_include_fills_bucketed_the_same_way(
    wired: tuple[TestClient, ServiceState],
) -> None:
    """成交点与信号点走**同一套分桶规则**（服务端算，前端只画）。
    分家了就会出现"箭头在这根、成交在那根"的错位。"""
    client, state = wired
    from sigdesk.trade.model import Fill, FillKind, Side

    bars = read_range_helper(state)
    assert bars, "夹具前提：要有已落盘的 bar"
    ts = bars[len(bars) // 2].close_ts
    state.runtime.append_fills([
        Fill(signal_key="k", symbol=BTC, side=Side.BUY, qty=0.1, price=77000.0,
             ts=ts, kind=FillKind.ENTRY),
    ])
    body = client.get(f"/api/markers?symbol={BTC}&timeframe=1m").json()
    assert len(body["fills"]) == 1
    f = body["fills"][0]
    assert f["kind"] == "entry" and f["price"] == 77000.0
    assert f["bucket_ts"] in {b.close_ts for b in bars}, "成交落到了不存在的 bar 上"


def read_range_helper(state: ServiceState) -> list[Bar]:
    from sigdesk.store.parquet_io import read_range

    return read_range(state.data_root, BTC, Timeframe.M1, 0, 2**31)


def test_intraday_uses_the_last_trading_day_and_applies_multiplier() -> None:
    """分时的均价 = 累计成交额 / (累计成交量 × **合约乘数**)。

    乘数不能漏：rb 每手 10 吨，money/volume 得到的是 31320 而不是 3132 ——
    漏了乘数，均价线会画在价格线十倍高的地方，图直接废掉。
    """
    from sigdesk.web.intraday import build_intraday

    def bar(day: str, ts: int, close: float, vol: float, money: float) -> Bar:
        return Bar("CN.SHFE.rb2610", Timeframe.M1, ts - 60, ts, close, close, close,
                   close, vol, money=money, trading_day=day)

    bars = [
        bar("2026-08-27", 100, 3000.0, 1.0, 30000.0),   # 上一个交易日，不该进来
        bar("2026-08-28", 200, 3100.0, 10.0, 310000.0),
        bar("2026-08-28", 260, 3200.0, 10.0, 320000.0),
    ]
    day, pts = build_intraday(bars, multiplier=10.0)
    assert day == "2026-08-28"
    assert [p.price for p in pts] == [3100.0, 3200.0]
    assert pts[0].avg == pytest.approx(3100.0)
    assert pts[1].avg == pytest.approx(3150.0), "累计均价，不是逐根均价"


def test_intraday_avg_is_none_when_amount_is_missing() -> None:
    """成交额缺失时均价**算不出来** —— 返回 None，不用收盘价冒充（ADR-0006）。"""
    from sigdesk.web.intraday import build_intraday

    bars = [Bar("X", Timeframe.M1, 0, 60, 1.0, 1.0, 1.0, 1.0, 5.0, money=0.0)]
    _, pts = build_intraday(bars)
    assert pts[0].avg is None


def test_intraday_endpoint_reports_the_day_it_drew(
    wired: tuple[TestClient, ServiceState],
) -> None:
    """数据可能是陈的（进程没跑、周末）。那时画出上一个交易日、并把日期标出来，
    比画一张空图诚实得多 —— 所以日期必须回给前端。"""
    client, _ = wired
    body = client.get(f"/api/intraday?symbol={BTC}").json()
    assert body["points"], "夹具里有 1m 数据，分时不该为空"
    assert body["trading_day"], "必须如实报出画的是哪一天"
    assert all(p["ts"] for p in body["points"])


def test_meta_flags_symbols_no_rule_is_watching(tmp_path: pathlib.Path) -> None:
    """下拉框列全部标的（为了能看主连），但 watch.py 只采集**规则 universe 里**的标的。
    列表里有、却没人盯的，选中就是一张空图 —— 不标出来，用户只会以为"期货连不上"。
    （真发生过：IF2609/m2701/cu2610 从没被任何规则覆盖，所以一根 bar 都没有。）
    """
    from sigdesk.core.registry import load_registry
    from sigdesk.rules.loader import load_rules

    reg = load_registry(pathlib.Path("config"))
    rules = load_rules(pathlib.Path("config/rules"))
    state = ServiceState(
        runtime=RuntimeStore(tmp_path / "r.sqlite3"), data_root=tmp_path,
        registry=reg, rules=rules,
    )
    body = TestClient(create_app(state)).get("/api/meta").json()
    by_uid = {s["uid"]: s for s in body["symbols"]}
    covered = {uid for r in rules for uid in r.universe}
    assert covered, "夹具前提：规则里得有 universe"
    for uid, s in by_uid.items():
        assert s["watched"] is (uid in covered), uid
    assert any(not s["watched"] for s in by_uid.values()), "夹具前提：应有没被盯的标的"


def test_moving_averages_reuse_the_engine_indicators() -> None:
    """**图上画的均线必须和规则引擎看到的是同一个数。**
    前端自己拿 close 重算，口径一偏（ADR-0006：EMA 用 SMA 播种、预热返回 None）
    就会出现「图上明明上穿了、规则却没触发」—— 那类问题查起来极其费劲。
    """
    from sigdesk.indicators.series import EMA, SMA
    from sigdesk.web.overlay import moving_averages, parse_spec

    bars = [
        Bar("X", Timeframe.M1, i * 60, (i + 1) * 60, 100.0 + i, 101.0 + i, 99.0 + i,
            100.0 + i, 1.0)
        for i in range(30)
    ]
    got = {m.label: m.values for m in moving_averages(bars, "5,ema10")}
    sma, ema = SMA(5), EMA(10)
    assert got["SMA5"] == [sma.update(b.close) for b in bars]
    assert got["EMA10"] == [ema.update(b.close) for b in bars]
    assert got["SMA5"][:4] == [None] * 4, "预热期是 None 不是 0（ADR-0006）"
    assert parse_spec("5,10,ema20,junk,0,9999") == [("sma", 5), ("sma", 10), ("ema", 20)]


def test_bars_endpoint_computes_ma_on_the_full_series(
    wired: tuple[TestClient, ServiceState],
) -> None:
    """均线要在**完整序列**上算再截取。只用截出来的那段算 MA60，
    前 59 根会是 None，图上左边缺一截。"""
    client, _ = wired
    body = client.get(f"/api/bars?symbol={BTC}&timeframe=1m&limit=5&ma=3").json()
    assert len(body["bars"]) == 5
    line = body["ma"][0]
    assert line["label"] == "SMA3" and len(line["values"]) == 5
    assert all(v is not None for v in line["values"]), "截出来的这 5 根应已过预热期"


# ---------------------------------------------------------------- 预警组


def test_watchlist_groups_by_market(wired: tuple[TestClient, ServiceState]) -> None:
    """每个市场一组独立的九格。

    不分开的话，一条加密信号会把你钉着的螺纹挤掉 —— 而这两个市场的作息
    完全不同（期货有夜盘和休市，加密 7×24），混在一起扫没有意义。
    """
    client, _ = wired
    d = client.get("/api/watchlist").json()
    assert [m["key"] for m in d["markets"]] == ["CN", "CRYPTO"]
    assert d["slots"] == 9
    crypto = next(m for m in d["markets"] if m["key"] == "CRYPTO")
    assert crypto["entries"], "夹具里的信号都是加密的，这组不该是空的"
    assert all(e["symbol"].startswith("CRYPTO.") for e in crypto["entries"])
    cn = next(m for m in d["markets"] if m["key"] == "CN")
    assert cn["entries"] == [], "没有期货信号，期货组就该是空的"


def test_watchlist_keeps_one_slot_per_symbol(
    wired: tuple[TestClient, ServiceState],
) -> None:
    """夹具里同一个标的触发了很多条，组里仍然只占一格 ——
    否则九格会被一个躁动的品种吃光。"""
    client, _ = wired
    d = client.get("/api/watchlist").json()
    for m in d["markets"]:
        uids = [e["symbol"] for e in m["entries"]]
        assert len(uids) == len(set(uids)), uids


def test_pinning_puts_a_symbol_first_and_keeps_it(
    wired: tuple[TestClient, ServiceState],
) -> None:
    """钉住 = 人工判断「还需要观察」。钉住的排最前，且不会被新信号挤掉。"""
    client, _ = wired
    assert client.post("/api/watchlist/pin", json={"symbol": BTC}).status_code == 200
    entries = _crypto(client)
    assert entries[0]["symbol"] == BTC
    assert entries[0]["pinned"] is True

    assert client.delete(f"/api/watchlist/pin?symbol={BTC}").status_code == 200
    assert all(e["pinned"] is False for e in _crypto(client))


def test_pinning_an_unregistered_symbol_is_refused(
    wired: tuple[TestClient, ServiceState],
) -> None:
    """钉一个不存在的标的会得到一个永远空着的格子，而且是静默的空。宁可拒绝。

    校验要有 registry 才做得了 —— 独立只读模式（registry 为 None）拿不到
    合约表，那时只能放行，这是有意的：宁可让只读模式少一道校验，
    也不要因为拿不到合约表就把功能整个关掉。
    """
    client, state = wired
    state.registry = load_registry(pathlib.Path(__file__).resolve().parents[1] / "config")
    r = client.post("/api/watchlist/pin", json={"symbol": "CN.SHFE.nope"})
    assert r.status_code == 404
    assert "symbols.yaml" in r.json()["detail"], "要告诉人去哪儿补"

    ok = client.post("/api/watchlist/pin", json={"symbol": "CN.SHFE.rb2610"})
    assert ok.status_code == 200


def test_watchlist_says_whether_anything_is_watching_the_symbol(
    wired: tuple[TestClient, ServiceState],
) -> None:
    """**没有规则盯 ⇒ 盯盘进程不采集 ⇒ 图永远是空的。**

    这条必须如实传给前端，否则又是一个静默的空 —— 这个项目已经在
    「空状态不说明为什么」上栽过（用户当时以为是行情连不上）。
    """
    client, state = wired
    client.post("/api/watchlist/pin", json={"symbol": BTC})
    e = _crypto(client)[0]
    assert e["watched"] is False, "夹具的 state.rules 是空的，所以没有规则盯它"

    state.rules = [load_rule(RULE)]
    assert _crypto(client)[0]["watched"] is True


def test_pinning_is_refused_when_not_bound_to_loopback(
    wired: tuple[TestClient, ServiceState],
) -> None:
    """钉住会让盯盘进程多采集一个标的（对外请求、耗行情配额），
    而面板没有鉴权。绑到 0.0.0.0 时必须拒绝。"""
    client, state = wired
    state.local_only = False
    r = client.post("/api/watchlist/pin", json={"symbol": BTC})
    assert r.status_code == 403
    assert "SSH" in r.json()["detail"], "拒绝时要给出可行的替代做法"
    assert client.get("/api/watchlist").status_code == 200, "只读仍然可用"


def _crypto(client: TestClient) -> list[dict[str, Any]]:
    d = client.get("/api/watchlist").json()
    return next(m for m in d["markets"] if m["key"] == "CRYPTO")["entries"]
