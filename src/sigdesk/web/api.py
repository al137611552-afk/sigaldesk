"""只读 Web API（FastAPI）。ARCHITECTURE §7 的 P0：信号流 / K线回看 / 运行健康。

**只读**：这一版没有任何写端点。规则 CRUD 是 P1，可视化编辑器是 P2。
面板能做的事就是看，看不坏任何东西。

两种运行模式共用同一套端点：

- **同进程**（`scripts/watch.py --web`）：引擎在跑，健康与 SSE 有真数据。
- **独立只读**（`scripts/serve.py`）：只连 SQLite 与 Parquet，看历史信号与统计。
  此时健康快照是空的 —— 面板会显示"未接入实时引擎"，而不是假装一切正常。
"""

from __future__ import annotations

import asyncio
import bisect
import datetime as dt
import json
import pathlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..core.models import Bar, Timeframe
from ..core.registry import Registry
from ..rules.loader import load_rules
from ..rules.model import Rule, Signal
from ..rules.store import RuleStore, RuleStoreError, parse_source
from ..rules.trial import run_trial
from ..stats.outcome import OutcomeParams, evaluate_all
from ..stats.report import build_report
from ..store.parquet_io import latest_partition, read_range
from ..store.runtime_store import RuntimeStore
from .health import HealthMonitor
from .intraday import build_intraday
from .markers import collapse, pair_trades
from .overlay import moving_averages
from .watchlist import SLOTS, build_group, latest_by_symbol

STATIC_DIR = pathlib.Path(__file__).parent / "static"
MAX_BARS = 5000


class SignalBroadcaster:
    """把新信号广播给所有 SSE 订阅者。

    每个订阅者一个**有界**队列：面板卡住或网络慢时丢它自己的旧消息，
    绝不能反压到行情处理线程上 —— 一个开着不管的浏览器标签页不该拖垮盯盘。
    """

    def __init__(self, maxsize: int = 100) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._maxsize = maxsize
        self.dropped = 0

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    def publish(self, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        for q in list(self._subscribers):
            try:
                q.put_nowait(text)
            except asyncio.QueueFull:
                self.dropped += 1

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)


@dataclass(slots=True)
class ServiceState:
    """API 要用到的一切。引擎不在时相关端点如实返回"未接入"，不假装。"""

    runtime: RuntimeStore
    data_root: pathlib.Path
    registry: Registry | None = None
    rules: list[Rule] = field(default_factory=list)
    health: HealthMonitor | None = None
    # 只在同进程模式下有；独立只读模式为 None，端点会如实说"未接入实时引擎"
    engine: Any = None
    desk: Any = None  # 纸上交易台；未启用或独立只读模式为 None
    broadcaster: SignalBroadcaster = field(default_factory=SignalBroadcaster)
    live: bool = False  # 是否与引擎同进程
    rules_dir: pathlib.Path | None = None
    # 规则写端点（FR-5.3）。**默认关闭** —— 面板本身没有鉴权，
    # 部署到公网 VPS 时不该顺手带上写能力。由 serve.py --allow-edit 打开。
    edit_enabled: bool = False
    # 面板是否绑在回环地址上。**钉住/取消钉住按它放行** —— 钉住会让盯盘进程
    # 多采集一个标的（对外请求、耗行情配额），所以它是写操作；但把它关在
    # --allow-edit 后面等于这个功能没法日常用。按绑定地址放行是折中：
    # 默认 127.0.0.1 开箱可用，--host 0.0.0.0 暴露到网上时自动拒绝。
    local_only: bool = True

    def now_ts(self) -> int:
        return int(dt.datetime.now(dt.UTC).timestamp())

    def require_local(self) -> None:
        """钉住类端点的前置检查。见 ``local_only``。"""
        if not self.local_only:
            raise HTTPException(
                403,
                "面板绑在非回环地址上，已禁用钉住。钉住会让盯盘进程多采集一个标的，"
                "而面板没有鉴权。要远程用请开 SSH 隧道："
                "ssh -L 8000:127.0.0.1:8000 <host>",
            )

    def require_edit(self) -> None:
        """规则编辑相关端点的前置检查。写端点与试算都要过。

        试算也算在内：它不改任何东西，但会按请求读任意标的的全量历史并同步跑引擎，
        既是 CPU 放大器，也会跟盯盘抢资源。跟写端点同一道门最省心。
        """
        if not self.edit_enabled:
            raise HTTPException(
                403,
                "规则编辑未开启。面板没有鉴权，写端点默认关闭；"
                "确认只在可信网络内访问后，用 `serve.py --allow-edit` 启动。",
            )
        if self.live:
            raise HTTPException(
                409,
                "盯盘进程（watch.py --web）里不允许改规则：引擎的状态机、TTL 与去重表"
                "都绑在当前这批规则上，热替换会静默丢掉已布防的链路。"
                "请用只读面板（serve.py --allow-edit）编辑，改完重启盯盘进程生效。",
            )

    def rule_store(self) -> RuleStore:
        self.require_edit()
        if self.rules_dir is None:
            raise HTTPException(500, "未配置规则目录")
        return RuleStore(self.rules_dir)

    def reload_rules(self) -> None:
        if self.rules_dir is not None:
            self.rules = load_rules(self.rules_dir)


class RuleSource(BaseModel):
    source: str = Field(..., description="规则 YAML 全文")


class TrialRequest(BaseModel):
    """试算请求。口径参数与 /api/stats 一致（ADR-0008），默认值也保持一致。"""

    source: str
    symbols: list[str] | None = None  # 不给就用规则自己的 universe
    start_ts: int = 0
    end_ts: int = 2**31
    horizon_bars: int = Field(20, ge=1, le=500)
    stop_pct: float = Field(0.005, gt=0)
    target_pct: float = Field(0.010, gt=0)
    cost_bps: float = Field(0.0, ge=0)
    entry_on_next_open: bool = True


# 开发机 2 核 4G。试算是同步阻塞的，喂太多会把面板拖死 —— 宁可明确拒绝。
MAX_TRIAL_BARS = 400_000


def _bar_dict(bar: Bar) -> dict[str, Any]:
    return {
        "open_ts": bar.open_ts,
        "close_ts": bar.close_ts,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "trading_day": bar.trading_day,
    }


def create_app(state: ServiceState) -> FastAPI:
    app = FastAPI(title="Signal Desk", version="0.1.0", docs_url="/api/docs")

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        """面板启动时拉一次：有哪些规则、哪些标的、是不是接了实时引擎。"""
        # **列全部标的，不只是 tradable()**：主连是给回测和看图用的，
        # 把它挡在下拉框外面，等于自己拼出来的连续序列在面板上根本选不到。
        # 排除主连是「预警/下单」路径的事（tradable() 仍然排除），不是看图的事。
        #
        # 但列全了就要说清楚**哪些其实没在采集**：watch.py 只采集出现在某条规则
        # universe 里的标的。列表里有、却从没被任何规则盯过的，选中就是一张空图 ——
        # 不标出来的话，用户只会以为"期货连不上"（真发生过）。
        # `watched` 只回答"有没有规则盯它"，**不回答"本地有没有数据"** —— 两件事。
        # 冷启动时最误导人的正是这个差：规则盯着 BTC/ETH（watched=True），
        # 但行情还没接入、一根 bar 都没落盘，下拉框里却干干净净什么标记都没有，
        # 选中就是空图，看着像面板坏了（用户实际撞上了）。所以再给一个 last_day。
        watched = {uid for r in state.rules for uid in r.universe}
        symbols = []
        if state.registry:
            symbols = [
                {
                    "uid": s.uid,
                    "market": str(s.market),
                    "code": s.code,
                    "exchange": s.exchange,
                    "price_tick": s.price_tick,
                    "is_continuous": s.is_continuous,  # 前端要标出来，免得与可交易合约混淆
                    "watched": s.uid in watched,  # 有规则盯它 ⇒ 盯盘进程会采它的行情
                    # 本地最后一个分区日；None = 一根 bar 都没有。只列目录名，不读文件。
                    "last_day": latest_partition(state.data_root, s.uid, Timeframe.M1),
                }
                for s in sorted(state.registry.symbols.values(), key=lambda s: s.uid)
            ]
        return {
            "live": state.live,
            "now_ts": state.now_ts(),
            "symbols": symbols,
            "timeframes": [t.value for t in Timeframe],
            "rules": [
                {
                    "id": r.id,
                    "description": r.description,
                    "enabled": r.enabled,
                    "universe": list(r.universe),
                    "timeframe": r.timeframe.value,
                    "levels": [
                        {"role": c.role, "on": c.on.value, "mode": str(c.mode),
                         "within": c.within, "when": c.when.source}
                        for c in r.conditions
                    ],
                    "direction": str(r.emit.direction),
                    "ttl_bars": r.emit.ttl_bars,
                    "cooldown_s": r.emit.cooldown_s,
                }
                for r in state.rules
            ],
        }

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        if state.health is None:
            return {
                "live": False,
                "healthy": None,
                "note": "未接入实时引擎（独立只读模式）",
                "now_ts": state.now_ts(),
                "feeds": [],
                "symbols": [],
            }
        snap = state.health.snapshot(state.now_ts())
        snap["live"] = state.live
        snap["sse_subscribers"] = state.broadcaster.subscribers
        snap["sse_dropped"] = state.broadcaster.dropped
        return snap

    @app.get("/api/signals")
    def signals(
        rule_id: str | None = None,
        symbol: str | None = None,
        limit: int = Query(200, ge=1, le=5000),
    ) -> dict[str, Any]:
        rows = state.runtime.signals(rule_id=rule_id, symbol=symbol)
        return {"total": len(rows), "signals": rows[-limit:]}

    @app.get("/api/bars")
    def bars(
        symbol: str,
        timeframe: str = "1m",
        start_ts: int = 0,
        end_ts: int = 2**31,
        limit: int = Query(1000, ge=1, le=MAX_BARS),
        ma: str = "",
        vma: str = "",
    ) -> dict[str, Any]:
        try:
            tf = Timeframe(timeframe)
        except ValueError:
            raise HTTPException(400, f"未知周期 {timeframe}") from None
        series = read_range(state.data_root, symbol, tf, start_ts, end_ts)
        shown = series[-limit:]
        # 均线要在**完整序列**上算再截取，不能只用截出来的那段 ——
        # 只喂最后 220 根去算 MA60，前 59 根会是 None，图上左边缺一截。
        lines = moving_averages(series, ma) if ma else []
        vlines = moving_averages(series, vma, source="volume") if vma else []
        cut = len(series) - len(shown)
        clip = [{**m.as_dict(), "values": m.values[cut:]} for m in (*lines, *vlines)]
        return {
            "symbol": symbol,
            "timeframe": tf.value,
            "total": len(series),
            "bars": [_bar_dict(b) for b in shown],
            "ma": [m for m in clip if m["source"] == "close"],
            "vma": [m for m in clip if m["source"] == "volume"],
        }

    @app.get("/api/intraday")
    def intraday(symbol: str) -> dict[str, Any]:
        """分时图：当日价格线 + 均价线（九宫格第一格）。

        分时不是一个周期，是当日 1m 的一种画法，所以不占 `Timeframe` 枚举。
        均价要用合约乘数、"当日"要看 trading_day —— 两样都只有服务端有，故算在这里。
        """
        mult = 1.0
        if state.registry is not None:
            try:
                mult = state.registry.symbol(symbol).multiplier
            except KeyError:
                mult = 1.0
        bars = read_range(state.data_root, symbol, Timeframe.M1, 0, 2**31)[-MAX_BARS:]
        day, points = build_intraday(bars, multiplier=mult)
        return {
            "symbol": symbol,
            "trading_day": day or None,
            "multiplier": mult,
            "points": [p.as_dict() for p in points],
        }

    @app.get("/api/trade")
    def trade(limit: int = Query(200, ge=1, le=5000)) -> dict[str, Any]:
        """纸上账户与成交记录。

        ``summary`` 是**当下**的账户状态（内存里的持仓与浮盈），只在同进程模式下有；
        ``fills`` 是落盘的成交流水，独立只读模式下也能看 —— 复盘不需要引擎在跑。
        """
        fills = state.runtime.fills(limit=limit)
        body: dict[str, Any] = {
            "live": state.desk is not None,
            "fills": fills,
            "total_fills": state.runtime.count_fills(),
        }
        if state.desk is None:
            from ..trade.desk import TradeDesk
            body["summary"] = TradeDesk.summary_from_snapshot(state.runtime.load_trade_state())
            body["note"] = (
                "未接入实时交易台（独立只读模式，或 config/trading.yaml 里 enabled: false）。"
                "下面是落盘的成交流水；账户数字取自**最后一次运行**的快照，不是当下状态。"
            )
        else:
            body["summary"] = state.desk.summary()
        return body

    @app.get("/api/markers")
    def markers(symbol: str, timeframe: str = "1m") -> dict[str, Any]:
        """K 线上的信号标注点。

        **算在服务端，前端只负责画**。M3 验收要求"图上标注的信号点与 SignalStore 记录
        逐条对得上"，放在前端 JS 里这条就没法测；放这里就是一个可单测的服务端不变量。

        分桶用与引擎**同一套墙钟规则**（`ceil(ts/period)*period`），不做就近吸附 ——
        吸附会让标注落在一根它并不属于的 bar 上，图看着好看，结论是错的。
        落不到任何一根上的信号如实计入 ``dropped``，不悄悄丢弃。

        日线没有墙钟桶边界（交易日长度不固定），改成落到"**收盘时刻不早于该信号**
        的第一根日线"上 —— 这与 DayBuilder 的归属完全一致，仍然不是就近吸附。
        """
        try:
            tf = Timeframe(timeframe)
        except ValueError:
            raise HTTPException(400, f"未知周期 {timeframe}") from None
        rows = state.runtime.signals(symbol=symbol)
        series = read_range(state.data_root, symbol, tf, 0, 2**31)
        stamps = {b.close_ts for b in series}
        closes = sorted(stamps)
        period = tf.seconds

        def bucket_of(ts: int) -> int:
            """信号/成交时刻 -> 它属于哪根 bar。与引擎同一套规则，不做就近吸附。"""
            if period:
                return -(-ts // period) * period
            i = bisect.bisect_left(closes, ts)  # 日线：收盘不早于该时刻的第一根
            return closes[i] if i < len(closes) else -1

        placed, dropped = [], []
        for r in rows:
            fired = int(r["fired_at"])
            bucket = bucket_of(fired)
            if bucket not in stamps:
                dropped.append({"dedup_key": r["dedup_key"], "fired_at": r["fired_at"]})
                continue
            placed.append({**r, "bucket_ts": bucket})
        # 同一根 bar 上的多条信号折成一枚「×N」。链路长度参与选代表，所以要把
        # 当前规则的段数喂进去；规则已被删掉的老信号按 1 段算（拿不到就别猜）。
        chain_len = {rule.id: len(rule.conditions) for rule in state.rules}
        out = collapse(placed, chain_len)

        # 成交点。信号是"我认为该进场"，成交是"实际以什么价成交了" —— 两件事，
        # 图上必须分开画。以前只画信号，所以"看不到成交的具体价格点"。
        fills = []
        for f in state.runtime.fills(symbol=symbol, limit=2000):
            bucket = bucket_of(int(f["ts"]))
            if bucket not in stamps:
                continue
            fills.append({
                "bucket_ts": bucket,
                "ts": int(f["ts"]),
                "kind": str(f["kind"]),
                "side": str(f["side"]),
                "price": float(f["price"]),
                "qty": float(f["qty"]),
                "realized": float(f["realized"] or 0.0),
                "signal_key": f["signal_key"],
            })
        fills.sort(key=lambda m: (m["bucket_ts"], m["ts"]))

        return {
            "symbol": symbol,
            "timeframe": tf.value,
            "signals": len(rows),
            "markers": out,
            "fills": fills,
            "trades": pair_trades(fills, _notional(state, symbol, fills)),
            "dropped": dropped,
        }

    @app.get("/api/watchlist")
    def watchlist() -> dict[str, Any]:
        """预警组：每个市场九个格子里放哪些标的。

        **组不是一份维护出来的名单，是算出来的**（见 web/watchlist.py）：
        钉住的 ∪ 最近触发的，取前九。只有「钉住」落库，其余每次重算 ——
        没有淘汰逻辑，也就没有淘汰 bug。

        一次把所有市场都返回：市场只有两个、每个至多九格，省得前端为了
        tab 上的未读数再逐个市场拉一遍。「未读」是每个人自己的状态，
        服务端只给最新那条信号的 ``dedup_key``，由前端跟本地已读集合比对。
        """
        pinned = state.runtime.pins()
        rows = state.runtime.signals()
        latest = latest_by_symbol(rows)
        watched = {u for rule in state.rules for u in rule.universe}

        out: dict[str, Any] = {"slots": SLOTS, "markets": [], "local_only": state.local_only}
        for market, label in (("CN", "期货"), ("CRYPTO", "加密")):
            # uid 的第一段就是市场（CN.SHFE.rb2610 / CRYPTO.OKX.BTCUSDT.PERP）。
            # 每个市场一组独立的九格：不然一条加密信号会把你钉着的螺纹挤掉，
            # 而这两个市场的作息完全不同（期货有夜盘和休市，加密 7×24）。
            def of(uid: str, m: str = market) -> bool:
                return uid.split(".")[0] == m

            mine = [u for u in pinned if of(u)]
            group = build_group(mine, [r for r in latest if of(str(r["symbol"]))], SLOTS)
            entries = []
            for e in group:
                uid = e["symbol"]
                sym = state.registry.symbols.get(uid) if state.registry else None
                entries.append({
                    **e,
                    # 没注册的标的（钉住之后从 symbols.yaml 里删了）如实标出来，
                    # 而不是让格子静默地空着。短名由前端的 shortSym() 统一算，
                    # 服务端再算一套迟早两边显示不一致。
                    "known": sym is not None,
                    # 没有规则盯 ⇒ 盯盘进程不采集它 ⇒ 图会一直是空的。
                    # 这条必须传给前端说清楚，不然就是又一个静默的空。
                    "watched": uid in watched,
                })
            out["markets"].append({
                "key": market, "label": label,
                "pinned_over_slots": max(0, len(mine) - SLOTS),
                "entries": entries,
            })
        return out

    @app.post("/api/watchlist/pin")
    def pin(body: dict[str, Any]) -> dict[str, Any]:
        """钉住：人工判断「还需要观察」的唯一表达。钉住的不会被新信号挤掉。"""
        state.require_local()
        uid = str(body.get("symbol") or "").strip()
        if not uid:
            raise HTTPException(400, "缺少 symbol")
        if state.registry is not None and uid not in state.registry.symbols:
            raise HTTPException(404, f"未注册的标的 {uid}；请先补进 config/symbols.yaml")
        added = state.runtime.pin(uid, state.now_ts())
        watched = any(uid in rule.universe for rule in state.rules)
        return {"symbol": uid, "pinned": True, "added": added, "watched": watched}

    @app.delete("/api/watchlist/pin")
    def unpin(symbol: str) -> dict[str, Any]:
        state.require_local()
        return {"symbol": symbol, "pinned": False, "removed": state.runtime.unpin(symbol)}

    @app.get("/api/stats")
    def stats(
        rule_id: str | None = None,
        symbol: str | None = None,
        horizon_bars: int = Query(20, ge=1, le=500),
        stop_pct: float = Query(0.005, gt=0),
        target_pct: float = Query(0.010, gt=0),
        cost_bps: float = Query(0.0, ge=0),
        entry_on_next_open: bool = True,
    ) -> dict[str, Any]:
        """信号质量报告。口径**由查询参数决定并原样带回** ——
        一份不写明口径的胜率没有意义。"""
        rows = state.runtime.signals(rule_id=rule_id, symbol=symbol)
        sigs = [_row_to_signal(r) for r in rows]
        needed = {s.symbol: s.timeframe for s in sigs}
        bars_by_symbol = {
            sym: read_range(state.data_root, sym, tf, 0, 2**31) for sym, tf in needed.items()
        }
        params = OutcomeParams(
            horizon_bars=horizon_bars, stop_pct=stop_pct, target_pct=target_pct,
            cost_bps=cost_bps, entry_on_next_open=entry_on_next_open,
        )
        outcomes = evaluate_all(sigs, bars_by_symbol, params)
        report = build_report(
            outcomes,
            {
                "horizon_bars": horizon_bars, "stop_pct": stop_pct, "target_pct": target_pct,
                "cost_bps": cost_bps, "entry_on_next_open": entry_on_next_open,
            },
        )
        payload = report.as_dict()
        payload["outcomes"] = [o.as_dict() for o in outcomes]
        return payload

    # ---- 规则编辑（FR-5.3）。默认关闭，见 ServiceState.rule_store() ----------

    def _rule_summary(rule: Rule) -> dict[str, Any]:
        return {
            "id": rule.id,
            "description": rule.description,
            "enabled": rule.enabled,
            "universe": list(rule.universe),
            "timeframe": rule.timeframe.value,
            "levels": [
                {"role": c.role, "on": c.on.value, "mode": str(c.mode), "when": c.when.source}
                for c in rule.conditions
            ],
            "direction": str(rule.emit.direction),
        }

    @app.get("/api/rules")
    def list_rules() -> dict[str, Any]:
        """规则清单。读是公开的（面板本来就展示规则），写才需要 --allow-edit。"""
        return {
            "editable": state.edit_enabled and not state.live,
            "live": state.live,
            "rules": [_rule_summary(r) for r in state.rules],
        }

    @app.get("/api/rules/{rule_id}/source")
    def rule_source(rule_id: str) -> dict[str, Any]:
        if state.rules_dir is None:
            raise HTTPException(500, "未配置规则目录")
        try:
            return {"id": rule_id, "source": RuleStore(state.rules_dir).read_source(rule_id)}
        except RuleStoreError as e:
            raise HTTPException(404, str(e)) from None

    @app.post("/api/rules/validate")
    def validate_rule(body: RuleSource) -> dict[str, Any]:
        """只校验不落盘。走的是与真正加载**完全同一条**编译路径 ——
        校验通过却启动失败，比不校验还糟。"""
        try:
            rule, _ = parse_source(body.source)
        except RuleStoreError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "rule": _rule_summary(rule)}

    @app.post("/api/rules", status_code=201)
    def create_rule(body: RuleSource) -> dict[str, Any]:
        store = state.rule_store()
        try:
            rule = store.save(body.source, create=True)
            store.validate_all()
        except RuleStoreError as e:
            raise HTTPException(400, str(e)) from None
        state.reload_rules()
        return {"id": rule.id, "rule": _rule_summary(rule)}

    @app.put("/api/rules/{rule_id}")
    def update_rule(rule_id: str, body: RuleSource) -> dict[str, Any]:
        store = state.rule_store()
        try:
            rule = store.save(body.source, expect_id=rule_id)
            store.validate_all()
        except RuleStoreError as e:
            raise HTTPException(400, str(e)) from None
        state.reload_rules()
        return {"id": rule.id, "rule": _rule_summary(rule)}

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: str) -> dict[str, Any]:
        store = state.rule_store()
        try:
            archived = store.delete(rule_id)
        except RuleStoreError as e:
            raise HTTPException(404, str(e)) from None
        state.reload_rules()
        # 明确告诉人东西去哪了：删除不可逆才需要确认，这里是可逆的。
        # **一律用正斜杠**（as_posix）：这是 JSON API 的返回值，不该跟着操作系统变 ——
        # Windows 上返回 `rules\_trash\x.yaml` 会让任何按路径断言/展示的调用方在
        # 跨平台时炸掉（实测在 Windows 验收时就炸了一条测试）。
        return {
            "id": rule_id,
            "archived_to": archived.relative_to(store.directory.parent).as_posix(),
        }

    @app.post("/api/rules/trial")
    def trial_rule(body: TrialRequest) -> dict[str, Any]:
        """历史试算：拿这条规则在已落盘的历史上跑一遍，看会在哪触发、值不值。

        用的是**同一个引擎**（ADR-0001），所以试算结果与实盘/回放逐条一致。
        试算不落盘、不推送、不下单，也不读写运行态。
        """
        state.require_edit()
        try:
            rule, _ = parse_source(body.source)
        except RuleStoreError as e:
            raise HTTPException(400, str(e)) from None

        symbols = body.symbols or list(rule.universe)
        if not symbols:
            raise HTTPException(400, "没有可试算的标的：规则的 universe 为空且未指定 symbols")
        bars_by_symbol = {
            uid: read_range(state.data_root, uid, Timeframe.M1, body.start_ts, body.end_ts)
            for uid in symbols
        }
        total = sum(len(v) for v in bars_by_symbol.values())
        if total > MAX_TRIAL_BARS:
            raise HTTPException(
                413,
                f"试算区间过大（{total} 根 1m，上限 {MAX_TRIAL_BARS}）。"
                f"请缩短 start_ts/end_ts 或减少标的。",
            )
        if total == 0:
            raise HTTPException(
                404,
                f"这些标的在该区间没有已落盘的 1m 数据：{', '.join(symbols)}。"
                f"请先用 scripts/backfill.py 回补。",
            )

        params = OutcomeParams(
            horizon_bars=body.horizon_bars, stop_pct=body.stop_pct,
            target_pct=body.target_pct, cost_bps=body.cost_bps,
            entry_on_next_open=body.entry_on_next_open,
        )
        result = run_trial(
            rule, bars_by_symbol, outcome_params=params,
            report_params={
                "horizon_bars": body.horizon_bars, "stop_pct": body.stop_pct,
                "target_pct": body.target_pct, "cost_bps": body.cost_bps,
                "entry_on_next_open": body.entry_on_next_open,
            },
        )
        payload = result.as_dict()
        payload["rule"] = _rule_summary(rule)
        payload["range"] = [body.start_ts, body.end_ts]
        return payload

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        """SSE 实时信号流。心跳兼作探活，浏览器断开后自动清理订阅。"""
        queue = state.broadcaster.subscribe()

        async def gen() -> AsyncIterator[str]:
            try:
                yield 'event: hello\ndata: {"ok":true}\n\n'
                while True:
                    try:
                        text = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield ": keepalive\n\n"  # 注释行，兼作探活
                        continue
                    yield f"event: signal\ndata: {text}\n\n"
            finally:
                state.broadcaster.unsubscribe(queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


def _notional(state: ServiceState, symbol: str, fills: list[dict[str, Any]]) -> dict[str, float]:
    """每笔交易的名义本金：开仓价 × 手数 × 合约乘数。盈亏百分比的分母。

    registry 不在（独立只读模式）就返回空表 —— 于是 ``pnl_pct`` 为 None、
    前端显示破折号。**别退化成乘数 1**：期货一手十吨，那样算出来的百分比
    看着像模像样，其实差一个量级，比不显示危险得多。
    """
    if state.registry is None:
        return {}
    try:
        mult = state.registry.symbol(symbol).multiplier or 1.0
    except KeyError:
        return {}
    out: dict[str, float] = {}
    for f in fills:
        if str(f["kind"]) != "entry":
            continue
        base = float(f["price"]) * float(f["qty"]) * mult
        if base > 0:
            out[str(f["signal_key"])] = base
    return out


def _row_to_signal(row: dict[str, Any]) -> Signal:
    from ..rules.model import Direction, Priority

    return Signal(
        rule_id=row["rule_id"],
        symbol=row["symbol"],
        direction=Direction(row["direction"]),
        timeframe=Timeframe(row["timeframe"]),
        fired_at=int(row["fired_at"]),
        trigger_price=float(row["trigger_price"]),
        dedup_key=row["dedup_key"],
        context=dict(row.get("context") or {}),
        role_bars=dict(row.get("role_bars") or {}),
        tentative=bool(row.get("tentative")),
        priority=Priority.coerce(row.get("priority", "normal")),
        trading_day=row.get("trading_day"),
    )


__all__ = ["MAX_BARS", "STATIC_DIR", "ServiceState", "SignalBroadcaster", "create_app"]
