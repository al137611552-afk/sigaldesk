"""OKX 加密行情：REST K 线与分页回补。

与期货 Quote API 的语义**处处相反**，必须显式建模（实测 2026-08-28，见 CLAUDE.md 坑#13-#17）：

| | 期货 Quote API | OKX |
|---|---|---|
| `time_stamp` / `ts` | K 线**收盘**时刻 | K 线**开盘**时刻 |
| 收盘判据 | 本地时钟推断 `close_ts <= now` | 数据源给的 `confirm` 字段 |
| 返回顺序 | 旧→新（升序） | **新→旧（降序）** |
| 末根 | 恒为进行中 | 首根为进行中 |

与 quote_api.py 一样分两半（CONVENTIONS：纯逻辑与 IO 分离）：
``normalize_candles`` 等纯函数可脱环境单测；``OkxRestClient`` 只负责 HTTP。

**仅适用于 SWAP（永续合约）**：本模块把 `volCcy`（币量）当作 volume、`volCcyQuote`
（计价币成交额）当作 money。OKX 的 SPOT 行情里 `vol`/`volCcy` 含义与此不同，
若日后接现货必须另走一条归一化路径，不可直接复用。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Final

import aiohttp

from ..core.models import Bar, Timeframe

# Timeframe -> OKX bar 参数。注意 OKX 的小时/日线用**大写** H/D，写成小写会被拒。
OKX_BAR: Final[dict[Timeframe, str]] = {
    Timeframe.M1: "1m",
    Timeframe.M5: "5m",
    Timeframe.M15: "15m",
    Timeframe.M30: "30m",
    Timeframe.H1: "1H",
    Timeframe.H4: "4H",
    Timeframe.D1: "1D",
}

# 【实测】limit 上限 300，超出**不报错而是静默截断** —— 必须自己夹紧，否则会误以为拿全了。
MAX_LIMIT: Final = 300

# candle 数组的列位（OKX 返回的是定长字符串数组，没有字段名）
_TS, _O, _H, _L, _C, _VOL, _VOL_CCY, _VOL_QUOTE, _CONFIRM = range(9)


class OkxApiError(RuntimeError):
    pass


# ---------------------------------------------------------------- 纯逻辑部分


def normalize_candles(
    rows: list[list[str]], *, symbol: str, timeframe: Timeframe, closed_only: bool = True
) -> list[Bar]:
    """OKX candle 数组 -> Bar 列表，**按 close_ts 升序**（数据源给的是降序）。

    关键语义（实测）：
    - ``ts`` = **开盘**时刻的毫秒 epoch ⇒ ``open_ts = ts // 1000``，``close_ts = open_ts + period``
    - ``confirm`` = "1" 表示该 bar 已收盘 ⇒ 收盘判据**不依赖本地时钟**，也不依赖数组位置
      （`/market/candles` 与 `/market/history-candles` **都**会带进行中的那根，实测两者皆然）
    - 加密 7×24 无交易日概念 ⇒ ``trading_day=None``，落盘按 UTC 自然日分区
    - candle 接口不含持仓量 ⇒ ``open_interest=0.0``（需要时另查 open-interest 接口）
    """
    period = timeframe.seconds
    if period <= 0:
        raise ValueError(f"{timeframe} 不是固定长度周期，OKX 归一化暂不支持")
    out: list[Bar] = []
    for r in rows:
        closed = r[_CONFIRM] == "1"
        if closed_only and not closed:
            continue
        open_ts = int(r[_TS]) // 1000
        out.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                open_ts=open_ts,
                close_ts=open_ts + period,
                open=float(r[_O]),
                high=float(r[_H]),
                low=float(r[_L]),
                close=float(r[_C]),
                # volCcy = 币量（张数 = volume / Symbol.multiplier，multiplier 即 ctVal）
                volume=float(r[_VOL_CCY]),
                money=float(r[_VOL_QUOTE]),
                closed=closed,
                trading_day=None,
            )
        )
    out.sort(key=lambda b: b.close_ts)
    return out


def unwrap_data(payload: dict[str, Any]) -> list[list[str]]:
    """剥响应外壳。OKX 用字符串错误码，``code == "0"`` 才是成功。"""
    if payload.get("code") != "0":
        raise OkxApiError(f"OKX 返回错误 code={payload.get('code')} msg={payload.get('msg')}")
    return [list(row) for row in (payload.get("data") or [])]


def next_page_anchor(rows: list[list[str]]) -> int | None:
    """翻下一页（更旧）的锚点 = 本页最早一根的 ts。

    【实测】``after`` = 只返回 ts **严格小于**锚点的数据，因此拿本页最早 ts 当锚点，
    相邻两页恰好首尾相接（实测相邻页间隔正好一个周期，无重叠也无缺口）。
    """
    if not rows:
        return None
    return min(int(r[_TS]) for r in rows)


# ---------------------------------------------------------------- IO 部分


@dataclass(frozen=True, slots=True)
class OkxRestConfig:
    base_url: str = "https://www.okx.com"
    timeout_s: float = 20.0
    max_retries: int = 3
    # 【实测】默认 UA 会被 403 拒；必须带一个真实 UA。
    user_agent: str = "sigdesk/0.1"
    # 限流：/history-candles 官方限额 20 次/2s，留一半余量
    min_interval_s: float = 0.12
    max_pages: int = 500  # 分页保险丝，防止锚点不前进时死循环


class OkxRestClient:
    """OKX 公共行情 REST 客户端。只读公开数据，不需要 API key。"""

    def __init__(self, cfg: OkxRestConfig | None = None) -> None:
        self._cfg = cfg or OkxRestConfig()
        self._session: aiohttp.ClientSession | None = None
        self._last_call = 0.0

    async def __aenter__(self) -> OkxRestClient:
        self._session = aiohttp.ClientSession(
            base_url=self._cfg.base_url,
            headers={"User-Agent": self._cfg.user_agent},
            timeout=aiohttp.ClientTimeout(total=self._cfg.timeout_s),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, params: dict[str, Any]) -> list[list[str]]:
        if self._session is None:
            raise OkxApiError("客户端未启动，请使用 async with")
        last: Exception | None = None
        for attempt in range(self._cfg.max_retries):
            wait = self._cfg.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
            try:
                async with self._session.get(path, params=params) as resp:
                    resp.raise_for_status()
                    return unwrap_data(dict(await resp.json()))
            except (aiohttp.ClientError, TimeoutError) as e:
                last = e
                await asyncio.sleep(0.5 * 2**attempt)  # 指数退避
        raise OkxApiError(f"{path} 重试 {self._cfg.max_retries} 次仍失败: {last}") from last

    async def candles(
        self, inst_id: str, timeframe: Timeframe, limit: int = MAX_LIMIT
    ) -> list[list[str]]:
        """最近 N 根（含进行中那根）。返回原始行，降序。"""
        return await self._get(
            "/api/v5/market/candles",
            {"instId": inst_id, "bar": OKX_BAR[timeframe], "limit": min(limit, MAX_LIMIT)},
        )

    async def history_candles(
        self,
        inst_id: str,
        timeframe: Timeframe,
        *,
        after_ms: int | None = None,
        before_ms: int | None = None,
        limit: int = MAX_LIMIT,
    ) -> list[list[str]]:
        """历史 K 线。``after_ms`` 取**更旧**的、``before_ms`` 取**更新**的（实测确认，别搞反）。"""
        params: dict[str, Any] = {
            "instId": inst_id,
            "bar": OKX_BAR[timeframe],
            "limit": min(limit, MAX_LIMIT),
        }
        if after_ms is not None:
            params["after"] = after_ms
        if before_ms is not None:
            params["before"] = before_ms
        return await self._get("/api/v5/market/history-candles", params)

    async def fetch_range(
        self, inst_id: str, symbol: str, timeframe: Timeframe, start_ts: int, end_ts: int
    ) -> list[Bar]:
        """回补 ``start_ts < close_ts <= end_ts`` 的已收盘 bar，升序。

        区间约定与 ``store.parquet_io.read_range`` 一致（左开右闭），便于缺口回补直接对接。
        """
        period = timeframe.seconds
        anchor: int | None = end_ts * 1000  # after 取严格更旧的 ⇒ 首页最新一根 close_ts == end_ts
        by_close: dict[int, Bar] = {}
        for _ in range(self._cfg.max_pages):
            rows = await self.history_candles(inst_id, timeframe, after_ms=anchor)
            if not rows:
                break
            for bar in normalize_candles(rows, symbol=symbol, timeframe=timeframe):
                if start_ts < bar.close_ts <= end_ts:
                    by_close[bar.close_ts] = bar
            nxt = next_page_anchor(rows)
            if nxt is None or (anchor is not None and nxt >= anchor):
                break  # 锚点不再前进，防死循环
            anchor = nxt
            if nxt // 1000 - period <= start_ts:
                break  # 已翻过区间左端
        return [by_close[t] for t in sorted(by_close)]


__all__ = [
    "MAX_LIMIT",
    "OKX_BAR",
    "OkxApiError",
    "OkxRestClient",
    "OkxRestConfig",
    "next_page_anchor",
    "normalize_candles",
    "unwrap_data",
]
