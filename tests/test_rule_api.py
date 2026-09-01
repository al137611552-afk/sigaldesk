"""规则 CRUD 与历史试算端点（FR-5.3）。

写端点默认关闭是**安全边界**，不是配置偏好 —— 面板没有鉴权。
这里把三道门（未开启 / 盯盘进程内 / 路径合法）都钉住。
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sigdesk.core.models import Timeframe
from sigdesk.feed.okx import normalize_candles
from sigdesk.store.bar_store import BarStore
from sigdesk.store.parquet_io import write_bars
from sigdesk.store.runtime_store import RuntimeStore
from sigdesk.web.api import ServiceState, create_app

BTC = "CRYPTO.OKX.BTCUSDT.PERP"

SOURCE = f"""
id: api-demo
description: 试算用
universe: [{BTC}]
conditions:
  - on: 1m
    mode: event
    when: cross_up(close, ema(close, 10))
emit:
  direction: long
  dedup_key: "{{symbol}}:{{rule}}:{{bar_close_ts}}"
"""


def _state(tmp_path: pathlib.Path, **kw: Any) -> ServiceState:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir(exist_ok=True)
    return ServiceState(
        runtime=RuntimeStore(tmp_path / "runtime.sqlite3"),
        data_root=tmp_path / "data",
        rules_dir=rules_dir,
        **kw,
    )


@pytest.fixture
def editable(tmp_path: pathlib.Path, btc_swap_okx: dict[str, Any]) -> TestClient:
    bars = normalize_candles(btc_swap_okx["1m"], symbol=BTC, timeframe=Timeframe.M1)
    store = BarStore(timeframes=[Timeframe.M5])
    persisted = [d for b in bars for d in store.push(b)]
    write_bars(tmp_path / "data", persisted)
    return TestClient(create_app(_state(tmp_path, live=False, edit_enabled=True)))


# ---- 三道门 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("post", "/api/rules"), ("put", "/api/rules/x"), ("delete", "/api/rules/x"),
     ("post", "/api/rules/trial")],
)
def test_writes_are_refused_when_editing_is_off(
    tmp_path: pathlib.Path, method: str, path: str
) -> None:
    """默认关闭。面板没有鉴权，部署到公网 VPS 时不该顺手带上写能力。"""
    client = TestClient(create_app(_state(tmp_path, live=False)))
    # 用 request() 而不是 client.delete(json=...) —— httpx 的 delete 不收 json
    resp = client.request(method.upper(), path, json={"source": SOURCE})
    assert resp.status_code == 403
    assert "allow-edit" in resp.json()["detail"]


def test_writes_are_refused_inside_the_watch_process(tmp_path: pathlib.Path) -> None:
    """热替换会静默丢掉已布防的链路 —— 宁可拒绝，也不要让人以为改生效了。"""
    client = TestClient(create_app(_state(tmp_path, live=True, edit_enabled=True)))
    resp = client.post("/api/rules", json={"source": SOURCE})
    assert resp.status_code == 409
    assert "状态机" in resp.json()["detail"]


def test_trial_is_gated_too(tmp_path: pathlib.Path) -> None:
    """试算不改任何东西，但会按请求读任意标的的全量历史并同步跑引擎 ——
    既是 CPU 放大器，也会跟盯盘抢资源。跟写端点同一道门。"""
    off = TestClient(create_app(_state(tmp_path, live=False)))
    assert off.post("/api/rules/trial", json={"source": SOURCE}).status_code == 403
    live = TestClient(create_app(_state(tmp_path, live=True, edit_enabled=True)))
    assert live.post("/api/rules/trial", json={"source": SOURCE}).status_code == 409


def test_reading_rules_stays_open(tmp_path: pathlib.Path) -> None:
    """读是公开的：面板本来就展示规则。只有写需要 --allow-edit。"""
    client = TestClient(create_app(_state(tmp_path, live=False)))
    body = client.get("/api/rules").json()
    assert body["editable"] is False and body["rules"] == []


def test_editable_flag_reflects_both_gates(tmp_path: pathlib.Path) -> None:
    live = TestClient(create_app(_state(tmp_path, live=True, edit_enabled=True)))
    assert live.get("/api/rules").json()["editable"] is False


# ---- 校验 -----------------------------------------------------------------


def test_validate_accepts_a_good_rule(editable: TestClient) -> None:
    body = editable.post("/api/rules/validate", json={"source": SOURCE}).json()
    assert body["ok"] is True
    assert body["rule"]["id"] == "api-demo" and body["rule"]["timeframe"] == "1m"


def test_validate_reports_the_error_without_500(editable: TestClient) -> None:
    """语法错是用户输入，不是服务器故障 —— 要 200 + ok:false，不是 500。"""
    resp = editable.post("/api/rules/validate", json={"source": "id: x\nconditions: []\n"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False and "没有条件" in body["error"]


def test_validate_rejects_sandbox_escape(editable: TestClient) -> None:
    bad = SOURCE.replace("cross_up(close, ema(close, 10))", "close.__class__")
    assert editable.post("/api/rules/validate", json={"source": bad}).json()["ok"] is False


def test_validate_does_not_write_anything(editable: TestClient, tmp_path: pathlib.Path) -> None:
    editable.post("/api/rules/validate", json={"source": SOURCE})
    assert list((tmp_path / "rules").glob("*.yaml")) == []


# ---- CRUD -----------------------------------------------------------------


def test_create_read_update_delete(editable: TestClient) -> None:
    assert editable.post("/api/rules", json={"source": SOURCE}).status_code == 201
    assert [r["id"] for r in editable.get("/api/rules").json()["rules"]] == ["api-demo"]

    src = editable.get("/api/rules/api-demo/source").json()["source"]
    assert "cross_up" in src

    updated = src.replace("description: 试算用", "description: 改过了")
    assert editable.put("/api/rules/api-demo", json={"source": updated}).status_code == 200
    assert editable.get("/api/rules").json()["rules"][0]["description"] == "改过了"

    body = editable.delete("/api/rules/api-demo").json()
    # 正斜杠是**接口约定**，不是平台细节：Windows 上曾返回 `rules\\_trash\\...`，
    # 这条断言当场就炸了。API 的路径表示不该跟着操作系统变。
    assert body["archived_to"].startswith("rules/_trash/")
    assert "\\" not in body["archived_to"]
    assert editable.get("/api/rules").json()["rules"] == []


def test_create_twice_is_a_400_not_a_silent_overwrite(editable: TestClient) -> None:
    editable.post("/api/rules", json={"source": SOURCE})
    resp = editable.post("/api/rules", json={"source": SOURCE})
    assert resp.status_code == 400 and "已存在" in resp.json()["detail"]


def test_saving_an_invalid_rule_is_a_400(editable: TestClient) -> None:
    resp = editable.post("/api/rules", json={"source": "id: x\n"})
    assert resp.status_code == 400


def test_delete_missing_is_404(editable: TestClient) -> None:
    assert editable.delete("/api/rules/nope").status_code == 404


def test_source_of_missing_rule_is_404(editable: TestClient) -> None:
    assert editable.get("/api/rules/nope/source").status_code == 404


def test_path_traversal_in_id_is_refused(editable: TestClient) -> None:
    assert editable.get("/api/rules/..%2F..%2Fetc%2Fpasswd/source").status_code in (400, 404)


# ---- 历史试算 -------------------------------------------------------------


def test_trial_runs_on_stored_bars(editable: TestClient) -> None:
    body = editable.post("/api/rules/trial", json={"source": SOURCE}).json()
    assert body["bars_scanned"] > 0
    assert body["symbols_scanned"] == [BTC]
    assert body["report"]["params"]["horizon_bars"] == 20
    assert len(body["outcomes"]) == len(body["signals"])
    for s in body["signals"]:
        assert s["rule_id"] == "api-demo" and s["symbol"] == BTC


def test_trial_is_reproducible(editable: TestClient) -> None:
    """同一批输入必然得到相同结果 —— 与 /api/stats 同一条验收。"""
    a = editable.post("/api/rules/trial", json={"source": SOURCE}).json()
    b = editable.post("/api/rules/trial", json={"source": SOURCE}).json()
    assert a["signals"] == b["signals"] and a["report"] == b["report"]


def test_trial_does_not_persist_anything(editable: TestClient, tmp_path: pathlib.Path) -> None:
    """试算不落盘、不推送、不下单，也不写运行态。"""
    editable.post("/api/rules/trial", json={"source": SOURCE})
    assert list((tmp_path / "rules").glob("*.yaml")) == []
    assert RuntimeStore(tmp_path / "runtime.sqlite3").count_signals() == 0


def test_trial_params_change_the_verdict_and_are_echoed(editable: TestClient) -> None:
    """口径由参数决定并原样带回 —— 一份不写明口径的胜率没有意义（ADR-0008）。"""
    tight = editable.post(
        "/api/rules/trial", json={"source": SOURCE, "stop_pct": 0.0005, "horizon_bars": 5}
    ).json()
    assert tight["report"]["params"]["stop_pct"] == 0.0005
    loose = editable.post(
        "/api/rules/trial", json={"source": SOURCE, "stop_pct": 0.05, "horizon_bars": 100}
    ).json()
    assert tight["report"]["overall"] != loose["report"]["overall"]


def test_trial_on_a_symbol_without_data_says_so(editable: TestClient) -> None:
    """"0 条信号"到底是规则太严还是数据没回补，是两个完全不同的结论。"""
    resp = editable.post(
        "/api/rules/trial", json={"source": SOURCE.replace(BTC, "CN.SHFE.nope")}
    )
    assert resp.status_code == 404 and "回补" in resp.json()["detail"]


def test_trial_rejects_an_invalid_rule(editable: TestClient) -> None:
    assert editable.post("/api/rules/trial", json={"source": "id: x\n"}).status_code == 400


def test_trial_matches_the_live_engine_signal_for_signal(
    editable: TestClient, tmp_path: pathlib.Path, btc_swap_okx: dict[str, Any]
) -> None:
    """**试算与实盘必须逐条一致** —— 它们本就该是同一个引擎（ADR-0001）。
    分家了就会出现"试算说会触发、实盘不触发"这种最难查的问题。"""
    from sigdesk.rules.engine import RuleEngine
    from sigdesk.rules.store import parse_source

    rule, _ = parse_source(SOURCE)
    bars = normalize_candles(btc_swap_okx["1m"], symbol=BTC, timeframe=Timeframe.M1)
    store = BarStore(timeframes=[])
    engine = RuleEngine([rule], store)
    expected = [s for b in bars for s in engine.on_bars(store.push(b))]

    got = editable.post("/api/rules/trial", json={"source": SOURCE}).json()["signals"]
    assert [s.as_dict() for s in expected] == got
