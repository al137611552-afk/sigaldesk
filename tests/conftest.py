from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from sigdesk.core import env as env_module

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

Raw = dict[str, list[dict[str, Any]]]


@pytest.fixture(autouse=True)
def _isolate_user_env(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**测试绝不能碰开发机上真实的 `~/.signal-desk/.env`。**

    `candidate_paths` 的最后一档是模块级常量 `USER_ENV`，指向真实家目录。
    不隔离的话有两个后果，第二个更糟：

    1. 断言"没找到 .env"的测试在**配过凭据的机器上必然失败** ——
       用户的 Windows 机器上就是这么红的，而代码本身没问题。
    2. **测试会把真实凭据读进 `os.environ`**。跑个测试就把自己的 key
       灌进进程环境，这是不该发生的事。

    `SIGDESK_ENV` 一并清掉 —— 开发者 shell 里若设了它，测试结果就随机器而变。
    """
    fake = tmp_path_factory.mktemp("fake-home") / ".signal-desk" / ".env"
    monkeypatch.setattr(env_module, "USER_ENV", fake)
    monkeypatch.delenv("SIGDESK_ENV", raising=False)


@pytest.fixture(scope="session")
def _rb2610_fixture() -> dict[str, Any]:
    return dict(json.loads((FIXTURES / "rb2610_bycount.json").read_text()))


@pytest.fixture(scope="session")
def rb2610_archived(_rb2610_fixture: dict[str, Any]) -> Raw:
    """rb2610 归档区间（当日 00:00 之前）的 1m/5m/15m/1h，同批拉取、同源。

    归档数据在数据源内部自洽，因此对拍要求**逐根精确一致**（含成交量）。
    """
    return dict(_rb2610_fixture["archived"])


@pytest.fixture(scope="session")
def rb2610_intraday(_rb2610_fixture: dict[str, Any]) -> Raw:
    """rb2610 当日盘中区间。

    留证用：数据源当日数据自身不自洽（实测 08-28 11:15 的 1m 线 close=3129/V=6855，
    而同源 5m/15m 线 close=3128 且总量少 1 手 —— 收盘那一秒的成交归属不同）。
    """
    return dict(_rb2610_fixture["intraday"])


@pytest.fixture(scope="session")
def btc_swap_okx() -> dict[str, Any]:
    """BTC-USDT-SWAP 归档段夹具：同一时间窗（对齐到整点）的 1m/5m/15m/1H。

    全部为 confirm=1 的已收盘 bar，且 1m 段恰好覆盖整数个 1h 桶 ——
    因此聚合对拍可要求逐根精确一致，不需要处理半截桶。
    """
    return dict(json.loads((FIXTURES / "btcusdt_swap_okx.json").read_text()))


@pytest.fixture(scope="session")
def btc_swap_okx_ws() -> dict[str, Any]:
    """同一根 bar 的 WS(confirm=1) 原始消息 与 REST 原始行，2026-08-28 实抓配对。

    留证：两者数值相同但**字符串格式不同**（WS "79382.0" / REST "79382"，
    以及 volCcyQuote 的尾随零）—— 任何按字符串比对的校验都会误报差异。
    """
    return dict(json.loads((FIXTURES / "btcusdt_swap_okx_ws.json").read_text()))
