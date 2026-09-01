"""规则状态机单测。纯逻辑、无任何 IO —— 状态机是 M2 的心脏，必须能脱环境逐条验证。

约定：三段链路 = (trend[state], setup[window], trigger[event])，
``persistent = (True, False, False)`` —— 只有 trend 是持续型。
"""

from __future__ import annotations

import pytest

from sigdesk.rules.state import Outcome, Phase, RuleState, StateMachine, Step

PERSIST3 = (True, False, False)
PERSIST1 = (False,)
T = True
F = False


def step(*sat: bool | None, ts: int = 1000, persistent: tuple[bool, ...] | None = None) -> Step:
    return Step(
        satisfied=tuple(sat),
        persistent=persistent or (PERSIST3 if len(sat) == 3 else (False,) * len(sat)),
        bar_close_ts=ts,
    )


def sm(chain_len: int = 3, ttl: int = 0, cooldown: int = 0) -> StateMachine:
    return StateMachine(chain_len=chain_len, ttl_bars=ttl, cooldown_s=cooldown)


# ---------------------------------------------------------------- 基本推进


def test_chain_advances_one_stage_per_bar() -> None:
    machine, state = sm(), RuleState()
    assert machine.advance(state, step(T, F, F)).outcome is Outcome.ADVANCED
    assert (state.stage, state.phase(3)) == (1, Phase.PROGRESSING)
    assert machine.advance(state, step(T, T, F)).outcome is Outcome.ADVANCED
    assert (state.stage, state.phase(3)) == (2, Phase.ARMED)
    assert machine.advance(state, step(T, T, T)).outcome is Outcome.FIRED


def test_whole_chain_can_complete_in_one_bar() -> None:
    """三段同时成立时一根 bar 直接打穿 —— 不该人为拖三根。"""
    machine, state = sm(), RuleState()
    assert machine.advance(state, step(T, T, T)).outcome is Outcome.FIRED


def test_later_condition_alone_does_not_advance() -> None:
    """扳机先成立、趋势还没成立 ⇒ 什么也不该发生。链路是有序的。"""
    machine, state = sm(), RuleState()
    assert machine.advance(state, step(F, T, T)).outcome is Outcome.NONE
    assert state.stage == 0


def test_single_condition_chain_fires_immediately() -> None:
    """单级别规则 = 长度 1 的链路，与 M1 行为一致。"""
    machine, state = sm(chain_len=1), RuleState()
    assert machine.advance(state, step(T, persistent=PERSIST1)).outcome is Outcome.FIRED


def test_unknown_is_treated_as_not_satisfied() -> None:
    """预热期 None 不推进、也不算失效（此时 stage 尚未越过它）。"""
    machine, state = sm(), RuleState()
    assert machine.advance(state, step(None, T, T)).outcome is Outcome.NONE
    assert state.stage == 0


# ---------------------------------------------------------------- 回退（验收项）


def test_persistent_condition_failing_retreats_the_chain() -> None:
    """M2 验收：trend 失效时链路正确回退，不留残留 armed 状态。"""
    machine, state = sm(), RuleState()
    machine.advance(state, step(T, T, F))
    assert state.stage == 2 and state.phase(3) is Phase.ARMED

    tr = machine.advance(state, step(F, T, F))

    assert tr.outcome is Outcome.RETREATED
    assert (state.stage, state.ttl_left, state.armed_at) == (0, 0, 0)
    assert state.phase(3) is Phase.IDLE


def test_retreat_beats_trigger_on_the_same_bar() -> None:
    """同一根上 trend 失效且扳机成立 —— 必须回退，不能发信号。
    反向趋势里的扳机正是最该被拦掉的那种假信号。"""
    machine, state = sm(), RuleState()
    machine.advance(state, step(T, T, F))
    assert machine.advance(state, step(F, T, T)).outcome is Outcome.RETREATED
    assert state.stage == 0


def test_non_persistent_condition_need_not_persist() -> None:
    """setup 是 window/event 型：达成之后不再要求它持续成立，否则链路永远推进不下去。"""
    machine, state = sm(), RuleState()
    machine.advance(state, step(T, T, F))
    tr = machine.advance(state, step(T, F, T))  # setup 已不成立，但扳机来了
    assert tr.outcome is Outcome.FIRED


def test_retreat_goes_to_the_stage_before_the_failure_not_to_idle() -> None:
    """四段链路里第二段（也是持续型）失效 ⇒ 退回 stage 1，保留第一段的判断。"""
    machine = StateMachine(chain_len=4)
    state = RuleState()
    persistent = (True, True, False, False)
    machine.advance(state, step(T, T, T, F, persistent=persistent))
    assert state.stage == 3

    tr = machine.advance(state, step(T, F, T, F, persistent=persistent))

    assert tr.outcome is Outcome.RETREATED
    assert state.stage == 1, "应退回失效条件之前那一段，而不是一路退到 IDLE"


# ---------------------------------------------------------------- TTL


def test_ttl_counts_trigger_bars_and_expires() -> None:
    machine, state = sm(ttl=3), RuleState()
    machine.advance(state, step(T, T, F))
    assert state.ttl_left == 3

    for expected in (2, 1, 0):
        assert machine.advance(state, step(T, F, F)).outcome is Outcome.NONE
        assert state.ttl_left == expected

    tr = machine.advance(state, step(T, F, F))
    assert tr.outcome is Outcome.EXPIRED
    assert state.stage == 1, "TTL 到期退回上一段，趋势判断不该一起丢掉"
    assert state.ttl_left == 0 and state.armed_at == 0


def test_ttl_does_not_expire_when_trigger_fires_in_time() -> None:
    machine, state = sm(ttl=2), RuleState()
    machine.advance(state, step(T, T, F))
    assert machine.advance(state, step(T, F, F)).outcome is Outcome.NONE
    assert machine.advance(state, step(T, F, T)).outcome is Outcome.FIRED


def test_zero_ttl_means_no_expiry() -> None:
    machine, state = sm(ttl=0), RuleState()
    machine.advance(state, step(T, T, F))
    for _ in range(50):
        assert machine.advance(state, step(T, F, F)).outcome is Outcome.NONE
    assert state.stage == 2


def test_ttl_restarts_when_rearmed() -> None:
    """回退再重新 armed 时 TTL 必须重新计满，不能沿用上一轮的残值。"""
    machine, state = sm(ttl=2), RuleState()
    machine.advance(state, step(T, T, F))
    machine.advance(state, step(T, F, F))
    assert state.ttl_left == 1
    machine.advance(state, step(F, F, F))  # 回退
    assert state.stage == 0
    machine.advance(state, step(T, T, F))  # 重新推进到 armed
    assert state.ttl_left == 2


def test_armed_at_records_the_bar() -> None:
    machine, state = sm(ttl=5), RuleState()
    machine.advance(state, step(T, T, F, ts=7777))
    assert state.armed_at == 7777


# ---------------------------------------------------------------- 冷却


def test_cooldown_blocks_then_releases_to_stage_one() -> None:
    machine, state = sm(cooldown=300), RuleState()
    machine.advance(state, step(T, T, T, ts=1000))
    machine.fire(state, 1000)
    assert state.cooldown_until == 1300 and state.phase(3) is Phase.COOLDOWN

    assert machine.advance(state, step(T, T, T, ts=1200)).outcome is Outcome.NONE
    assert state.phase(3) is Phase.COOLDOWN

    tr = machine.advance(state, step(T, F, F, ts=1300))
    assert tr.outcome is Outcome.COOLED
    assert state.stage == 1, "冷却结束回到 TREND_OK，而不是 IDLE"
    assert state.cooldown_until == 0


def test_cooldown_is_exactly_the_configured_duration() -> None:
    """解冷那一根要**继续参与判定**。若把它消耗掉，实际冷却会比配置的多一根 bar，
    而且多出的时长还随周期变化（5m 周期多 5 分钟，1h 周期多 1 小时）。"""
    machine, state = sm(cooldown=300), RuleState()
    machine.advance(state, step(T, T, T, ts=1000))
    machine.fire(state, 1000)

    assert machine.advance(state, step(T, T, T, ts=1299)).outcome is Outcome.NONE
    assert machine.advance(state, step(T, T, T, ts=1300)).outcome is Outcome.FIRED, (
        "冷却刚好走完的那一根就该能触发"
    )


def test_fire_without_cooldown_returns_to_waiting() -> None:
    machine, state = sm(cooldown=0), RuleState()
    machine.advance(state, step(T, T, T, ts=500))
    machine.fire(state, 500)
    assert state.cooldown_until == 0
    assert state.stage == 1
    assert state.last_fired_ts == 500


def test_abort_puts_state_back_to_armed() -> None:
    """扳机成立但信号被去重挡下时，状态不能停在越界的 stage 上。"""
    machine, state = sm(), RuleState()
    machine.advance(state, step(T, T, T))
    assert state.stage == 3
    machine.abort(state)
    assert state.stage == 2 and state.phase(3) is Phase.ARMED


# ---------------------------------------------------------------- 序列化


def test_state_round_trips_through_rows() -> None:
    """SQLite 持久化的前提：状态能原样存取。"""
    state = RuleState(stage=2, ttl_left=3, cooldown_until=999, armed_at=888, last_fired_ts=777)
    assert RuleState.from_row(state.as_row()) == state


def test_from_row_tolerates_missing_columns() -> None:
    assert RuleState.from_row({}) == RuleState()


# ---------------------------------------------------------------- 参数校验


def test_step_length_must_match_chain() -> None:
    machine, state = sm(chain_len=3), RuleState()
    with pytest.raises(ValueError, match="条件数不匹配"):
        machine.advance(state, step(T, T))


def test_empty_chain_rejected() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        StateMachine(chain_len=0)
    with pytest.raises(ValueError, match="不能为空"):
        Step(satisfied=(), persistent=(), bar_close_ts=0)


def test_step_arity_must_agree() -> None:
    with pytest.raises(ValueError, match="长度必须一致"):
        Step(satisfied=(True,), persistent=(True, False), bar_close_ts=0)
