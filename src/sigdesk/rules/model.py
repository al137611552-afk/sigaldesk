"""规则与信号的数据模型。纯值对象，无 IO。

一条规则是一串**有序的条件链路**（ARCHITECTURE §5.1 的三段式是它的常见形态）：
``trend -> setup -> trigger``，前一段满足才轮到后一段，最后一段满足即触发。
单级别规则就是长度为 1 的链路 —— 因此 M1 与 M2 **共用一个模型、一个引擎**，
不存在"单级别引擎"和"多级别引擎"两套东西。

条件按 ``role``（角色名）而非周期来引用，规则头部的 ``timeframes`` 给出角色到周期的映射。
角色名会进入去重键占位符（``{trend_bar_close_ts}``），也进入信号的各级别快照。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..core.models import Timeframe
from ..patterns.expr import CompiledExpr

_DURATION = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# 默认去重键：同一根 bar 上同规则同标的只报一次
DEFAULT_DEDUP_KEY = "{symbol}:{rule}:{bar_close_ts}"


def parse_duration(text: str | int) -> int:
    """'30m' / '2h' / 90 -> 秒。冷却时间用秒比较**bar 的 close_ts**，不看墙钟 ——
    这样回测与实盘的冷却行为逐条一致（M2 的红线验收就靠这个前提）。"""
    if isinstance(text, int):
        return text
    m = _DURATION.match(str(text))
    if not m:
        raise ValueError(f"无法解析时长 {text!r}；用 30s/15m/2h/1d 这种写法")
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2)]


class Mode(StrEnum):
    """条件的求值方式。

    - ``state``：当前 bar 成立即可（**持续状态**）。只有它会被检查"是否仍然成立"，
      因此只有它能让链路回退（trend 失效 ⇒ 整条链路退回）。
    - ``window``：最近 ``within`` 根该周期 bar 内**出现过**即可。瞬时达成后不要求持续。
    - ``event``：**边沿** —— 上一根不成立、这一根成立。想抓"刚刚发生"用它。
      未知（预热期 None）不算"不成立"，因此预热期结束后的第一根不会被误判成边沿。
    """

    STATE = "state"
    WINDOW = "window"
    EVENT = "event"

    @property
    def is_persistent(self) -> bool:
        """该模式是否表达"持续状态"。只有持续型条件失效才触发链路回退 ——
        window/event 是瞬时的，要求它们"仍然成立"没有意义，那样链路永远推进不下去。"""
        return self is Mode.STATE


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class Priority(StrEnum):
    """信号档位。规则里手写，用来决定密集处折叠时**留谁当代表**。

    收成枚举而不是自由字符串：原来 loader 直接 ``str(...)``，把 ``high`` 敲成
    ``higth`` 照收不误，静默多出一个谁也没定义的档位 —— 排序时它既不高也不低，
    表现就是"这条规则的信号偶尔莫名其妙被别的盖住"，且没有任何报错。
    """

    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"

    @property
    def rank(self) -> int:
        """越大越重要。排序键直接取负号用，别在调用处再写映射表。"""
        return {Priority.LOW: 0, Priority.NORMAL: 1, Priority.HIGH: 2}[self]

    @staticmethod
    def parse(value: object) -> Priority:
        """严格解析：给配置用，未知值报错。"""
        try:
            return Priority(str(value).strip().lower())
        except ValueError:
            allowed = ", ".join(p.value for p in Priority)
            raise ValueError(f"未知的 priority {value!r}；只能是 {allowed}") from None

    @staticmethod
    def coerce(value: object) -> Priority:
        """宽松解析：给**读历史数据**用。

        枚举是后加的，早先落盘的信号里可能存着任意字符串。读回时报错会让整个
        面板打不开 —— 历史数据不该因为今天收紧了口径而变成毒丸，落回 normal 即可。
        写入路径仍然是严格的，所以脏值只会越来越少。
        """
        try:
            return Priority(str(value).strip().lower())
        except ValueError:
            return Priority.NORMAL


@dataclass(frozen=True, slots=True)
class RuleCondition:
    role: str  # trend / setup / trigger 或自定义；单级别规则用周期名当角色
    on: Timeframe
    mode: Mode
    when: CompiledExpr
    within: int = 1  # window 模式：最近多少根该周期 bar 内出现过


@dataclass(frozen=True, slots=True)
class RuleEmit:
    direction: Direction = Direction.NEUTRAL
    cooldown_s: int = 0
    dedup_key: str = DEFAULT_DEDUP_KEY
    channels: tuple[str, ...] = ()
    priority: Priority = Priority.NORMAL
    # 恒为 True：盘中预报尚未实现，加载器会拒绝 false（见 loader）。
    confirm_on_close: bool = True
    # 链路推进到"只差最后一段"之后，多少根**扳机周期** bar 内没触发就作废。0 = 不限。
    # 用根数而非墙钟：休市与数据缺口不会消耗 TTL，回测与实盘因此逐条一致。
    ttl_bars: int = 0


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    universe: tuple[str, ...]
    conditions: tuple[RuleCondition, ...]  # 有序链路，最后一条是扳机
    emit: RuleEmit = field(default_factory=RuleEmit)
    enabled: bool = True
    description: str = ""
    # 触发时要快照的关键值：名字 -> 表达式。推送内容与事后统计都读它。
    # 表达式在**扳机周期**上求值；要看其他级别的值，用 context_by_role。
    context: tuple[tuple[str, CompiledExpr], ...] = ()
    # 按角色分组的快照：角色 -> (名字, 表达式)。在该角色自己的周期上求值，
    # 这样推送里才能带上"各级别关键值"（§5.4）。
    context_by_role: tuple[tuple[str, tuple[tuple[str, CompiledExpr], ...]], ...] = ()

    @property
    def trigger(self) -> RuleCondition:
        """扳机 = 链路最后一段。求值时机由它决定（§5.2）。"""
        return self.conditions[-1]

    @property
    def timeframe(self) -> Timeframe:
        """扳机周期。引擎只在这个周期的 bar 收盘时做触发判定。"""
        return self.trigger.on

    @property
    def timeframes(self) -> dict[str, Timeframe]:
        return {c.role: c.on for c in self.conditions}

    @property
    def is_multi_level(self) -> bool:
        return len({c.on for c in self.conditions}) > 1

    @property
    def required_timeframes(self) -> set[Timeframe]:
        """这条规则**要求 BarStore 派生**的全部周期。

        既包括各条件自身的周期，也包括表达式里用 ``at('1h', ...)`` 引用到的。
        少派生一个的后果不是报错，而是该级别恒为空序列 -> 指标恒 None ->
        条件恒"不成立" —— 一条信号都不报，且毫无提示。所以这个集合必须由规则自己给全。
        """
        need = {c.on for c in self.conditions}
        for c in self.conditions:
            need |= c.when.timeframes
        for _, expr in self.context:
            need |= expr.timeframes
        for _, pairs in self.context_by_role:
            for _, expr in pairs:
                need |= expr.timeframes
        return need


def store_timeframes(rules: Iterable[Rule]) -> list[Timeframe]:
    """一批规则要求 BarStore 派生的高周期（不含恒存的 1m），按周期长度升序。

    **建 BarStore 时一律用它**，别再手写 DERIVED 常量：规则里多写一个
    ``at('4h', ...)``，手写的常量不会跟着变，而后果是静默的 —— 该级别恒为空序列、
    条件恒"不成立"，一条信号都不报。
    """
    need: set[Timeframe] = set()
    for rule in rules:
        need |= rule.required_timeframes
    return sorted((tf for tf in need if tf is not Timeframe.M1), key=lambda t: t.rank)


@dataclass(frozen=True, slots=True)
class Signal:
    """一条已触发的信号（ARCHITECTURE §5.4）。

    ``fired_at`` 是**触发 bar 的 close_ts**，不是墙钟时刻 —— 回放与实盘因此可以逐条对齐。
    """

    rule_id: str
    symbol: str
    direction: Direction
    timeframe: Timeframe
    fired_at: int
    trigger_price: float
    dedup_key: str
    context: dict[str, float | None] = field(default_factory=dict)
    # 各级别在触发时刻的 bar close_ts（角色 -> close_ts）。
    # 推送内容、Web 回看与"为什么这一刻触发"的复盘都要它。
    role_bars: dict[str, int] = field(default_factory=dict)
    tentative: bool = False  # 盘中预报（INV-2）：不进统计、不下单
    priority: Priority = Priority.NORMAL
    trading_day: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "symbol": self.symbol,
            "direction": str(self.direction),
            "timeframe": str(self.timeframe),
            "fired_at": self.fired_at,
            "trigger_price": self.trigger_price,
            "dedup_key": self.dedup_key,
            "context": dict(self.context),
            "role_bars": dict(self.role_bars),
            "tentative": self.tentative,
            "priority": str(self.priority),
            "trading_day": self.trading_day,
        }


def rank_key(
    *,
    tentative: bool,
    priority: Priority,
    timeframe: Timeframe,
    chain_len: int,
    rule_id: str,
) -> tuple[Any, ...]:
    """折叠时的排序键，**越小越优先**（直接给 ``sorted`` 用）。

    逐级比较，每一级的理由：

    1. ``tentative``：盘中预报永远排在已确认之后。INV-2 已规定它不进统计，
       展示上也不该压过一条真信号。
    2. 声明档位：规则里手写的 high/normal/low。放在周期前面 —— 那是人主动标的，
       应当能压过自动规则。
    3. 扳机周期：**大周期优先**。日线的判断比 1m 的重，也正是"看大做小"的顺序。
    4. 链路长度：跨三个级别验证过的，比单条件的可信。
    5. ``rule_id`` 字典序：**兜底，保证全序**。少了它，两条各项都相等的规则谁当代表
       取决于字典迭代顺序 —— 回放和实盘会折出不同的代表，直接违反"replay 与 live
       逐条一致"这条红线，而且只在有并列时偶发，最难查。

    **方向不参与排序**：多空同时触发是矛盾信息，调用方必须分组而不是折叠
    （见 ``web/markers.collapse``）。折成一个代表等于把矛盾藏起来。

    取键而不是直接比较对象：信号从 SQLite 读回来时是一行 dict，没有 Signal 实例。
    两边各写一套比较逻辑迟早排出两种顺序，所以口径只有这一处。
    """
    return (tentative, -priority.rank, -timeframe.rank, -chain_len, rule_id)


def signal_rank(signal: Signal, chain_len: int = 1) -> tuple[Any, ...]:
    """``rank_key`` 的 Signal 版本。口径在 ``rank_key``，这里只负责取字段。"""
    return rank_key(
        tentative=signal.tentative,
        priority=signal.priority,
        timeframe=signal.timeframe,
        chain_len=chain_len,
        rule_id=signal.rule_id,
    )


__all__ = [
    "store_timeframes",
    "DEFAULT_DEDUP_KEY",
    "Direction",
    "Mode",
    "Priority",
    "Rule",
    "RuleCondition",
    "RuleEmit",
    "Signal",
    "rank_key",
    "signal_rank",
    "parse_duration",
]
