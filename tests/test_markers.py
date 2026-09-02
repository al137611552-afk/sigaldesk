"""标记的折叠与配对（web/markers.py）。纯逻辑，脱离 web app 直接测。"""

from __future__ import annotations

import pytest

from sigdesk.core.models import Timeframe
from sigdesk.rules.model import Priority, rank_key
from sigdesk.web.markers import collapse, pair_trades


def sig(rule: str, *, bucket: int = 100, direction: str = "long",
        priority: str = "normal", tf: str = "1m", tentative: bool = False,
        fired: int = 100, price: float = 10.0) -> dict:
    return {
        "bucket_ts": bucket, "fired_at": fired, "direction": direction,
        "rule_id": rule, "dedup_key": f"{rule}@{fired}", "trigger_price": price,
        "priority": priority, "timeframe": tf, "tentative": tentative,
    }


# ---------------------------------------------------------------- 排序键

def test_declared_priority_outranks_the_timeframe() -> None:
    """声明档位在周期之前：手动标的 high 应当压过一个更大周期的 normal。
    这是有意的取舍 —— 人主动标的应当能盖过自动规则。"""
    high_1m = rank_key(tentative=False, priority=Priority.HIGH,
                       timeframe=Timeframe.M1, chain_len=1, rule_id="a")
    normal_1d = rank_key(tentative=False, priority=Priority.NORMAL,
                         timeframe=Timeframe.D1, chain_len=1, rule_id="a")
    assert high_1m < normal_1d


def test_bigger_timeframe_wins_at_equal_priority() -> None:
    """同档位时大周期优先 —— "看大做小"的判断顺序。"""
    assert rank_key(tentative=False, priority=Priority.NORMAL, timeframe=Timeframe.D1,
                    chain_len=1, rule_id="a") < \
           rank_key(tentative=False, priority=Priority.NORMAL, timeframe=Timeframe.M5,
                    chain_len=1, rule_id="a")


def test_tentative_always_loses() -> None:
    """盘中预报永远排在已确认之后，哪怕它档位更高、周期更大。"""
    assert rank_key(tentative=False, priority=Priority.LOW, timeframe=Timeframe.M1,
                    chain_len=1, rule_id="z") < \
           rank_key(tentative=True, priority=Priority.HIGH, timeframe=Timeframe.D1,
                    chain_len=9, rule_id="a")


def test_longer_chain_wins() -> None:
    """跨三个级别验证过的比单条件的可信。"""
    assert rank_key(tentative=False, priority=Priority.NORMAL, timeframe=Timeframe.M1,
                    chain_len=3, rule_id="b") < \
           rank_key(tentative=False, priority=Priority.NORMAL, timeframe=Timeframe.M1,
                    chain_len=1, rule_id="a")


def test_rank_is_a_total_order() -> None:
    """**红线**：各项全相等时仍必须由 rule_id 定序。少了这一级，代表的选择
    取决于字典迭代顺序 —— 回放与实盘会折出不同的代表，且只在并列时偶发。"""
    a = rank_key(tentative=False, priority=Priority.HIGH, timeframe=Timeframe.M1,
                 chain_len=2, rule_id="aaa")
    b = rank_key(tentative=False, priority=Priority.HIGH, timeframe=Timeframe.M1,
                 chain_len=2, rule_id="bbb")
    assert a < b and a != b


# ---------------------------------------------------------------- 折叠

def test_same_bar_same_direction_collapses() -> None:
    out = collapse([sig("r1"), sig("r2", fired=101), sig("r3", fired=102)])
    assert len(out) == 1
    assert out[0]["count"] == 3
    assert [m["rule_id"] for m in out[0]["members"]] == ["r1", "r2", "r3"]


def test_opposite_directions_never_collapse() -> None:
    """同一根 bar 上多空同时触发是**矛盾**，必须两枚都留下 —— 折成一个代表
    等于把矛盾藏起来，而那正是最该被看见的一刻。"""
    out = collapse([sig("bull"), sig("bear", direction="short")])
    assert len(out) == 2
    assert {m["direction"] for m in out} == {"long", "short"}
    assert all(m["count"] == 1 for m in out)


def test_representative_is_the_highest_ranked() -> None:
    out = collapse([
        sig("plain"),
        sig("flagged", fired=101, priority="high"),
        sig("weak", fired=102, priority="low"),
    ])
    assert out[0]["rule_id"] == "flagged"
    assert out[0]["trigger_price"] == out[0]["members"][0]["trigger_price"]


def test_chain_length_breaks_the_tie() -> None:
    out = collapse([sig("short_chain"), sig("long_chain", fired=101)],
                   {"long_chain": 3, "short_chain": 1})
    assert out[0]["rule_id"] == "long_chain"


def test_unknown_priority_in_old_rows_does_not_explode() -> None:
    """枚举是后加的，早先落盘的信号里可能存着任意字符串。读回时报错会让整个面板
    打不开 —— 历史数据不该因为今天收紧口径而变成毒丸。"""
    out = collapse([sig("legacy", priority="higth")])
    assert out[0]["priority"] == "normal"


def test_collapse_is_deterministic_regardless_of_input_order() -> None:
    rows = [sig("c", fired=102), sig("a"), sig("b", fired=101)]
    assert collapse(rows) == collapse(list(reversed(rows)))


# ---------------------------------------------------------------- 配对

def fill(key: str, kind: str, ts: int, price: float, *, side: str = "buy",
         realized: float = 0.0, qty: float = 1.0) -> dict:
    return {"signal_key": key, "kind": kind, "ts": ts, "bucket_ts": ts,
            "price": price, "side": side, "realized": realized, "qty": qty}


def test_entry_and_exit_pair_into_one_trade() -> None:
    trades = pair_trades([
        fill("s1", "entry", 100, 10.0),
        fill("s1", "target", 200, 11.0, side="sell", realized=1.0),
    ], {"s1": 10.0})
    assert len(trades) == 1
    t = trades[0]
    assert t["entry"]["price"] == 10.0
    assert t["exit"]["kind"] == "target"
    assert t["open"] is False
    assert t["pnl_pct"] == pytest.approx(10.0)


@pytest.mark.parametrize("kind", ["stop", "target", "horizon", "forced"])
def test_every_exit_kind_pairs(kind: str) -> None:
    """离场用"不是 entry"来判定而不是白名单：新增一种 FillKind 时，白名单会静默
    漏掉它，那笔交易永远配不上对，图上只剩一个孤零零的开仓点。"""
    trades = pair_trades([fill("s1", "entry", 100, 10.0), fill("s1", kind, 200, 11.0)])
    assert trades[0]["exit"]["kind"] == kind


def test_open_position_still_renders() -> None:
    """只开仓没离场 = 持仓中，照样返回。等平了才画就看不见"这笔还开着"。"""
    trades = pair_trades([fill("s1", "entry", 100, 10.0)])
    assert trades[0]["open"] is True
    assert trades[0]["exit"] is None
    assert trades[0]["pnl_pct"] is None


def test_pnl_is_none_without_notional_not_zero() -> None:
    """算不出来要显示破折号，**不是 0**。这个项目已经在"算不出来显示成 0"上
    栽过好几次 —— 0% 会被读成"这笔白做了"，与"不知道"是两回事。"""
    trades = pair_trades([
        fill("s1", "entry", 100, 10.0),
        fill("s1", "stop", 200, 9.0, realized=-1.0),
    ])
    assert trades[0]["pnl_pct"] is None
    assert trades[0]["realized"] == -1.0


def test_orphan_exit_without_entry_is_skipped() -> None:
    assert pair_trades([fill("s1", "stop", 200, 9.0)]) == []
