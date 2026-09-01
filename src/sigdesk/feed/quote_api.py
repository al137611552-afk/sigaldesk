"""Quote API 客户端。

分为两半（CONVENTIONS：纯逻辑与 IO 分离）：
- ``normalize_klines`` 等纯函数：把接口原始 JSON 转成内部 Bar，全部可脱环境单测。
- ``QuoteApiClient``：只负责 HTTP，薄壳。

接口分工（实测，见 CLAUDE.md 坑#10）：
- ``kline_by_count``     唯一能取到**当日盘中**数据（≤2000 根，不支持复权）→ 实时轮询用
- ``kline_by_timerange`` **不含当日**数据，支持复权、无数量限制 → 历史回补/权威校正用
"""

from __future__ import annotations

import asyncio
import hashlib
import ssl
from dataclasses import dataclass
from typing import Any

import aiohttp

from ..core.calendar import MarketCalendar
from ..core.models import QUOTE_API_INTERVAL, Bar, Timeframe
from ..core.timeframes import is_closed

# ---------------------------------------------------------------- 纯逻辑部分


def normalize_klines(
    rows: list[dict[str, Any]],
    *,
    symbol: str,
    timeframe: Timeframe,
    now_ts: int,
    calendar: MarketCalendar | None = None,
) -> list[Bar]:
    """接口 K 线 JSON -> Bar 列表。

    关键语义（实测，见 docs/ARCHITECTURE.md §3.2）：
    - 分钟/小时线：``time_stamp`` = **收盘时刻**的 UTC epoch ⇒ open_ts = close_ts - period
    - 日线：``time_stamp`` = **交易日的 UTC 零点**（纯日期编码）⇒ 走独立路径
    - 末根恒为进行中 bar ⇒ 由 ``is_closed(close_ts, now_ts)`` 判定，不靠位置
    """
    if timeframe is Timeframe.D1:
        return _normalize_daily(rows, symbol=symbol, now_ts=now_ts)

    period = timeframe.seconds
    out: list[Bar] = []
    for r in rows:
        close_ts = int(r["time_stamp"])
        out.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                open_ts=close_ts - period,
                close_ts=close_ts,
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r.get("volume", 0.0)),
                money=float(r.get("money", 0.0)),
                open_interest=float(r.get("open_interest", 0.0)),
                closed=is_closed(close_ts, now_ts),
                trading_day=calendar.trading_day(close_ts) if calendar else None,
            )
        )
    return out


def _normalize_daily(rows: list[dict[str, Any]], *, symbol: str, now_ts: int) -> list[Bar]:
    """日线：time_stamp 是交易日的 UTC 零点，不是收盘时刻。

    open_ts/close_ts 用该交易日的 [00:00, 24:00) UTC 名义区间表示，
    真实收盘时刻由 trading_day 承载 —— 日线不参与墙钟分桶，故名义区间足够。
    """
    import datetime as dt

    out: list[Bar] = []
    for r in rows:
        day_ts = int(r["time_stamp"])
        day = dt.datetime.fromtimestamp(day_ts, dt.UTC).date().isoformat()
        out.append(
            Bar(
                symbol=symbol,
                timeframe=Timeframe.D1,
                open_ts=day_ts,
                close_ts=day_ts + 86400,
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r.get("volume", 0.0)),
                money=float(r.get("money", 0.0)),
                open_interest=float(r.get("open_interest", 0.0)),
                closed=day_ts + 86400 <= now_ts,
                trading_day=day,
            )
        )
    return out


def unwrap_payload(
    payload: dict[str, Any], *, requested_code: str | None = None
) -> list[dict[str, Any]]:
    """剥掉响应外壳。单品种时 data 直接是 K 线数组；多品种时是 [{code, klines}]。

    data 为 null 是合法的"无数据"（例：by-timerange 查当日区间），返回空列表。
    """
    if payload.get("code") != 0:
        raise QuoteApiError(f"接口返回错误 code={payload.get('code')} msg={payload.get('msg')}")
    data = payload.get("data")
    if not data:
        return []
    if isinstance(data[0], dict) and "klines" in data[0]:
        for item in data:
            if requested_code is None or item.get("code") == requested_code:
                return list(item.get("klines") or [])
        return []
    return list(data)


class QuoteApiError(RuntimeError):
    pass


# ---------------------------------------------------------------- IO 部分


@dataclass(frozen=True, slots=True)
class QuoteApiConfig:
    base_url: str
    api_key: str
    tls_fingerprint: str = ""  # sha256 十六进制；为空且未显式放行则拒绝连接
    allow_insecure_tls: bool = False
    timeout_s: float = 30.0
    max_retries: int = 3


def fetch_tls_fingerprint(host: str, port: int = 443) -> str:
    """抓取服务端证书的 sha256 指纹，用于首次写入 .env 的 QUOTE_API_TLS_FINGERPRINT。

    这是**引导步骤**，本身不受保护 —— 应在可信网络下执行一次，之后固定比对。
    """
    der = ssl.get_server_certificate((host, port))
    from ssl import PEM_cert_to_DER_cert

    return hashlib.sha256(PEM_cert_to_DER_cert(der)).hexdigest()


class QuoteApiClient:
    """异步客户端。自签名证书 -> 用指纹固定，不做全局 verify=False（ADR-0002）。"""

    def __init__(self, cfg: QuoteApiConfig) -> None:
        self._cfg = cfg
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> QuoteApiClient:
        if self._cfg.tls_fingerprint:
            ssl_opt: Any = aiohttp.Fingerprint(bytes.fromhex(self._cfg.tls_fingerprint))
        elif self._cfg.allow_insecure_tls:
            ssl_opt = False
        else:
            raise QuoteApiError(
                "未配置 QUOTE_API_TLS_FINGERPRINT。请先运行 scripts/pin_tls.py 写入指纹，"
                "或显式设置 allow_insecure_tls=True（不推荐：请求头带着 AK）。"
            )
        self._session = aiohttp.ClientSession(
            base_url=self._cfg.base_url,
            headers={"Authorization": f"Bearer {self._cfg.api_key}"},
            timeout=aiohttp.ClientTimeout(total=self._cfg.timeout_s),
            connector=aiohttp.TCPConnector(ssl=ssl_opt),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            raise QuoteApiError("客户端未启动，请使用 async with")
        last: Exception | None = None
        for attempt in range(self._cfg.max_retries):
            try:
                async with self._session.post(path, json=body) as resp:
                    resp.raise_for_status()
                    return dict(await resp.json())
            except (aiohttp.ClientError, TimeoutError) as e:
                last = e
                await asyncio.sleep(0.5 * 2**attempt)  # 指数退避
        raise QuoteApiError(f"{path} 重试 {self._cfg.max_retries} 次仍失败: {last}") from last

    async def kline_by_count(
        self, quote_code: str, timeframe: Timeframe, count: int
    ) -> list[dict[str, Any]]:
        """最近 N 根。**唯一能取当日盘中数据的接口**，上限 2000 根，不支持复权。"""
        payload = await self._post(
            "/api/v1/kline/by-count",
            {
                "variety_code": quote_code,
                "interval_range": QUOTE_API_INTERVAL[timeframe],
                "count": min(count, 2000),
            },
        )
        return unwrap_payload(payload, requested_code=quote_code)

    async def kline_by_timerange(
        self,
        quote_code: str,
        timeframe: Timeframe,
        start_ts: int,
        end_ts: int,
        adjust_type: int = 0,
    ) -> list[dict[str, Any]]:
        """按时间范围。**不含当日数据**（实测截止到当日 00:00 之前），支持复权。"""
        payload = await self._post(
            "/api/v1/kline/by-timerange",
            {
                "variety_code": quote_code,
                "interval_range": QUOTE_API_INTERVAL[timeframe],
                "start_time": start_ts,
                "end_time": end_ts,
                "adjust_type": adjust_type,
            },
        )
        return unwrap_payload(payload, requested_code=quote_code)

    async def main_by_date(
        self, product_codes: list[str], start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """历史主力合约及其生效区间。

        注意：``GET /api/v1/varieties/main`` 当前服务端返回 500（CLAUDE.md 坑#4），
        当前主力也从本接口查（把 end_date 设为今天）。
        """
        payload = await self._post(
            "/api/v1/varieties/main-by-date",
            {"start_time": start_date, "end_time": end_date, "main_variety_codes": product_codes},
        )
        if payload.get("code") != 0:
            raise QuoteApiError(f"main-by-date 失败: {payload.get('msg')}")
        return list(payload.get("data") or [])

    async def search(
        self, keyword: str = "", exchange_code: str = "", category_type: int | None = None
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {}
        if keyword:
            body["keyword"] = keyword
        if exchange_code:
            body["exchange_code"] = exchange_code
        if category_type is not None:
            body["category_type"] = category_type
        payload = await self._post("/api/v1/varieties/search", body)
        if payload.get("code") != 0:
            raise QuoteApiError(f"search 失败: {payload.get('msg')}")
        return list(payload.get("data") or [])


__all__ = [
    "QuoteApiClient",
    "QuoteApiConfig",
    "QuoteApiError",
    "fetch_tls_fingerprint",
    "normalize_klines",
    "unwrap_payload",
]
