"""规则状态机（ARCHITECTURE §5.3）。纯逻辑：无 IO、无当前时间、无 BarStore 依赖。

一条规则是**有序条件链路**。``stage`` = 已满足到第几段：

    stage 0 (IDLE) ──C0成立──> stage 1 ──C1成立──> stage 2 = ARMED（只差扳机，TTL 开始倒数）
        ↑                          │                         │
        └────── 持续型前置条件失效 ─┴─────────────────────────┤ 扳机成立
                                                             ↓
                              stage 1 <── 冷却结束 ── COOLDOWN <── FIRED

三条关键设计（都被单测钉住）：

1. **只有持续型（state）前置条件失效才回退**。window/event 是瞬时的，
   要求它们"仍然成立"没有意义 —— 那样链路永远推进不下去。
2. **TTL 用扳机周期的 bar 根数，不用墙钟**。休市与数据缺口不消耗 TTL，
   因此回放与实盘的作废时刻完全一致（红线验收的前提之一）。
3. **回退目标是"失效条件之前那一段"**，不是无脑回到 IDLE：
   trend 还在、只是 setup 过期时，应该退回 TREND_OK 继续等下一次 setup，
   而不是把趋势判断也丢掉重来。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Phase(StrEnum):
    """对外可读的阶段名。内部真正的状态是 ``stage`` 整数，这里只是给人看的投影。"""

    IDLE = "idle"
    PROGRESSING = "progressing"  # 链路推进中，但还没到"只差扳机"
    ARMED = "armed"  # 只差扳机，TTL 倒数中
    COOLDOWN = "cooldown"


class Outcome(StrEnum):
    """一次推进的结果。引擎据此决定要不要发信号，也便于日志与回放对账。"""

    NONE = "none"  # 什么也没发生
    ADVANCED = "advanced"  # 链路前进了一段
    FIRED = "fired"  # 扳机成立
    EXPIRED = "expired"  # TTL 到期作废
    RETREATED = "retreated"  # 持续型前置条件失效，链路回退
    COOLED = "cooled"  # 冷却结束，回到可再次推进的状态


@dataclass(slots=True)
class RuleState:
    """一个 (rule, symbol) 的状态。可完整序列化到 SQLite 并原样恢复。"""

    stage: int = 0
    ttl_left: int = 0  # 只在 ARMED 阶段有意义；0 表示不限或未 armed
    cooldown_until: int = 0  # bar close_ts；0 表示不在冷却
    armed_at: int = 0  # 进入 ARMED 的 bar close_ts，便于复盘
    last_fired_ts: int = 0

    def phase(self, chain_len: int) -> Phase:
        if self.cooldown_until:
            return Phase.COOLDOWN
        if self.stage <= 0:
            return Phase.IDLE
        if self.stage >= chain_len - 1:
            return Phase.ARMED
        return Phase.PROGRESSING

    def as_row(self) -> dict[str, int]:
        return {
            "stage": self.stage,
            "ttl_left": self.ttl_left,
            "cooldown_until": self.cooldown_until,
            "armed_at": self.armed_at,
            "last_fired_ts": self.last_fired_ts,
        }

    @staticmethod
    def from_row(row: dict[str, int]) -> RuleState:
        return RuleState(
            stage=int(row.get("stage", 0)),
            ttl_left=int(row.get("ttl_left", 0)),
            cooldown_until=int(row.get("cooldown_until", 0)),
            armed_at=int(row.get("armed_at", 0)),
            last_fired_ts=int(row.get("last_fired_ts", 0)),
        )


@dataclass(frozen=True, slots=True)
class Step:
    """一次推进的输入：各段条件在**当前扳机 bar 时刻**的判定。

    ``satisfied[i]``：第 i 段此刻是否满足（None = 未知/预热期，一律按不满足处理）。
    ``persistent[i]``：第 i 段是不是持续型（只有它失效才回退）。
    """

    satisfied: tuple[bool | None, ...]
    persistent: tuple[bool, ...]
    bar_close_ts: int

    def __post_init__(self) -> None:
        if len(self.satisfied) != len(self.persistent):
            raise ValueError("satisfied 与 persistent 长度必须一致")
        if not self.satisfied:
            raise ValueError("条件链路不能为空")


@dataclass(frozen=True, slots=True)
class Transition:
    outcome: Outcome
    stage_before: int
    stage_after: int
    detail: str = ""


@dataclass(slots=True)
class StateMachine:
    """链路推进器。一次 ``advance`` 对应扳机周期上的一根 bar。"""

    chain_len: int
    ttl_bars: int = 0
    cooldown_s: int = 0

    def __post_init__(self) -> None:
        if self.chain_len < 1:
            raise ValueError("条件链路不能为空")

    def advance(self, state: RuleState, step: Step) -> Transition:
        """推进一步，**就地**修改 state，返回本次发生了什么。

        顺序是有讲究的：先解冷却 → 再查回退 → 再算 TTL → 最后才推进链路。
        把回退放在推进之前，是为了避免"这一根既失效又前进"的模糊状态。
        """
        if len(step.satisfied) != self.chain_len:
            raise ValueError(
                f"条件数不匹配：状态机 {self.chain_len} 段，收到 {len(step.satisfied)} 段"
            )
        before = state.stage

        cooled = False
        if state.cooldown_until:
            if step.bar_close_ts < state.cooldown_until:
                return Transition(Outcome.NONE, before, before, "冷却中")
            state.cooldown_until = 0
            # 冷却结束后不回 IDLE：趋势多半还在，退回"已满足首段"继续等下一次机会
            state.stage = min(1, self.chain_len - 1) if self.chain_len > 1 else 0
            cooled = True
            # **不在这里 return**：解冷这一根要继续参与后续判定。
            # 若把它消耗掉，实际冷却就比配置的多一根 bar，而且多出的时长还随周期变化。

        retreat = self._retreat_target(state.stage, step)
        if retreat is not None:
            state.stage = retreat
            state.ttl_left = 0
            state.armed_at = 0
            return Transition(Outcome.RETREATED, before, retreat, "持续型前置条件失效")

        if self._is_armed(state) and self.ttl_bars:
            state.ttl_left -= 1
            if state.ttl_left < 0:
                # TTL 用完：退回上一段（首段多半还成立），别把整条链路丢掉
                state.stage = max(0, self.chain_len - 2)
                state.ttl_left = 0
                state.armed_at = 0
                return Transition(Outcome.EXPIRED, before, state.stage, "TTL 到期")

        advanced = False
        while state.stage < self.chain_len and step.satisfied[state.stage] is True:
            state.stage += 1
            advanced = True
            if self._is_armed(state):
                state.ttl_left = self.ttl_bars
                state.armed_at = step.bar_close_ts

        if state.stage >= self.chain_len:
            state.stage = self.chain_len  # FIRED，由 fire() 收尾
            return Transition(Outcome.FIRED, before, state.stage, "扳机成立")
        if advanced:
            return Transition(Outcome.ADVANCED, before, state.stage, "链路前进")
        if cooled:
            return Transition(Outcome.COOLED, before, state.stage, "冷却结束")
        return Transition(Outcome.NONE, before, state.stage, "")

    def fire(self, state: RuleState, bar_close_ts: int) -> None:
        """信号已发出，进入冷却（或直接退回等待下一次）。"""
        state.last_fired_ts = bar_close_ts
        state.ttl_left = 0
        state.armed_at = 0
        if self.cooldown_s:
            state.cooldown_until = bar_close_ts + self.cooldown_s
            state.stage = max(0, self.chain_len - 1)
        else:
            state.stage = min(1, self.chain_len - 1) if self.chain_len > 1 else 0

    def abort(self, state: RuleState) -> None:
        """扳机成立但信号被去重/冷却挡下时，把状态退回 ARMED，别停在越界的 stage 上。"""
        state.stage = max(0, self.chain_len - 1)

    def _is_armed(self, state: RuleState) -> bool:
        return self.chain_len > 1 and state.stage == self.chain_len - 1

    def _retreat_target(self, stage: int, step: Step) -> int | None:
        """已满足的持续型前置条件里，最早失效的那个 —— 回退到它之前那一段。"""
        for i in range(min(stage, self.chain_len)):
            if step.persistent[i] and step.satisfied[i] is not True:
                return i
        return None


__all__ = ["Outcome", "Phase", "RuleState", "StateMachine", "Step", "Transition"]
