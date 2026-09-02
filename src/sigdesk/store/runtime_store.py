"""运行态持久化：SQLite（WAL）。ADR-0004 选型，ARCHITECTURE §6。

存三样东西：
- ``rule_state``：状态机 + 条件日志 + 去重表，一个 (rule_id, symbol) 一行。
- ``signal``：已发出的信号，供 M3 的统计与回看。
- ``meta``：schema 版本等。

**只存"重算不出来的东西"**。指标缓存不存 —— 它可以由 BarStore 的历史重新喂出来；
存进去只会让快照又大又易腐。K 线也不存这里，它在 Parquet（ADR-0004）。

进程重启后的恢复顺序（"不丢报不重报"的实现，见 scripts/watch.py）：

1. 从本库恢复状态机、条件日志、去重表；
2. 把历史 bar 装回 BarStore —— 指标会在首次用到时整段回放自行预热；
3. 只把**游标之后**的新 bar 喂给引擎。已处理过的 bar 即使重复喂也会被去重表挡住。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from types import TracebackType
from typing import Any

from ..rules.model import Signal

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_state (
    rule_id        TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    stage          INTEGER NOT NULL,
    ttl_left       INTEGER NOT NULL,
    cooldown_until INTEGER NOT NULL,
    armed_at       INTEGER NOT NULL,
    last_fired_ts  INTEGER NOT NULL,
    logs           TEXT    NOT NULL,  -- JSON: 角色 -> {results, last_ts}
    seen           TEXT    NOT NULL,  -- JSON: 已发去重键，按时间先后
    PRIMARY KEY (rule_id, symbol)
);
CREATE TABLE IF NOT EXISTS signal (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id       TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    direction     TEXT    NOT NULL,
    timeframe     TEXT    NOT NULL,
    fired_at      INTEGER NOT NULL,
    trigger_price REAL    NOT NULL,
    dedup_key     TEXT    NOT NULL,
    context       TEXT    NOT NULL,
    role_bars     TEXT    NOT NULL,
    tentative     INTEGER NOT NULL,
    priority      TEXT    NOT NULL,
    trading_day   TEXT,
    UNIQUE (rule_id, symbol, dedup_key)
);
CREATE INDEX IF NOT EXISTS signal_by_time ON signal (fired_at);
CREATE TABLE IF NOT EXISTS trade_state (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fill (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_key TEXT    NOT NULL,
    symbol     TEXT    NOT NULL,
    side       TEXT    NOT NULL,
    qty        REAL    NOT NULL,
    price      REAL    NOT NULL,
    ts         INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    fee        REAL    NOT NULL,
    realized   REAL    NOT NULL,
    -- 一笔成交由「哪条信号 + 什么性质 + 哪根 bar」唯一确定。
    -- 重启补喂会重复产出同一批 bar，靠这个约束兜底，账不会被重复计。
    UNIQUE (signal_key, kind, ts)
);
CREATE INDEX IF NOT EXISTS fill_by_time ON fill (ts);
-- 预警组里被「钉住」的标的。**这是预警组唯一的持久状态** ——
-- 组里其余的格子由最近的信号算出来（见 web/watchlist.py），不落库。
-- 放这里而不是浏览器：钉住会让盯盘进程开始采集这个标的（scripts/watch.py 要读它），
-- 存在 localStorage 里的话采集端根本看不见。
CREATE TABLE IF NOT EXISTS watchlist_pin (
    symbol    TEXT    PRIMARY KEY,
    pinned_at INTEGER NOT NULL
);
"""


class RuntimeStore:
    """薄薄一层 SQLite。引擎给出快照，这里只负责存取，不理解快照的语义。

    **线程安全**：Web 与引擎同进程时，FastAPI 会把同步端点丢进线程池执行，
    而 sqlite3 连接默认绑定创建它的线程。所以这里 ``check_same_thread=False``
    并用一把锁把所有语句串行化 —— 连接对象本身不是线程安全的，
    WAL 只解决进程/连接之间的并发，解决不了同一个连接被多线程同时使用。
    本项目是单写者、低频访问，串行化的代价可以忽略。
    """

    def __init__(self, path: pathlib.Path | str = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            pathlib.Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")  # 崩溃后不丢已提交的事务
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def __enter__(self) -> RuntimeStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ 运行态

    def save_state(self, rows: Sequence[dict[str, Any]]) -> None:
        """整体覆盖写。规则状态量很小（一条规则一个标的一行），不值得做增量。"""
        with self._lock:
            self._save_state(rows)

    def _save_state(self, rows: Sequence[dict[str, Any]]) -> None:
        self._conn.executemany(
            """INSERT INTO rule_state
                 (rule_id, symbol, stage, ttl_left, cooldown_until, armed_at, last_fired_ts,
                  logs, seen)
               VALUES (:rule_id, :symbol, :stage, :ttl_left, :cooldown_until, :armed_at,
                       :last_fired_ts, :logs, :seen)
               ON CONFLICT (rule_id, symbol) DO UPDATE SET
                 stage=excluded.stage, ttl_left=excluded.ttl_left,
                 cooldown_until=excluded.cooldown_until, armed_at=excluded.armed_at,
                 last_fired_ts=excluded.last_fired_ts, logs=excluded.logs, seen=excluded.seen""",
            [
                {
                    "rule_id": r["rule_id"],
                    "symbol": r["symbol"],
                    "stage": r["stage"],
                    "ttl_left": r["ttl_left"],
                    "cooldown_until": r["cooldown_until"],
                    "armed_at": r["armed_at"],
                    "last_fired_ts": r["last_fired_ts"],
                    "logs": json.dumps(r.get("logs") or {}),
                    "seen": json.dumps(r.get("seen") or []),
                }
                for r in rows
            ],
        )
        self._conn.commit()

    def load_state(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM rule_state ORDER BY rule_id, symbol"
            ).fetchall()
        return [
            {
                **{k: row[k] for k in
                   ("rule_id", "symbol", "stage", "ttl_left", "cooldown_until",
                    "armed_at", "last_fired_ts")},
                "logs": json.loads(row["logs"]),
                "seen": json.loads(row["seen"]),
            }
            for row in rows
        ]

    def forget_rule(self, rule_id: str) -> int:
        """规则下线时清掉它的残留状态。留着的话改名重用会诈尸。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM rule_state WHERE rule_id = ?", (rule_id,))
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------ 信号

    # ------------------------------------------------------------ 预警组

    def pins(self) -> list[str]:
        """钉住的标的，按钉住先后。顺序就是它们在预警组里的排位。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol FROM watchlist_pin ORDER BY pinned_at, symbol"
            ).fetchall()
        return [str(r["symbol"]) for r in rows]

    def pin(self, symbol: str, ts: int) -> bool:
        """钉住。已钉住则**保持原来的时间**，不往后挪 —— 重复点一下不该改变排位。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO watchlist_pin (symbol, pinned_at) VALUES (?, ?)",
                (symbol, int(ts)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def unpin(self, symbol: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM watchlist_pin WHERE symbol = ?", (symbol,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------ 信号

    def append_signals(self, signals: Iterable[Signal]) -> int:
        """落信号。同一 (rule, symbol, dedup_key) 只留一条 ——
        重启补喂时会重复产出同一条信号，靠这个唯一约束兜底，统计不会被重复计数污染。"""
        rows = [
            (
                s.rule_id, s.symbol, str(s.direction), str(s.timeframe), s.fired_at,
                s.trigger_price, s.dedup_key, json.dumps(s.context), json.dumps(s.role_bars),
                int(s.tentative), str(s.priority), s.trading_day,
            )
            for s in signals
        ]
        if not rows:
            return 0
        with self._lock:
            return self._insert_signals(rows)

    def _insert_signals(self, rows: list[tuple[Any, ...]]) -> int:
        cur = self._conn.executemany(
            """INSERT OR IGNORE INTO signal
                 (rule_id, symbol, direction, timeframe, fired_at, trigger_price, dedup_key,
                  context, role_bars, tentative, priority, trading_day)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        self._conn.commit()
        return cur.rowcount

    def signals(
        self, rule_id: str | None = None, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM signal"
        where, params = [], []
        if rule_id:
            where.append("rule_id = ?")
            params.append(rule_id)
        if symbol:
            where.append("symbol = ?")
            params.append(symbol)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY fired_at, rule_id, symbol"
        with self._lock:
            fetched = self._conn.execute(sql, params).fetchall()
        return [
            {
                **dict(row),
                "context": json.loads(row["context"]),
                "role_bars": json.loads(row["role_bars"]),
                "tentative": bool(row["tentative"]),
            }
            for row in fetched
        ]

    # ------------------------------------------------------------ 交易

    def save_trade_state(self, snapshot: dict[str, Any]) -> None:
        """整体覆盖写。纸上账户很小（几个持仓 + 一张去重表），不值得做增量。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO trade_state (id, snapshot) VALUES (1, ?) "
                "ON CONFLICT (id) DO UPDATE SET snapshot = excluded.snapshot",
                (json.dumps(snapshot),),
            )
            self._conn.commit()

    def load_trade_state(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT snapshot FROM trade_state WHERE id = 1").fetchone()
        return dict(json.loads(row[0])) if row else {}

    def append_fills(self, fills: Iterable[Any]) -> int:
        """落成交。同一 (信号, 性质, bar) 只留一条 —— 重启补喂不会把账重复计。"""
        rows = [
            (f.signal_key, f.symbol, str(f.side), f.qty, f.price, f.ts, str(f.kind),
             f.fee, f.realized)
            for f in fills
        ]
        if not rows:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                "INSERT OR IGNORE INTO fill (signal_key, symbol, side, qty, price, ts, kind,"
                " fee, realized) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
            return cur.rowcount

    def fills(self, symbol: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        sql = "SELECT * FROM fill"
        params: list[Any] = []
        if symbol:
            sql += " WHERE symbol = ?"
            params.append(symbol)
        sql += " ORDER BY ts, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows][-limit:]

    def count_fills(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM fill").fetchone()[0])

    def count_signals(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0])


__all__ = ["SCHEMA_VERSION", "RuntimeStore"]
