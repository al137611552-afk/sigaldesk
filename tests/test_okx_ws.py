"""OKX WS Feed 单测。连接被抽象成协议，因此重连与回补全部脱网验证。"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from sigdesk.core.models import Bar, Market, Symbol, Timeframe
from sigdesk.feed import okx_ws
from sigdesk.feed.okx import OkxApiError, OkxRestClient, OkxRestConfig, normalize_candles
from sigdesk.feed.okx_ws import (
    OkxWsFeed,
    WsConnection,
    parse_push,
    plan_backfill,
    reconnect_delay,
    subscribe_payload,
)

INST = "BTC-USDT-SWAP"
UID = "CRYPTO.OKX.BTCUSDT.PERP"
SYM = Symbol(uid=UID, market=Market.CRYPTO, exchange="OKX", code=INST, calendar="crypto_24x7")


def push(*rows: list[str]) -> str:
    return json.dumps({"arg": {"channel": "candle1m", "instId": INST}, "data": list(rows)})


class _FakeConn:
    """按脚本吐消息；脚本里放异常就在那一步抛出。"""

    def __init__(self, script: Sequence[str | BaseException]) -> None:
        self._script = list(script)
        self.sent: list[str] = []

    async def send_str(self, data: str) -> None:
        self.sent.append(data)

    async def receive_str(self, timeout: float) -> str:
        if not self._script:
            raise ConnectionError("脚本放完，模拟对端关闭")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def connector(*conns: _FakeConn) -> okx_ws.Connector:
    it = iter(conns)

    @contextlib.asynccontextmanager
    async def connect() -> AsyncIterator[WsConnection]:
        yield next(it)

    return connect


class _FakeRest(OkxRestClient):
    """回补桩：从给定的全量 bar 里按区间切，并记录被请求的区间。"""

    def __init__(self, universe: list[Bar]) -> None:
        super().__init__(OkxRestConfig(min_interval_s=0.0))
        self._universe = universe
        self.asked: list[tuple[int, int]] = []

    async def fetch_range(  # type: ignore[override]
        self, inst_id: str, symbol: str, timeframe: Timeframe, start_ts: int, end_ts: int
    ) -> list[Bar]:
        self.asked.append((start_ts, end_ts))
        return [b for b in self._universe if start_ts < b.close_ts <= end_ts]


async def drain(feed: OkxWsFeed) -> list[Bar]:
    return [bar async for bar in feed.stream()]


# ------------------------------------------------------------------ 纯函数


def test_subscribe_payload_targets_candle_channel() -> None:
    body = json.loads(subscribe_payload([INST], Timeframe.M1))
    assert body == {"op": "subscribe", "args": [{"channel": "candle1m", "instId": INST}]}


def test_parse_push_ignores_pong_and_ack() -> None:
    """心跳回执是裸字符串 "pong"（非 JSON），订阅回执没有 data —— 都不是行情。"""
    assert parse_push("pong") is None
    assert parse_push(json.dumps({"event": "subscribe", "arg": {"channel": "candle1m"}})) is None


def test_parse_push_raises_on_error_event() -> None:
    """订错端点会返回 60018；静默忽略就会变成"连着却永远没数据"。"""
    with pytest.raises(OkxApiError, match="60018"):
        parse_push(json.dumps({"event": "error", "code": "60018", "msg": "wrong URL or channel"}))


@pytest.mark.parametrize(
    ("last", "nxt", "expected"),
    [
        (None, 1000, None),  # 首根：没有基准，不回补
        (940, 1000, None),  # 连续
        (880, 1000, (880, 940)),  # 漏 1 根
        (700, 1000, (700, 940)),  # 漏 4 根
    ],
)
def test_plan_backfill(last: int | None, nxt: int, expected: tuple[int, int] | None) -> None:
    """右端是 nxt-period：WS 已带回 nxt 那根，不重复拉。"""
    assert plan_backfill(last, nxt, 60) == expected


def test_reconnect_delay_backs_off_and_caps() -> None:
    assert [reconnect_delay(i) for i in range(4)] == [1.0, 2.0, 4.0, 8.0]
    assert reconnect_delay(99) == okx_ws.RECONNECT_CAP_S


# ------------------------------------------- 验收：WS 与 REST 同一根 bar 完全一致


def test_ws_and_rest_bar_are_identical(btc_swap_okx_ws: dict[str, Any]) -> None:
    """M0-B 验收项。两侧原始**字符串**不同（"79382.0" vs "79382"），归一化后必须逐字段相同。"""
    ws_row = json.loads(btc_swap_okx_ws["ws_closed_msg"])["data"][0]
    rest_row = btc_swap_okx_ws["rest_row"]
    assert ws_row != rest_row, "夹具应保留字符串格式差异，否则这条测试就没意义了"

    (from_ws,) = normalize_candles([ws_row], symbol=UID, timeframe=Timeframe.M1)
    (from_rest,) = normalize_candles([rest_row], symbol=UID, timeframe=Timeframe.M1)
    assert from_ws == from_rest


# ------------------------------------------------------------------ Feed 行为


async def test_only_closed_bars_are_emitted(btc_swap_okx: dict[str, Any]) -> None:
    """进行中的 bar 每分钟被推 30~80 次，一根都不许漏出去（INV-2）。"""
    closed_row, *_ = btc_swap_okx["1m"]
    open_row = [*closed_row[:-1], "0"]
    conn = _FakeConn([push(open_row), push(open_row), push(closed_row)])
    feed = OkxWsFeed([SYM], connect=connector(conn), max_reconnects=0)

    bars = await drain(feed)

    assert [b.closed for b in bars] == [True]
    assert len(bars) == 1


async def test_duplicate_pushes_are_deduped(btc_swap_okx: dict[str, Any]) -> None:
    rows = list(reversed(btc_swap_okx["1m"][:3]))  # 升序三根
    conn = _FakeConn([push(rows[0]), push(rows[0]), push(rows[1]), push(rows[0]), push(rows[2])])
    feed = OkxWsFeed([SYM], connect=connector(conn), max_reconnects=0)

    bars = await drain(feed)

    assert [b.close_ts for b in bars] == [normalize_candles([r], symbol=UID,
            timeframe=Timeframe.M1)[0].close_ts for r in rows]


async def test_unsubscribed_inst_is_ignored(btc_swap_okx: dict[str, Any]) -> None:
    row = btc_swap_okx["1m"][0]
    msg = json.dumps({"arg": {"channel": "candle1m", "instId": "ETH-USDT-SWAP"}, "data": [row]})
    feed = OkxWsFeed([SYM], connect=connector(_FakeConn([msg])), max_reconnects=0)
    assert await drain(feed) == []


async def test_ping_is_sent_on_silence() -> None:
    """OKX 30s 无往来即断开；静默时必须主动发裸字符串 "ping"。"""
    conn = _FakeConn([TimeoutError(), TimeoutError()])
    feed = OkxWsFeed([SYM], connect=connector(conn), max_reconnects=0)

    await drain(feed)

    assert conn.sent[0].startswith('{"op": "subscribe"'), "首条必须是订阅"
    assert conn.sent[1:] == ["ping", "ping"]


async def test_reconnect_backfills_gap_without_dup_or_loss(
    btc_swap_okx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """M0-B 验收项：断线重连后自动回补，无重复无缺失。"""
    monkeypatch.setattr(okx_ws, "reconnect_delay", lambda attempt: 0.0)
    rows = list(reversed(btc_swap_okx["1m"][:6]))  # 升序 6 根
    universe = normalize_candles(rows, symbol=UID, timeframe=Timeframe.M1)
    rest = _FakeRest(universe)

    # 第一路连接给出前两根后断开；第二路直接跳到第 6 根，中间 3 根靠 REST 补
    conn1 = _FakeConn([push(rows[0]), push(rows[1]), ConnectionError("网络抖动")])
    conn2 = _FakeConn([push(rows[5])])
    feed = OkxWsFeed([SYM], rest=rest, connect=connector(conn1, conn2), max_reconnects=1)

    bars = await drain(feed)

    assert [b.close_ts for b in bars] == [b.close_ts for b in universe], "回补后应完全连续"
    assert feed.reconnects == 1
    assert rest.asked == [(universe[1].close_ts, universe[4].close_ts)], "只补断档区间"
    assert feed.backfilled == [(UID, universe[1].close_ts, universe[4].close_ts)]


async def test_reconnect_without_gap_does_not_call_rest(btc_swap_okx: dict[str, Any],
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """断线但没漏 bar 时不该白拉一次 REST。"""
    monkeypatch.setattr(okx_ws, "reconnect_delay", lambda attempt: 0.0)
    rows = list(reversed(btc_swap_okx["1m"][:3]))
    rest = _FakeRest(normalize_candles(rows, symbol=UID, timeframe=Timeframe.M1))
    conn1 = _FakeConn([push(rows[0]), push(rows[1]), ConnectionError("抖动")])
    conn2 = _FakeConn([push(rows[2])])
    feed = OkxWsFeed([SYM], rest=rest, connect=connector(conn1, conn2), max_reconnects=1)

    bars = await drain(feed)

    assert len(bars) == 3
    assert rest.asked == []


async def test_missing_inst_id_is_rejected_at_construction() -> None:
    bad = Symbol(uid=UID, market=Market.CRYPTO, exchange="OKX", code="", calendar="crypto_24x7")
    with pytest.raises(ValueError, match="instId"):
        OkxWsFeed([bad])


async def test_resume_from_backfills_the_downtime_gap(
    btc_swap_okx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """进程重启：用已落盘位置播种游标，停机期间的缺口按断线同一条路径自动补齐。

    不播种的话游标为空，第一根到达时判不出漏了什么 —— 缺口只能靠人工回补。
    """
    monkeypatch.setattr(okx_ws, "reconnect_delay", lambda attempt: 0.0)
    rows = list(reversed(btc_swap_okx["1m"][:6]))
    universe = normalize_candles(rows, symbol=UID, timeframe=Timeframe.M1)
    rest = _FakeRest(universe)

    # 假设进程停机前已落盘到第 2 根，重启后 WS 第一条就是第 6 根
    feed = OkxWsFeed(
        [SYM],
        rest=rest,
        connect=connector(_FakeConn([push(rows[5])])),
        max_reconnects=0,
        resume_from={UID: universe[1].close_ts},
    )

    bars = await drain(feed)

    assert [b.close_ts for b in bars] == [b.close_ts for b in universe[2:]]
    assert rest.asked == [(universe[1].close_ts, universe[4].close_ts)]


async def test_without_resume_from_first_bar_triggers_no_backfill(
    btc_swap_okx: dict[str, Any],
) -> None:
    """没播种时首根不回补 —— 这是正确行为（无从得知漏了什么），但要显式钉住，
    免得把"重启没补上"当成 bug 去改 plan_backfill。"""
    rows = list(reversed(btc_swap_okx["1m"][:6]))
    rest = _FakeRest(normalize_candles(rows, symbol=UID, timeframe=Timeframe.M1))
    feed = OkxWsFeed([SYM], rest=rest, connect=connector(_FakeConn([push(rows[5])])),
                     max_reconnects=0)

    assert len(await drain(feed)) == 1
    assert rest.asked == []


async def test_gap_without_rest_client_is_recorded_not_swallowed(
    btc_swap_okx: dict[str, Any],
) -> None:
    """没配 REST 就补不了缺口，但**必须留下记录** —— 静默丢弃缺口是最难查的一类问题。"""
    rows = list(reversed(btc_swap_okx["1m"][:6]))
    universe = normalize_candles(rows, symbol=UID, timeframe=Timeframe.M1)
    feed = OkxWsFeed(
        [SYM], connect=connector(_FakeConn([push(rows[0]), push(rows[5])])), max_reconnects=0
    )

    bars = await drain(feed)

    assert [b.close_ts for b in bars] == [universe[0].close_ts, universe[5].close_ts]
    assert feed.gaps_detected == [(UID, universe[0].close_ts, universe[4].close_ts)]
    assert feed.backfilled == []


async def test_backoff_grows_when_connection_dies_before_any_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"连上即断"时退避必须继续增长，否则会变成猛敲对端的热循环。"""
    attempts: list[int] = []

    def record(attempt: int) -> float:
        attempts.append(attempt)
        return 0.0

    monkeypatch.setattr(okx_ws, "reconnect_delay", record)
    dead = [_FakeConn([]) for _ in range(4)]  # 每一路连上就断，一条数据都没有
    feed = OkxWsFeed([SYM], connect=connector(*dead), max_reconnects=3)

    assert await drain(feed) == []
    assert attempts == [0, 1, 2], "退避指数应递增，而不是每次重连都归零"
