"""快照/恢复与 SQLite 运行态持久化单测。

M2 验收：**进程重启后状态机恢复，不丢报不重报**。
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Any

import pytest

from sigdesk.core.models import Bar, Timeframe
from sigdesk.rules.engine import RuleEngine
from sigdesk.rules.loader import load_rule
from sigdesk.rules.model import Direction, Signal
from sigdesk.rules.state import Phase
from sigdesk.store.bar_store import BarStore
from sigdesk.store.runtime_store import SCHEMA_VERSION, RuntimeStore

UID = "CRYPTO.OKX.BTCUSDT.PERP"

RULE: dict[str, Any] = {
    "id": "r1",
    "universe": [UID],
    "timeframes": {"trend": "5m", "setup": "1m", "trigger": "1m"},
    "conditions": [
        {"on": "trend", "mode": "state", "when": "close > 100"},
        {"on": "setup", "mode": "window", "within": 3, "when": "volume > 50"},
        {"on": "trigger", "mode": "event", "when": "close > open"},
    ],
    "emit": {"direction": "long", "ttl": "5 bars", "cooldown": "2m"},
}


def bar(ts: int, close: float, volume: float = 10.0, *, up: bool = False) -> Bar:
    open_ = close - 1.0 if up else close + 1.0
    return Bar(UID, Timeframe.M1, ts - 60, ts, open_, max(open_, close) + 0.5,
               min(open_, close) - 0.5, close, volume)


def fresh() -> tuple[RuleEngine, BarStore]:
    store = BarStore(timeframes=[Timeframe.M5])
    return RuleEngine([load_rule(RULE)], store), store


def run(engine: RuleEngine, store: BarStore, bars: list[Bar]) -> list[Signal]:
    return [s for b in bars for s in engine.on_bars(store.push(b))]


def history(n: int, *, volume: float = 10.0) -> list[Bar]:
    return [bar(60 * i, 101.0, volume=volume) for i in range(1, n + 1)]


# ---------------------------------------------------------------- 快照往返（纯逻辑）


def test_snapshot_round_trips_without_sqlite() -> None:
    """快照是纯数据，能脱离 SQLite 单独验证 —— 持久化层因此可以做得很薄。"""
    engine, store = fresh()
    run(engine, store, history(6, volume=99.0))
    before = engine.snapshot()
    assert before and before[0]["rule_id"] == "r1"

    engine2, _ = fresh()
    assert engine2.restore(before) == 1
    assert engine2.snapshot() == before


def test_restore_drops_state_of_removed_rules() -> None:
    """规则下线后旧状态不该悄悄复活。"""
    engine, _ = fresh()
    assert engine.restore([{"rule_id": "gone", "symbol": UID, "stage": 2}]) == 0
    assert engine.instances() == {}


def test_restore_ignores_roles_that_no_longer_exist() -> None:
    """规则改过之后，旧快照里已删掉的角色要被忽略，而不是炸掉。"""
    engine, store = fresh()
    run(engine, store, history(6, volume=99.0))
    snap = engine.snapshot()
    snap[0]["logs"]["ghost"] = {"results": [True], "last_ts": 999}

    engine2, _ = fresh()
    engine2.restore(snap)
    assert "ghost" not in engine2.instance("r1", UID).logs


def test_cursor_reports_last_processed_bar() -> None:
    engine, store = fresh()
    run(engine, store, history(7))
    assert engine.cursor() == {UID: 420}


# ---------------------------------------------------------------- SQLite 往返


def test_sqlite_round_trip(tmp_path: pathlib.Path) -> None:
    engine, store = fresh()
    run(engine, store, history(6, volume=99.0))

    db = tmp_path / "runtime.sqlite3"
    with RuntimeStore(db) as rs:
        rs.save_state(engine.snapshot())
    assert db.exists()

    engine2, _ = fresh()
    with RuntimeStore(db) as rs:
        assert engine2.restore(rs.load_state()) == 1
    assert engine2.snapshot() == engine.snapshot()


def test_save_state_is_idempotent(tmp_path: pathlib.Path) -> None:
    """同一 (rule, symbol) 反复保存是覆盖，不是堆积。"""
    engine, store = fresh()
    run(engine, store, history(6, volume=99.0))
    with RuntimeStore(tmp_path / "r.sqlite3") as rs:
        for _ in range(3):
            rs.save_state(engine.snapshot())
        assert len(rs.load_state()) == 1


def test_forget_rule_removes_state(tmp_path: pathlib.Path) -> None:
    engine, store = fresh()
    run(engine, store, history(6, volume=99.0))
    with RuntimeStore(tmp_path / "r.sqlite3") as rs:
        rs.save_state(engine.snapshot())
        assert rs.forget_rule("r1") == 1
        assert rs.load_state() == []


def test_schema_version_recorded(tmp_path: pathlib.Path) -> None:
    with RuntimeStore(tmp_path / "r.sqlite3") as rs:
        row = rs._conn.execute(  # noqa: SLF001
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
    assert int(row[0]) == SCHEMA_VERSION


def test_reopening_an_existing_db_keeps_data(tmp_path: pathlib.Path) -> None:
    db = tmp_path / "r.sqlite3"
    engine, store = fresh()
    run(engine, store, history(6, volume=99.0))
    with RuntimeStore(db) as rs:
        rs.save_state(engine.snapshot())
    with RuntimeStore(db) as rs:
        assert len(rs.load_state()) == 1


# ---------------------------------------------------------------- 重启（验收项）


def scenario() -> list[Bar]:
    """一段会触发的行情：5m trend 成立、放量、收阳。"""
    bars = [bar(60 * i, 101.0, volume=99.0) for i in range(1, 6)]  # 300 处 5m 收在 101
    bars += [bar(360, 101.0, volume=99.0, up=True)]  # 扳机边沿
    bars += [bar(60 * i, 101.0, volume=99.0) for i in range(7, 12)]
    return bars


def test_restart_does_not_lose_or_duplicate_signals(tmp_path: pathlib.Path) -> None:
    """M2 验收：进程重启后状态机恢复，不丢报不重报。

    做法：在信号**发生之前**（第 4 根）崩溃 —— 这样"停机期间漏掉的信号"才真实存在。
    新进程恢复状态后用 ``resume`` 重放整段历史：游标之前的只用于重建 BarStore 与指标，
    游标之后的才补判。最终落库的信号必须与全程不重启的基准逐条一致。
    """
    bars = scenario()

    baseline_engine, baseline_store = fresh()
    baseline = run(baseline_engine, baseline_store, bars)
    assert baseline, "基准场景一次都没触发，这条测试就没有说服力"

    # ---- 第一段：跑到第 4 根后存盘"崩溃"（此时还没触发）
    engine1, store1 = fresh()
    part1 = run(engine1, store1, bars[:4])
    assert part1 == [], "崩溃点应当选在信号之前，否则测不到「不丢报」"
    db = tmp_path / "runtime.sqlite3"
    with RuntimeStore(db) as rs:
        rs.save_state(engine1.snapshot())
        rs.append_signals(part1)

    # ---- 第二段：新进程，恢复后重放整段
    engine2, store2 = fresh()
    with RuntimeStore(db) as rs:
        engine2.restore(rs.load_state())
        part2 = engine2.resume(bars)
        rs.append_signals(part2)
        stored = rs.signals()

    assert part2, "停机期间的信号一条都没补回来（丢报）"
    assert [(s["fired_at"], s["dedup_key"]) for s in stored] == [
        (s.fired_at, s.dedup_key) for s in baseline
    ], "重启前后的信号与不重启的基准不一致"


def test_resume_of_already_processed_bars_emits_nothing() -> None:
    """整段历史都在游标之前 ⇒ 一条都不补判（不重报）。"""
    bars = scenario()
    engine1, store1 = fresh()
    assert run(engine1, store1, bars)

    engine2, _ = fresh()
    engine2.restore(engine1.snapshot())
    assert engine2.resume(bars) == []


def test_persisted_dedup_blocks_refire_after_restart() -> None:
    """去重键绑在大级别 bar 上时，重启后同一根 5m 内的新 1m 不得再报一次。

    这条专测**去重表的持久化**：游标之后确实有新 bar、状态机也确实会再次触发，
    唯一挡住它的就是恢复回来的去重表。
    """
    raw = dict(
        RULE,
        conditions=[
            {"on": "trend", "mode": "state", "when": "close > 100"},
            {"on": "setup", "mode": "window", "within": 3, "when": "volume > 50"},
            {"on": "trigger", "mode": "state", "when": "close > open"},
        ],
        emit={"direction": "long", "dedup_key": "{symbol}:{rule}:{trend_bar_close_ts}"},
    )

    def build() -> tuple[RuleEngine, BarStore]:
        store = BarStore(timeframes=[Timeframe.M5])
        return RuleEngine([load_rule(raw)], store), store

    warm = [bar(60 * i, 101.0, volume=99.0) for i in range(1, 6)]
    fire = [bar(360, 101.0, volume=99.0, up=True)]
    more = [bar(420, 101.0, volume=99.0, up=True)]  # 仍在 5m 桶 600 之内

    engine1, store1 = build()
    assert run(engine1, store1, warm + fire), "第一段应当触发"

    engine2, _ = build()
    engine2.restore(engine1.snapshot())
    assert engine2.resume(warm + fire + more) == [], "去重表没恢复，同一根 5m 内重报了"

    engine3, _ = build()
    snap = engine1.snapshot()
    for row in snap:
        row["seen"] = []  # 反证：抹掉去重表就会重报
    engine3.restore(snap)
    assert engine3.resume(warm + fire + more), "抹掉去重表后反而没重报，说明没测到点子上"


def test_armed_state_survives_restart() -> None:
    """崩在 armed 中间：恢复后仍是 armed，TTL 剩余根数原样保留。"""
    engine, store = fresh()
    run(engine, store, [bar(60 * i, 101.0, volume=99.0) for i in range(1, 6)])
    state = engine.instance("r1", UID).state
    assert state.phase(3) is Phase.ARMED
    ttl_before, armed_before = state.ttl_left, state.armed_at

    engine2, _ = fresh()
    engine2.restore(engine.snapshot())
    restored = engine2.instance("r1", UID).state

    assert restored.phase(3) is Phase.ARMED
    assert (restored.ttl_left, restored.armed_at) == (ttl_before, armed_before)


def test_cooldown_survives_restart() -> None:
    engine, store = fresh()
    run(engine, store, scenario()[:6])
    assert engine.instance("r1", UID).state.cooldown_until > 0

    engine2, _ = fresh()
    engine2.restore(engine.snapshot())
    assert (
        engine2.instance("r1", UID).state.cooldown_until
        == engine.instance("r1", UID).state.cooldown_until
    )


# ---------------------------------------------------------------- 信号表


def test_signals_are_deduped_by_key(tmp_path: pathlib.Path) -> None:
    """重启补喂会重复产出同一条信号，唯一约束兜底，统计不会被重复计数污染。"""
    sig = Signal(
        rule_id="r1", symbol=UID, direction=Direction.LONG, timeframe=Timeframe.M1,
        fired_at=600, trigger_price=101.0, dedup_key="k1",
        context={"close": 101.0}, role_bars={"trigger": 600},
    )
    with RuntimeStore(tmp_path / "r.sqlite3") as rs:
        assert rs.append_signals([sig]) == 1
        assert rs.append_signals([sig]) == 0
        assert rs.count_signals() == 1
        (row,) = rs.signals()
        assert row["context"] == {"close": 101.0}
        assert row["role_bars"] == {"trigger": 600}
        assert row["tentative"] is False


def test_signals_can_be_filtered(tmp_path: pathlib.Path) -> None:
    def mk(rule: str, symbol: str, ts: int) -> Signal:
        return Signal(rule_id=rule, symbol=symbol, direction=Direction.LONG,
                      timeframe=Timeframe.M1, fired_at=ts, trigger_price=1.0,
                      dedup_key=f"{rule}:{symbol}:{ts}")

    with RuntimeStore(tmp_path / "r.sqlite3") as rs:
        rs.append_signals([mk("a", UID, 60), mk("b", UID, 120), mk("a", "OTHER", 180)])
        assert len(rs.signals(rule_id="a")) == 2
        assert len(rs.signals(symbol=UID)) == 2
        assert len(rs.signals(rule_id="a", symbol=UID)) == 1
        assert [s["fired_at"] for s in rs.signals()] == [60, 120, 180]


def test_append_empty_is_noop(tmp_path: pathlib.Path) -> None:
    with RuntimeStore(tmp_path / "r.sqlite3") as rs:
        assert rs.append_signals([]) == 0


def test_in_memory_store_works() -> None:
    """单测与回测用内存库，不落磁盘。"""
    with RuntimeStore() as rs:
        rs.save_state([])
        assert rs.load_state() == []


# ---------------------------------------------------------------- 单写者


def test_second_writer_is_refused(tmp_path: pathlib.Path) -> None:
    """**同一个运行态只允许一个写者。**

    跑两个 watch，状态机、去重表、冷却各持一份内存态，同一根 bar 会被判两次 ——
    你会收到两条一模一样的预警，而且**没有任何报错**。靠人记住"别跑两个"太脆。
    """
    db = tmp_path / "rt.sqlite3"
    with RuntimeStore(db) as s:
        assert s.claim_writer(os.getpid(), 100) is None, "第一个应当拿到"
        held = s.claim_writer(1, 200)          # pid 1 一定存在
        assert held is not None and held["pid"] == os.getpid()
        assert held["started_at"] == 100, "要能告诉用户对方是什么时候起的"


def test_same_process_can_reclaim(tmp_path: pathlib.Path) -> None:
    """同一个进程重入不该把自己挡在门外（重连、重建 store 都会走到）。"""
    with RuntimeStore(tmp_path / "rt.sqlite3") as s:
        assert s.claim_writer(os.getpid(), 100) is None
        assert s.claim_writer(os.getpid(), 200) is None


def test_dead_writer_does_not_block(tmp_path: pathlib.Path) -> None:
    """**上次是被强杀的，这次要能起来。**

    这正是不用 pid 文件的原因：pid 文件崩溃后会留下，反而把自己挡在门外。
    这里存 pid，启动时验对方是不是还活着。
    """
    db = tmp_path / "rt.sqlite3"
    with RuntimeStore(db) as s:
        s.claim_writer(999_999_999, 100)       # 一个不可能存在的 pid
        assert s.claim_writer(os.getpid(), 200) is None, "死掉的写者不该挡路"


def test_release_only_removes_your_own_claim(tmp_path: pathlib.Path) -> None:
    """让出时**只删自己的** —— 别把接手的那个进程的记录抹掉。"""
    with RuntimeStore(tmp_path / "rt.sqlite3") as s:
        s.claim_writer(os.getpid(), 100)
        s.release_writer(12345)                # 不是我的，不该动
        assert s.claim_writer(1, 200) is not None, "记录被误删了"
        s.release_writer(os.getpid())
        assert s.claim_writer(1, 300) is None, "让出后别人应当能接手"


def test_watch_refuses_a_second_instance() -> None:
    """接线还在：watch.py 启动时要 claim，退出时要 release。

    真跑验证过：第二个实例退出码 2 并打印对方 pid 与启动时间；
    第一个退出后 writer 记录自动让出（DEVLOG 2026-09-02）。
    """
    src = pathlib.Path("scripts/watch.py").read_text(encoding="utf-8")
    assert "claim_writer(os.getpid()" in src
    assert "stack.callback(runtime.release_writer" in src, "退出时要让出，否则下次起不来"
    assert "serve.py" in src, "拒绝时要给出正确的替代做法（只读面板）"


# ---------------------------------------------------------------- 跨平台存活探测


def test_alive_never_calls_os_kill_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """**`os.kill(pid, 0)` 在 Windows 上会真的杀掉那个进程。**

    Python 的 `os.kill` 在 Windows 上没有"信号 0 探测"：除 CTRL_C_EVENT /
    CTRL_BREAK_EVENT 外走的是 `TerminateProcess()`。而这是生产路径 ——
    watch.py 启动时 claim_writer 会调它。库里若留着陈旧的写者记录、
    那个 pid 又被系统回收给了别的进程，启动盯盘就会杀掉一个无关进程。

    开发机是 Linux，跑不到那条分支，所以这里**证明它压根不会走到 os.kill**。
    """
    from sigdesk.store import runtime_store

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("Windows 上绝不能调用 os.kill —— 它会终止目标进程")

    monkeypatch.setattr(runtime_store.sys, "platform", "win32")
    monkeypatch.setattr(runtime_store.os, "kill", boom)
    monkeypatch.setattr(runtime_store, "_alive_windows", lambda pid: True)
    assert runtime_store._alive(12345) is True

    # pid <= 0 要在分支之前就短路掉，不该进任何平台实现
    monkeypatch.setattr(runtime_store, "_alive_windows", boom)
    assert runtime_store._alive(0) is False
    assert runtime_store._alive(-1) is False


def test_alive_on_posix_uses_signal_zero() -> None:
    """POSIX 这条路照旧：自己一定活着，一个不可能存在的 pid 一定不活。"""
    from sigdesk.store.runtime_store import _alive

    if sys.platform == "win32":  # pragma: no cover - 开发机是 Linux
        pytest.skip("这条只验 POSIX 分支")
    assert _alive(os.getpid()) is True
    assert _alive(2**22) is False


def test_alive_assumes_alive_when_the_probe_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**探测失败一律当"还活着"。** 误判成"死了"会放行第二个写者，
    那正是单写者要防的；误判成"活着"只是多问一句。"""
    from sigdesk.store import runtime_store

    if sys.platform == "win32":  # pragma: no cover
        pytest.skip("这条只验 POSIX 分支")

    def denied(*a: object, **k: object) -> None:
        raise PermissionError("不归你管")

    monkeypatch.setattr(runtime_store.os, "kill", denied)
    assert runtime_store._alive(12345) is True

    def weird(*a: object, **k: object) -> None:
        raise OSError("这平台不支持")

    monkeypatch.setattr(runtime_store.os, "kill", weird)
    assert runtime_store._alive(12345) is True


def test_source_documents_why_windows_needs_its_own_path() -> None:
    """这个坑只要有人"顺手统一一下"就会复活，把理由钉在源码里。"""
    src = pathlib.Path("src/sigdesk/store/runtime_store.py").read_text(encoding="utf-8")
    fn = src[src.index("def _alive_windows("):src.index("def _alive(")]
    assert "TerminateProcess" in fn, "要写明 Windows 上 os.kill 实际会做什么"
    assert "OpenProcess" in fn and "GetExitCodeProcess" in fn
