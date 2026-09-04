"""多级别规则引擎。纯逻辑：喂 bar，吐 Signal，无网络无磁盘、**不读墙钟**。

## 求值时机（ARCHITECTURE §5.2）

一根 bar 到达时分两步，顺序不能反：

1. **记账**：任何周期的 bar 收盘 ⇒ 求值挂在该周期上的条件，把结果记进该条件的滚动日志。
   每个条件在每根自己周期的 bar 上**只求值一次**，指标缓存因此可以单向前进。
2. **判定**：只有**扳机周期**的 bar 收盘才跑状态机，读各条件日志的最新结果。

必须先把一批同时收盘的 bar 全部记完账再判定 —— 否则 15m(setup) 与 5m(trigger) 同刻收盘时，
扳机会读到**上一根** setup 的结果。所以入口是 ``on_bars(批)`` 而不是 ``on_bar(单根)``。

## 为什么 replay 与 live 能逐条一致（红线验收）

引擎的全部输入只有"按顺序到达的已收盘 bar"，全部时间取自 ``bar.close_ts``：
冷却比较 close_ts、TTL 数扳机 bar 根数、信号时间戳取 close_ts。
没有任何一处读系统时钟，所以只要 bar 序列相同，输出必然相同。
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.models import Bar, Timeframe
from ..patterns.context import EvalContext, IndicatorCache
from ..patterns.expr import CompiledExpr, ExprError
from ..patterns.values import scalar
from ..store.bar_store import BarStore
from .model import Mode, Rule, RuleCondition, Signal
from .state import Outcome, RuleState, StateMachine, Step

# 每个 (规则, 标的) 记住多少个已发去重键。够覆盖一天的分钟级信号，又不至于无限增长。
DEDUP_MEMORY = 512


# 条件判定的只读旁路。参数：(rule_id, symbol, role, timeframe, close_ts, raw, satisfied)
ConditionRecorder = Callable[[str, str, str, Timeframe, int, bool | None, bool | None], None]


@dataclass(slots=True)
class _ConditionLog:
    """一个条件在自己周期上的最近判定结果，以及最后记账到哪根 bar。"""

    results: deque[bool | None]
    last_ts: int = 0

    @staticmethod
    def for_condition(cond: RuleCondition) -> _ConditionLog:
        # window 要看最近 within 根；event 要看最近两根；state 只看最新一根
        depth = max(cond.within, 2)
        return _ConditionLog(results=deque(maxlen=depth))

    def book(self, value: bool | None, ts: int) -> bool:
        """记一次判定。同一根重复记账返回 False（不会污染 event 的边沿判定）。"""
        if ts <= self.last_ts:
            return False
        self.results.append(value)
        self.last_ts = ts
        return True

    def satisfied(self, cond: RuleCondition) -> bool | None:
        """按模式给出"此刻这一段算不算满足"。"""
        if not self.results:
            return None
        if cond.mode is Mode.STATE:
            return self.results[-1]
        if cond.mode is Mode.WINDOW:
            window = list(self.results)[-cond.within :]
            if any(v is True for v in window):
                return True
            return None if any(v is None for v in window) else False
        # event：只认 False -> True 这一个跳变。None 不算"上一根不成立"，
        # 否则预热期结束后的第一根会被误判成边沿。
        if len(self.results) < 2 or self.results[-1] is not True:
            return False
        return self.results[-2] is False


@dataclass(frozen=True, slots=True)
class _ViewSource:
    """跨级别引用的取数口。**复用同一个 BarView**，所以 as-of 截断位置完全一致 ——
    5m 那根收在 10:05 时，1h 侧看到的是 10:00 收盘那根，绝不会偷看正在走的 1h。

    指标缓存仍按 (symbol, timeframe) 分开，1h 的 EMA 只算一份，
    不管是被 1h 的条件用还是被 5m 的条件通过 at() 用。
    """

    view: Any
    engine: Any
    symbol: str

    def bars_at(self, timeframe: Timeframe) -> Sequence[Bar]:
        bars: Sequence[Bar] = self.view.bars(timeframe)
        if not bars and timeframe not in (*self.view_timeframes(), Timeframe.M1):
            raise ValueError(
                f"at('{timeframe.value}', ...) 取不到数据：BarStore 没有派生 {timeframe.value}。"
                f"这通常是规则加载路径没有把 Rule.required_timeframes 传给 BarStore。"
            )
        return bars

    def view_timeframes(self) -> tuple[Timeframe, ...]:
        return tuple(self.engine.store.timeframes)

    def cache_at(self, timeframe: Timeframe) -> IndicatorCache:
        cache: IndicatorCache = self.engine.cache_for(self.symbol, timeframe)
        return cache


@dataclass(slots=True)
class _Instance:
    """一个 (rule, symbol) 的全部运行态。可整体序列化到 SQLite（见 store.py）。"""

    state: RuleState
    logs: dict[str, _ConditionLog]
    seen: OrderedDict[str, None] = field(default_factory=OrderedDict)

    def remember(self, key: str) -> None:
        self.seen[key] = None
        while len(self.seen) > DEDUP_MEMORY:
            self.seen.popitem(last=False)


class RuleEngine:
    """把规则挂在 BarStore 上：每根收盘 bar 记账，扳机周期的 bar 触发判定。"""

    def __init__(
        self,
        rules: Sequence[Rule],
        store: BarStore,
        *,
        recorder: ConditionRecorder | None = None,
    ) -> None:
        """`recorder` 是一个**只读旁路**：每记一次条件判定就回调一次。

        为什么做成钩子而不是让调用方自己重算一遍条件：ADR-0007 明确拒绝第二条求值路径。
        "各级别条件在哪几段成立"必须与真正驱动链路的判定**同源**，
        否则面板画的带状图和引擎实际看到的会不一样，那比不画更糟。
        钩子只被喂结果，动不了任何状态。
        """
        self._rules = [r for r in rules if r.enabled]
        self._store = store
        self._recorder = recorder
        self._instances: dict[tuple[str, str], _Instance] = {}
        self._machines = {
            r.id: StateMachine(
                chain_len=len(r.conditions), ttl_bars=r.emit.ttl_bars, cooldown_s=r.emit.cooldown_s
            )
            for r in self._rules
        }
        # 指标缓存按 (symbol, timeframe) 共享：多条规则用同一个 ema(close,20) 只算一份
        self._caches: dict[tuple[str, Timeframe], IndicatorCache] = {}
        self.transitions: list[tuple[str, str, int, Outcome]] = []

    @property
    def rules(self) -> list[Rule]:
        return list(self._rules)

    @property
    def store(self) -> BarStore:
        return self._store

    def cache_for(self, symbol: str, timeframe: Timeframe) -> IndicatorCache:
        return self._caches.setdefault((symbol, timeframe), IndicatorCache())

    def instance(self, rule_id: str, symbol: str) -> _Instance:
        key = (rule_id, symbol)
        inst = self._instances.get(key)
        if inst is None:
            rule = next(r for r in self._rules if r.id == rule_id)
            inst = _Instance(
                state=RuleState(),
                logs={c.role: _ConditionLog.for_condition(c) for c in rule.conditions},
            )
            self._instances[key] = inst
        return inst

    def instances(self) -> dict[tuple[str, str], _Instance]:
        return self._instances

    # ------------------------------------------------------------ 快照与恢复

    def snapshot(self) -> list[dict[str, Any]]:
        """把全部运行态导成可 JSON 化的行。持久化层只管存取，不碰引擎内部结构。

        指标缓存**不在**快照里：它可以由 BarStore 的历史重新喂出来（首次用到时整段回放），
        存进去只会让快照又大又易腐。真正非存不可的是「状态机 + 条件日志 + 去重表」——
        这三样无法从行情重算出来。
        """
        return [
            {
                "rule_id": rule_id,
                "symbol": symbol,
                **inst.state.as_row(),
                "logs": {
                    role: {"results": list(log.results), "last_ts": log.last_ts}
                    for role, log in inst.logs.items()
                },
                "seen": list(inst.seen),
            }
            for (rule_id, symbol), inst in sorted(self._instances.items())
        ]

    def restore(self, rows: Iterable[dict[str, Any]]) -> int:
        """从快照恢复。只认当前已加载规则里存在的角色 —— 规则改过之后旧快照不该悄悄复活。"""
        by_id = {r.id: r for r in self._rules}
        restored = 0
        for row in rows:
            rule = by_id.get(str(row["rule_id"]))
            if rule is None:
                continue  # 规则已下线，丢弃它的旧状态
            inst = self.instance(rule.id, str(row["symbol"]))
            inst.state = RuleState.from_row(row)
            for cond in rule.conditions:
                saved = (row.get("logs") or {}).get(cond.role)
                if not saved:
                    continue
                log = inst.logs[cond.role]
                log.results.clear()
                log.results.extend(saved.get("results") or [])
                log.last_ts = int(saved.get("last_ts", 0))
            inst.seen.clear()
            for key in row.get("seen") or []:
                inst.remember(str(key))
            restored += 1
        return restored

    def cursor(self) -> dict[str, int]:
        """各标的已处理到的最新 bar close_ts。重启后据此决定要补喂哪些 bar。"""
        out: dict[str, int] = {}
        for (_, symbol), inst in self._instances.items():
            newest = max((log.last_ts for log in inst.logs.values()), default=0)
            if newest > out.get(symbol, 0):
                out[symbol] = newest
        return out

    # ------------------------------------------------------------ 入口

    def on_bars(self, bars: Sequence[Bar]) -> list[Signal]:
        """一批**同时收盘**的 bar（``BarStore.push`` 的返回值）。先全部记账，再统一判定。"""
        closed = [b for b in bars if b.closed]
        if not closed:
            return []
        for bar in closed:
            self._book(bar)
        out: list[Signal] = []
        for bar in closed:
            for rule in self._rules:
                if rule.timeframe is bar.timeframe and bar.symbol in rule.universe:
                    signal = self._decide(rule, bar)
                    if signal is not None:
                        out.append(signal)
        return out

    def on_bar(self, bar: Bar) -> list[Signal]:
        """单根入口。仅在确实只有一根时用；同刻多周期收盘请用 ``on_bars``。"""
        return self.on_bars([bar])

    def feed(self, bars: Iterable[Bar]) -> list[Signal]:
        """按顺序喂一串 bar（回放/回测用）。与实盘走同一条 ``on_bars`` 路径。"""
        return [s for bar in bars for s in self.on_bars([bar])]

    def resume(self, bars: Iterable[Bar]) -> list[Signal]:
        """重启后重放历史，返回**停机期间漏掉的**信号。

        每根都进 BarStore（重建高周期序列；指标会在首次用到时整段回放自行预热），
        但**只有游标之后的 bar 才交给判定**：

        - 已处理过的 bar 再跑一遍状态机会造成虚假的推进与回退，而且它们的信号早发过了；
        - 游标之后的 bar 是真正漏掉的，必须补判 —— 这就是"不丢报"。
        - "不重报"由持久化的去重表兜底：即使补判范围有重叠，同一去重键也只会发一次。

        调用前先 ``restore``，否则游标为空、整段历史都会被当成新数据判一遍。
        """
        cursor = self.cursor()
        out: list[Signal] = []
        for bar in bars:
            derived = self._store.push(bar)
            if bar.close_ts <= cursor.get(bar.symbol, 0):
                continue
            out.extend(self.on_bars(derived))
        return out

    def prime(self, bars: Iterable[Bar]) -> None:
        """用历史 bar 预热：**只记账，不判定** —— 不发信号、不动状态机、不占冷却与去重表。

        为什么需要：window 要凑够窗口、event 要有"上一根"、指标要喂饱。
        预热又绝不能真发信号 —— 那等于把历史行情当实时报一遍。
        """
        for bar in bars:
            if bar.closed:
                self._book(bar)

    # ------------------------------------------------------------ 记账与判定

    def _book(self, bar: Bar) -> None:
        for rule in self._rules:
            if bar.symbol not in rule.universe:
                continue
            for cond in rule.conditions:
                if cond.on is not bar.timeframe:
                    continue
                inst = self.instance(rule.id, bar.symbol)
                log = inst.logs[cond.role]
                if log.last_ts >= bar.close_ts:
                    continue  # 这一根已经记过；重复投递不得污染 event 的边沿判定
                ctx = self._context(bar.symbol, cond.on, bar.close_ts)
                raw = cond.when.evaluate(ctx)
                if log.book(raw, bar.close_ts) and self._recorder is not None:
                    # raw = 表达式这一根自己的真值；satisfied = 套上 mode 之后链路看到的东西。
                    # 两者不同：event 只认跳变、window 看最近 within 根，
                    # "表达式成立但链路不认"正是"为什么没触发"最常见的答案。
                    self._recorder(
                        rule.id, bar.symbol, cond.role, cond.on,
                        bar.close_ts, raw, log.satisfied(cond),
                    )

    def _decide(self, rule: Rule, bar: Bar) -> Signal | None:
        inst = self.instance(rule.id, bar.symbol)
        machine = self._machines[rule.id]
        step = Step(
            satisfied=tuple(inst.logs[c.role].satisfied(c) for c in rule.conditions),
            persistent=tuple(c.mode.is_persistent for c in rule.conditions),
            bar_close_ts=bar.close_ts,
        )
        transition = machine.advance(inst.state, step)
        if transition.outcome is not Outcome.NONE:
            self.transitions.append((rule.id, bar.symbol, bar.close_ts, transition.outcome))
        if transition.outcome is not Outcome.FIRED:
            return None

        key = self._dedup_key(rule, bar, inst)
        if key in inst.seen:
            machine.abort(inst.state)  # 别把状态停在越界的 stage 上
            return None

        inst.remember(key)
        machine.fire(inst.state, bar.close_ts)
        return Signal(
            rule_id=rule.id,
            symbol=bar.symbol,
            direction=rule.emit.direction,
            timeframe=rule.timeframe,
            fired_at=bar.close_ts,
            trigger_price=bar.close,
            dedup_key=key,
            context=self._snapshot(rule, bar),
            role_bars=self._role_bars(rule, bar.symbol, bar.close_ts),
            priority=rule.emit.priority,
            trading_day=bar.trading_day,
        )

    # ------------------------------------------------------------ 辅助

    def _context(self, symbol: str, timeframe: Timeframe, as_of: int) -> EvalContext:
        view = self._store.view(symbol, as_of=as_of)
        return EvalContext(
            symbol=symbol,
            timeframe=timeframe,
            bars=view.bars(timeframe),
            cache=self.cache_for(symbol, timeframe),
            source=_ViewSource(view, self, symbol),
        )

    def _role_bars(self, rule: Rule, symbol: str, as_of: int) -> dict[str, int]:
        """触发时刻各级别最近一根已收盘 bar 的 close_ts。"""
        view = self._store.view(symbol, as_of=as_of)
        out: dict[str, int] = {}
        for cond in rule.conditions:
            last = view.last(cond.on)
            if last is not None:
                out[cond.role] = last.close_ts
        return out

    def _dedup_key(self, rule: Rule, bar: Bar, inst: _Instance) -> str:
        """去重键。除通用占位符外，每个角色都有 ``{<role>_bar_close_ts}``
        —— 默认把去重绑在大级别 bar 上，就是"同一根大级别 bar 内同规则同标的只报一次"。"""
        fields: dict[str, object] = {
            "symbol": bar.symbol,
            "rule": rule.id,
            "timeframe": rule.timeframe.value,
            "bar_close_ts": bar.close_ts,
            "trading_day": bar.trading_day or "",
            "direction": str(rule.emit.direction),
            "armed_at": inst.state.armed_at,
        }
        for role, ts in self._role_bars(rule, bar.symbol, bar.close_ts).items():
            fields[f"{role}_bar_close_ts"] = ts
        try:
            return rule.emit.dedup_key.format(**fields)
        except KeyError as e:
            raise ValueError(
                f"规则 {rule.id} 的 dedup_key 用了未知占位符 {e}；可用: {', '.join(sorted(fields))}"
            ) from e

    def _snapshot(self, rule: Rule, bar: Bar) -> dict[str, float | None]:
        """触发时刻的关键值快照（§5.4）。

        ``context`` 在扳机周期上求值；``context_by_role`` 在各自角色的周期上求值 ——
        推送里的"各级别关键值"靠后者。无论声明与否都带上收盘价与成交量，
        因为一条不带价格的信号在推送里几乎没法用。
        """
        snap: dict[str, float | None] = {"close": bar.close, "volume": bar.volume}
        trigger_ctx = self._context(bar.symbol, rule.timeframe, bar.close_ts)
        for name, expr in rule.context:
            snap[name] = _safe_value(expr, trigger_ctx)
        for role, exprs in rule.context_by_role:
            cond = next(c for c in rule.conditions if c.role == role)
            ctx = self._context(bar.symbol, cond.on, bar.close_ts)
            for name, expr in exprs:
                snap[f"{role}.{name}"] = _safe_value(expr, ctx)
        return snap


def _safe_value(expr: CompiledExpr, ctx: EvalContext) -> float | None:
    """快照算不出来（除零、预热期）不该拖垮信号本身。"""
    try:
        value = scalar(expr.value(ctx))
    except ExprError:
        return None
    return None if isinstance(value, str) else value


__all__ = ["DEDUP_MEMORY", "RuleEngine"]
