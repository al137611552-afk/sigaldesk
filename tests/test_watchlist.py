"""预警组的组装逻辑（web/watchlist.py）与钉住的落库。纯逻辑，脱离 web app 直接测。"""

from __future__ import annotations

import pathlib

from sigdesk.store.runtime_store import RuntimeStore
from sigdesk.web.watchlist import build_group, latest_by_symbol


def sig(uid: str, fired: int, rule: str = "r1", direction: str = "long") -> dict:
    return {"symbol": uid, "fired_at": fired, "rule_id": rule, "direction": direction,
            "dedup_key": f"{uid}:{rule}:{fired}", "trigger_price": 100.0}


# ------------------------------------------------------------ 压成一格一标的

def test_one_row_per_symbol_keeping_the_newest() -> None:
    """一个标的连着触发五次，在组里仍然只占一格。

    占五格的话，九格会被一个躁动的品种吃光 —— 而那恰恰是最不需要盯的情况。
    """
    rows = latest_by_symbol([sig("A", 100), sig("A", 300), sig("A", 200), sig("B", 150)])
    assert [r["symbol"] for r in rows] == ["A", "B"]
    assert rows[0]["fired_at"] == 300


def test_newest_first() -> None:
    rows = latest_by_symbol([sig("A", 100), sig("B", 500), sig("C", 300)])
    assert [r["symbol"] for r in rows] == ["B", "C", "A"]


def test_order_is_deterministic_when_times_tie() -> None:
    """同一时刻触发的两个标的要有稳定顺序，否则每次刷新格子位置都在跳。"""
    a = latest_by_symbol([sig("B", 100), sig("A", 100)])
    b = latest_by_symbol([sig("A", 100), sig("B", 100)])
    assert [r["symbol"] for r in a] == [r["symbol"] for r in b] == ["A", "B"]


# ------------------------------------------------------------ 组装

def test_group_is_recent_signals_when_nothing_pinned() -> None:
    g = build_group([], latest_by_symbol([sig("A", 100), sig("B", 300), sig("C", 200)]))
    assert [e["symbol"] for e in g] == ["B", "C", "A"]
    assert all(e["pinned"] is False for e in g)


def test_pinned_come_first_in_pin_order() -> None:
    """钉住的排在最前，顺序是钉住的先后 —— 位置稳定，眼睛才记得住哪个在哪。"""
    recent = latest_by_symbol([sig("A", 100), sig("B", 300), sig("C", 200)])
    g = build_group(["C", "A"], recent)
    assert [e["symbol"] for e in g] == ["C", "A", "B"]
    assert [e["pinned"] for e in g] == [True, True, False]


def test_pinned_are_never_evicted() -> None:
    """**这是整个功能的核心。** 钉住 = 人工判断「还需要观察」，
    被新信号挤掉的话这个功能就白做了。"""
    recent = latest_by_symbol([sig(f"S{i}", 1000 + i) for i in range(20)])
    g = build_group(["OLD"], recent)
    assert g[0]["symbol"] == "OLD"
    assert len(g) == 9


def test_unsignalled_pin_has_no_rule() -> None:
    """钉住了一个从没触发过的标的：rule_id 为 None，
    前端据此显示「手动钉住」，而不是编一条不存在的规则出来。"""
    g = build_group(["QUIET"], [])
    assert g[0]["rule_id"] is None
    assert g[0]["fired_at"] is None
    assert g[0]["pinned"] is True


def test_pinned_symbol_is_not_listed_twice() -> None:
    """钉住的标的又触发了新信号：仍然只占一格，且带上那条信号。"""
    g = build_group(["A"], latest_by_symbol([sig("A", 500), sig("B", 100)]))
    assert [e["symbol"] for e in g] == ["A", "B"]
    assert g[0]["fired_at"] == 500


def test_group_is_capped_at_nine() -> None:
    g = build_group([], latest_by_symbol([sig(f"S{i}", 100 + i) for i in range(30)]))
    assert len(g) == 9


def test_pins_beyond_the_slots_are_still_returned() -> None:
    """钉住的比格子还多时**照样全返回**，由调用方如实告诉用户，
    而不是在这里悄悄砍掉一半 —— 用户会以为自己的钉住丢了。"""
    g = build_group([f"P{i}" for i in range(12)], [])
    assert len(g) == 12
    assert all(e["pinned"] for e in g)


def test_duplicate_pins_do_not_take_two_slots() -> None:
    g = build_group(["A", "A", "B"], [])
    assert [e["symbol"] for e in g] == ["A", "B"]


def test_group_is_a_pure_function_of_its_inputs() -> None:
    """同样的输入永远给出同样的组。组是**算出来的**，不是维护出来的 ——
    没有淘汰逻辑，也就没有"手动删了又被加回来"这类状态同步 bug。"""
    recent = latest_by_symbol([sig("A", 100), sig("B", 300)])
    assert build_group(["B"], recent) == build_group(["B"], recent)


# ------------------------------------------------------------ 钉住落库

def test_pins_persist_in_pin_order() -> None:
    with RuntimeStore() as s:
        s.pin("B", 200)
        s.pin("A", 100)
        assert s.pins() == ["A", "B"], "顺序按钉住时刻，不是字典序"


def test_pinning_twice_keeps_the_original_position() -> None:
    """重复点一下不该把它挪到最后 —— 位置跳动比不生效更让人困惑。"""
    with RuntimeStore() as s:
        s.pin("A", 100)
        s.pin("B", 200)
        assert s.pin("A", 999) is False
        assert s.pins() == ["A", "B"]


def test_unpin() -> None:
    with RuntimeStore() as s:
        s.pin("A", 100)
        assert s.unpin("A") is True
        assert s.unpin("A") is False
        assert s.pins() == []


def test_pins_survive_reopen(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """钉住是这个功能唯一的持久状态，重启后必须还在。"""
    db = tmp_path / "rt.sqlite3"
    with RuntimeStore(db) as s:
        s.pin("CN.SHFE.rb2610", 100)
    with RuntimeStore(db) as s:
        assert s.pins() == ["CN.SHFE.rb2610"]


# ------------------------------------------------------------ 钉住即采集

def test_pinned_symbols_join_the_collection_list() -> None:
    """**钉住的标的也要采集。**

    这条横跨面板与采集端：钉住写在 SQLite 里，`scripts/watch.py` 启动时读它、
    并进规则 universe。少了这一步，钉住一个没规则盯的品种就会得到一个
    永远空着的格子 —— 而且是静默的空。

    用源码断言而不是导入 watch.py：那个脚本一导入就会拉起一堆 feed 依赖。
    这里守的是"接线还在"，真正的口径由 build_group 与 RuntimeStore.pins 的单测覆盖。
    """
    src = pathlib.Path("scripts/watch.py").read_text(encoding="utf-8")
    assert "def wanted_symbols(" in src
    fn = src[src.index("def wanted_symbols("):]
    fn = fn[: fn.index("\ndef ", 1)]
    assert "pinned" in fn, "采集列表没有把钉住的并进来"
    assert "from_rules | set(pinned)" in fn, "并集写法变了，检查是否仍然包含钉住的"
    assert "_pins_db.pins()" in src, "启动时没有从 RuntimeStore 读钉住的标的"


def test_watch_reports_pins_that_no_rule_covers() -> None:
    """钉住了一个没规则盯的品种时要说出来 —— 那是采集列表变长的原因，
    不说的话下次看日志会以为多采了标的是个 bug。"""
    src = pathlib.Path("scripts/watch.py").read_text(encoding="utf-8")
    assert "来自预警组钉住" in src
