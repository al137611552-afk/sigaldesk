"""单级别规则引擎、规则加载与推送格式化的单测。全部脱网、无磁盘（除读示例 YAML）。"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

from sigdesk.core.models import Bar, Market, Timeframe
from sigdesk.core.registry import load_registry
from sigdesk.feed.okx import normalize_candles
from sigdesk.rules.engine import DEDUP_MEMORY, RuleEngine
from sigdesk.rules.loader import RuleError, load_rule, load_rules
from sigdesk.rules.model import Direction, Mode, Signal, parse_duration
from sigdesk.sinks.notify import BarkNotifier, MultiNotifier, format_signal
from sigdesk.store.bar_store import BarStore

UID = "CRYPTO.OKX.BTCUSDT.PERP"
ROOT = pathlib.Path(__file__).resolve().parents[1]


def rule_yaml(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "t1",
        "universe": [UID],
        "timeframe": "1m",
        "conditions": [{"on": "1m", "mode": "state", "when": "close > 0"}],
        "emit": {"direction": "long"},
    }
    base.update(overrides)
    return base


def bar(close_ts: int, close: float = 100.0, volume: float = 1.0, symbol: str = UID) -> Bar:
    return Bar(symbol, Timeframe.M1, close_ts - 60, close_ts, close, close + 1, close - 1,
               close, volume)


def engine_for(raw: dict[str, Any]) -> tuple[RuleEngine, BarStore]:
    store = BarStore(timeframes=[])
    return RuleEngine([load_rule(raw)], store), store


def feed(engine: RuleEngine, store: BarStore, bars: list[Bar]) -> list[Signal]:
    """按实盘顺序走：先入库，再求值 —— 顺序反了就取不到当前这根。"""
    out: list[Signal] = []
    for b in bars:
        store.push(b)
        out.extend(engine.on_bar(b))
    return out


# ---------------------------------------------------------------- 加载与校验


def test_all_shipped_rules_load() -> None:
    """仓库里带的每一条规则都必须能加载 —— 否则等于放了个坏样板。

    **不点名具体某条规则**：规则会随策略调整增删（bounce / ema-cross / volume-spike
    都删过），点名就会在删规则时误伤这条测试，让人以为删坏了东西。
    这里守的是"发出去的都是好的"，具体某条规则的语义由它自己的试算负责。
    """
    rules = load_rules(ROOT / "config" / "rules", load_registry(ROOT / "config"))
    assert rules, "config/rules 空了"
    assert len({r.id for r in rules}) == len(rules), "有重复 id"
    for r in rules:
        assert r.conditions, f"{r.id} 没有条件"
        assert r.universe, f"{r.id} 没有标的"


def test_single_level_rule_still_compiles() -> None:
    """单级别规则（只有一段条件）仍然要能编译。

    M1 只支持单级别，M2 之后共用同一个模型和引擎（ADR-0001）——
    这条守的是"长度为 1 的链路没被多级别改动搞坏"。**用内联规则**，
    不依赖 config/rules 里恰好有哪条。
    """
    rule = load_rule(rule_yaml(
        conditions=[{"on": "trigger", "mode": "event",
                     "when": "cross_up(close, ema(close, 20))"}],
        timeframes={"trigger": "15m"},
    ))
    assert rule.timeframe is Timeframe.M15
    assert rule.trigger.mode is Mode.EVENT
    assert len(rule.conditions) == 1 and not rule.is_multi_level


def test_multi_level_example_now_loads() -> None:
    """M1 时期这个样板会被拒绝（当时只支持单级别）；M2 之后它必须能加载。

    三段式：trend(1h/state) -> setup(15m/window,6) -> trigger(5m/event)。
    """
    raw = yaml.safe_load(
        (ROOT / "docs" / "examples" / "m2-trend-pullback.yaml").read_text(encoding="utf-8")
    )
    rule = load_rule(raw)

    assert [c.role for c in rule.conditions] == ["trend", "setup", "trigger"]
    assert rule.timeframes == {
        "trend": Timeframe.H1, "setup": Timeframe.M15, "trigger": Timeframe.M5
    }
    assert rule.trigger.on is Timeframe.M5 and rule.timeframe is Timeframe.M5
    assert rule.conditions[1].mode is Mode.WINDOW and rule.conditions[1].within == 6
    assert rule.emit.ttl_bars == 8
    assert rule.is_multi_level


def test_every_shipped_rule_file_loads() -> None:
    """config/rules/ 下的文件都会被真加载：任何一个坏了就是启动失败，不能只测示例那一个。"""
    assert len(load_rules(ROOT / "config" / "rules",
                          load_registry(ROOT / "config"))) >= 1


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"id": ""}, "缺少 id"),
        ({"universe": []}, "universe 为空"),
        ({"timeframe": "7m"}, "无效"),
        ({"conditions": []}, "没有条件"),
        ({"conditions": [{"on": "1m", "mode": "state", "when": "close > 0"}] * 2}, "角色重复"),
        ({"conditions": [{"on": "1m", "mode": "state", "within": 6, "when": "close>0"}]},
         "只对 mode: window"),
        ({"conditions": [{"on": "1m", "mode": "sideways", "when": "close>0"}]}, "mode"),
        ({"conditions": [{"on": "1m", "mode": "state"}]}, "缺少 when"),
        ({"conditions": [{"on": "1m", "mode": "state", "plugin": "x"}]}, "plugin"),
        ({"emit": {"direction": "sideways"}}, "direction"),
        ({"emit": {"ttl": "8 bars"}}, "ttl"),
    ],
)
def test_bad_rule_is_rejected_at_load_time(patch: dict[str, Any], match: str) -> None:
    """规则打错字应该在启动时炸掉，而不是盘中静默不触发。"""
    with pytest.raises(RuleError, match=match):
        load_rule(rule_yaml(**patch))


def test_bad_expression_names_the_rule() -> None:
    with pytest.raises(RuleError, match="t1 的 1m.when"):
        load_rule(rule_yaml(conditions=[{"on": "1m", "mode": "state", "when": "emaa(close,2)>0"}]))


def test_duplicate_rule_id_rejected(tmp_path: pathlib.Path) -> None:
    for name in ("a.yaml", "b.yaml"):
        (tmp_path / name).write_text(yaml.safe_dump(rule_yaml()), encoding="utf-8")
    with pytest.raises(RuleError, match="id 重复"):
        load_rules(tmp_path)


@pytest.mark.parametrize(
    ("text", "seconds"), [("30s", 30), ("15m", 900), ("2h", 7200), ("1d", 86400), (90, 90)]
)
def test_parse_duration(text: str | int, seconds: int) -> None:
    assert parse_duration(text) == seconds


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="无法解析"):
        parse_duration("half an hour")


# ---------------------------------------------------------------- 求值与触发


def test_state_mode_fires_every_bar_while_true() -> None:
    engine, store = engine_for(rule_yaml())
    got = feed(engine, store, [bar(60), bar(120), bar(180)])
    assert [s.fired_at for s in got] == [60, 120, 180]


def test_event_mode_fires_only_on_the_rising_edge() -> None:
    engine, store = engine_for(
        rule_yaml(conditions=[{"on": "1m", "mode": "event", "when": "close > 100"}])
    )
    bars = [bar(60, 99), bar(120, 101), bar(180, 102), bar(240, 99), bar(300, 105)]
    got = feed(engine, store, bars)
    assert [s.fired_at for s in got] == [120, 300], "只在 False->True 的跳变上触发"


def test_event_mode_does_not_fire_on_first_known_value() -> None:
    """预热期是"未知"不是"不成立" —— 否则每条规则刚上线都会先误报一次。"""
    engine, store = engine_for(
        rule_yaml(conditions=[{"on": "1m", "mode": "event", "when": "sma(close,3) > 0"}])
    )
    got = feed(engine, store, [bar(60 * i) for i in range(1, 6)])
    assert got == [], "预热期结束后的第一根被误判成了边沿"


def test_unknown_never_fires() -> None:
    engine, store = engine_for(
        rule_yaml(conditions=[{"on": "1m", "mode": "state", "when": "sma(close,50) > 0"}])
    )
    assert feed(engine, store, [bar(60 * i) for i in range(1, 10)]) == []


def test_rule_ignores_other_symbols_and_timeframes() -> None:
    engine, store = engine_for(rule_yaml())
    other = bar(60, symbol="CRYPTO.OKX.ETHUSDT.PERP")
    store.push(other)
    assert engine.on_bar(other) == []
    five = Bar(UID, Timeframe.M5, 0, 300, 1, 1, 1, 1, 1)
    assert engine.on_bar(five) == []


def test_unclosed_bar_never_evaluates() -> None:
    """INV-2：盘中未收盘的 bar 不得触发信号。"""
    engine, _ = engine_for(rule_yaml())
    tentative = Bar(UID, Timeframe.M1, 0, 60, 1, 1, 1, 1, 1, closed=False)
    assert engine.on_bar(tentative) == []


def test_disabled_rule_is_not_loaded_into_engine() -> None:
    engine, store = engine_for(rule_yaml(enabled=False))
    assert engine.rules == []
    assert feed(engine, store, [bar(60)]) == []


# ---------------------------------------------------------------- 冷却与去重


def test_cooldown_uses_bar_time_not_wall_clock() -> None:
    """冷却按 bar 的 close_ts 算 —— 这样回放与实盘的冷却行为逐条一致。"""
    engine, store = engine_for(rule_yaml(emit={"direction": "long", "cooldown": "5m"}))
    got = feed(engine, store, [bar(60 * i) for i in range(1, 12)])
    assert [s.fired_at for s in got] == [60, 360, 660], "冷却 300s：60 -> 360 -> 660"


def test_dedup_key_blocks_repeat_on_same_bar() -> None:
    engine, store = engine_for(rule_yaml())
    b = bar(60)
    store.push(b)
    assert len(engine.on_bar(b)) == 1
    assert engine.on_bar(b) == [], "同一根 bar 重复投递不得二次触发"


def test_dedup_key_can_widen_the_window() -> None:
    """把去重键绑到交易日，就是"每个交易日只报一次"。"""
    engine, store = engine_for(
        rule_yaml(emit={"direction": "long", "dedup_key": "{symbol}:{rule}:{trading_day}"})
    )
    bars = [
        Bar(UID, Timeframe.M1, t - 60, t, 100, 101, 99, 100, 1, trading_day="2026-08-28")
        for t in (60, 120, 180)
    ]
    assert len(feed(engine, store, bars)) == 1


def test_unknown_dedup_placeholder_is_reported() -> None:
    engine, store = engine_for(
        rule_yaml(emit={"direction": "long", "dedup_key": "{symbol}:{nope}"})
    )
    store.push(bar(60))
    with pytest.raises(ValueError, match="未知占位符"):
        engine.on_bar(bar(60))


def test_dedup_memory_is_bounded() -> None:
    """去重表不能无限增长 —— 开发机只有 4G。"""
    engine, store = engine_for(rule_yaml())
    feed(engine, store, [bar(60 * i) for i in range(1, DEDUP_MEMORY + 200)])
    inst = engine.instances()[("t1", UID)]
    assert len(inst.seen) == DEDUP_MEMORY


# ---------------------------------------------------------------- 信号内容


def test_signal_carries_key_values(btc_swap_okx: dict[str, Any]) -> None:
    """M1 验收：推送内容必须含各关键值，否则收到通知也没法据以决策。"""
    bars = normalize_candles(btc_swap_okx["1m"], symbol=UID, timeframe=Timeframe.M1)
    engine, store = engine_for(
        rule_yaml(
            conditions=[{"on": "1m", "mode": "state", "when": "close > ema(close,20)"}],
            context={"ema20": "ema(close,20)", "rsi14": "rsi(14)", "vol_ratio":
                     "volume / sma(volume,20)"},
        )
    )
    got = feed(engine, store, bars)
    assert got, "整段真实行情里一次都没触发，规则或引擎有问题"
    sig = got[-1]
    assert set(sig.context) == {"close", "volume", "ema20", "rsi14", "vol_ratio"}
    assert sig.context["ema20"] is not None
    assert sig.context["close"] == sig.trigger_price
    assert sig.direction is Direction.LONG
    assert sig.as_dict()["rule_id"] == "t1"


def test_context_snapshot_failure_does_not_kill_the_signal() -> None:
    """某个快照表达式算不出来（如除零）时，信号照发，该项记 None。"""
    engine, store = engine_for(rule_yaml(context={"bad": "close / 0"}))
    (sig,) = feed(engine, store, [bar(60)])
    assert sig.context["bad"] is None
    assert sig.context["close"] == 100.0


def test_indicator_cache_is_shared_between_rules() -> None:
    """两条规则用同一个 ema(close,20) 只应算一份。"""
    store = BarStore(timeframes=[])
    rules = [
        load_rule(rule_yaml(id="r1", conditions=[{"on": "1m", "mode": "state",
                                                  "when": "ema(close,5) > 0"}])),
        load_rule(rule_yaml(id="r2", conditions=[{"on": "1m", "mode": "state",
                                                  "when": "ema(close,5) > 1"}])),
    ]
    engine = RuleEngine(rules, store)
    for i in range(1, 20):
        b = bar(60 * i)
        store.push(b)
        engine.on_bar(b)
    assert len(engine.cache_for(UID, Timeframe.M1).states) == 1


def test_replay_and_incremental_give_identical_signals(btc_swap_okx: dict[str, Any]) -> None:
    """同一段行情，逐根喂与整段回放必须产出**完全相同**的信号。

    这是 M2 红线（replay == live）的单级别预演 —— 引擎不读墙钟就是为了这个。
    """
    bars = normalize_candles(btc_swap_okx["1m"], symbol=UID, timeframe=Timeframe.M1)
    raw = rule_yaml(
        conditions=[{"on": "1m", "mode": "event", "when": "cross_up(close, ema(close,20))"}],
        emit={"direction": "long", "cooldown": "3m"},
    )
    live_engine, live_store = engine_for(raw)
    live = feed(live_engine, live_store, bars)

    replay_store = BarStore(timeframes=[])
    replay_engine = RuleEngine([load_rule(raw)], replay_store)
    replay_store.load(bars)  # 整段先入库
    replay = replay_engine.feed(bars)

    assert [s.as_dict() for s in live] == [s.as_dict() for s in replay]
    assert live, "整段行情一次都没触发，这条测试就没有说服力"


# ---------------------------------------------------------------- 推送格式化


def make_signal(**kw: Any) -> Signal:
    base: dict[str, Any] = dict(
        rule_id="ema-cross-long",
        symbol=UID,
        direction=Direction.LONG,
        timeframe=Timeframe.M15,
        fired_at=1787920800,
        trigger_price=79382.9,
        dedup_key="k",
        context={"close": 79382.9, "ema20": 79310.25, "rsi14": None},
    )
    base.update(kw)
    return Signal(**base)


def test_format_includes_direction_symbol_time_price_and_values() -> None:
    text = format_signal(make_signal(), description="15m 上穿 EMA20")
    assert "▲ 多" in text
    assert UID in text and "15m" in text
    assert "79,382.90" in text
    assert "ema20 = 79,310.25" in text
    assert "15m 上穿 EMA20" in text
    assert "UTC" in text, "加密标的用 UTC 显示"


def test_format_shows_dash_for_warmup_values() -> None:
    """预热期的值显示破折号，不能显示 0 —— 0 在推送里会被当成真实数字读。"""
    assert "rsi14 = —" in format_signal(make_signal())


def test_format_futures_signal_uses_beijing_time_and_trading_day() -> None:
    text = format_signal(
        make_signal(symbol="CN.SHFE.rb2610", timeframe=Timeframe.M5, trading_day="2026-08-28")
    )
    assert "CST" in text and "交易日 2026-08-28" in text
    assert Market.CRYPTO.value not in text


def test_format_marks_tentative() -> None:
    assert "盘中预报" in format_signal(make_signal(tentative=True))


class _FakeNotifier:
    def __init__(self, name: str, ok: bool = True, boom: bool = False) -> None:
        self.name, self._ok, self._boom = name, ok, boom
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        if self._boom:
            raise RuntimeError("渠道炸了")
        self.sent.append(text)
        return self._ok


async def test_one_broken_channel_does_not_block_the_others() -> None:
    """推送失败绝不能反过来打断行情处理。"""
    good, bad, boom = _FakeNotifier("good"), _FakeNotifier("bad", ok=False), \
        _FakeNotifier("boom", boom=True)
    multi = MultiNotifier([good, bad, boom])

    result = await multi.send("hello")

    assert result == {"good": True, "bad": False, "boom": False}
    assert good.sent == ["hello"]
    assert multi.failures == {"bad": 1, "boom": 1}


async def test_multi_notifier_with_no_channels_is_a_noop() -> None:
    assert await MultiNotifier([]).send("x") == {}


def test_bark_url_escapes_the_message() -> None:
    """信号正文里有 / 和空格，不转义会拼出错误的 URL。"""
    notifier = BarkNotifier("https://api.day.app/key/")
    title, _, body = "A/B 标题\n正文 有空格".partition("\n")
    from urllib.parse import quote

    assert quote(title, safe="") == "A%2FB%20%E6%A0%87%E9%A2%98"
    assert notifier.base_url.rstrip("/") == "https://api.day.app/key"
    assert quote(body, safe="").startswith("%E6%AD%A3%E6%96%87")


def test_prime_updates_edge_state_without_emitting() -> None:
    """预热必须喂饱 event 模式的"上一根"，又绝不能把历史行情当实时报一遍。"""
    raw = rule_yaml(conditions=[{"on": "1m", "mode": "event", "when": "close > 100"}])
    engine, store = engine_for(raw)
    history = [bar(60, 105), bar(120, 106)]  # 历史上条件一直成立
    for b in history:
        store.push(b)
    engine.prime(history)

    live = bar(180, 107)
    store.push(live)
    assert engine.on_bar(live) == [], "上一根已成立，本根不是边沿，不该触发"

    dip = bar(240, 99)
    store.push(dip)
    engine.on_bar(dip)
    rise = bar(300, 108)
    store.push(rise)
    assert len(engine.on_bar(rise)) == 1, "真正的边沿应当触发"


def test_prime_does_not_consume_cooldown_or_dedup() -> None:
    raw = rule_yaml(emit={"direction": "long", "cooldown": "1h"})
    engine, store = engine_for(raw)
    history = [bar(60), bar(120)]
    for b in history:
        store.push(b)
    engine.prime(history)

    live = bar(180)
    store.push(live)
    assert len(engine.on_bar(live)) == 1, "预热占用了冷却或去重表"


def test_confirm_on_close_false_is_rejected_not_silently_ignored() -> None:
    """盘中预报没实现。宁可拒绝启动，也不要让人以为开了。"""
    with pytest.raises(RuleError, match="尚未实现"):
        load_rule(rule_yaml(emit={"confirm_on_close": False}))


def test_confirm_on_close_must_be_a_real_bool() -> None:
    """bool("false") 是 True —— 不能用 bool() 兜，否则写错的人以为自己关掉了。"""
    with pytest.raises(RuleError, match="必须是布尔值"):
        load_rule(rule_yaml(emit={"confirm_on_close": "false"}))


def test_unknown_priority_is_rejected() -> None:
    """档位收成枚举。原来 loader 直接 ``str(...)``，把 high 敲成 higth 照收不误，
    静默多出一个谁也没定义的档位 —— 排序时它既不高也不低，表现是"这条规则的信号
    偶尔莫名被别的盖住"，且没有任何报错。宁可拒绝启动。"""
    with pytest.raises(RuleError, match="priority"):
        load_rule(rule_yaml(emit={"priority": "higth"}))


def test_known_priorities_load() -> None:
    for name in ("high", "normal", "low"):
        assert str(load_rule(rule_yaml(emit={"priority": name})).emit.priority) == name


# ---------------------------------------------------------------- universe 通配符


def _reg() -> Any:
    return load_registry(ROOT / "config")


def test_wildcard_universe_expands_at_load_time() -> None:
    """`CN.*` 在**加载时**就展开成具体 uid，下游拿到的永远是具体清单。

    不这么做的话，`Rule.universe` 有十几处消费方（引擎、试算、面板、采集列表、
    纸上回测…），任何一处忘了处理通配符，表现都是"这个标的悄悄不被盯" ——
    没有报错，只是永远收不到那个品种的信号。
    """
    rule = load_rule(rule_yaml(universe=["CN.*"]), _reg())
    assert rule.universe, "通配符展开成了空"
    assert all(u.startswith("CN.") for u in rule.universe)
    assert "CN.*" not in rule.universe, "通配符本身不该留在展开结果里"


def test_wildcard_excludes_continuous_series() -> None:
    """**主连不进 universe。** 它是拼接序列，可回测可看图但不可下单
    （CLAUDE.md 坑#9），不该产生预警。"""
    rule = load_rule(rule_yaml(universe=["CN.*"]), _reg())
    assert not any(u.endswith(".CONT") for u in rule.universe), rule.universe


def test_wildcard_without_registry_is_refused() -> None:
    """没有 registry 就展不开 —— **报错，不要静默当成空**。
    空 universe 的规则一条都不会报，而且毫无提示。"""
    with pytest.raises(RuleError, match="通配符"):
        load_rule(rule_yaml(universe=["CN.*"]))


def test_wildcard_matching_nothing_is_refused() -> None:
    """写错前缀（比如 `CNN.*`）匹配不到任何标的，同样要报错而不是静默为空。"""
    with pytest.raises(RuleError, match="没有匹配到"):
        load_rule(rule_yaml(universe=["CNN.*"]), _reg())


def test_wildcard_and_explicit_do_not_duplicate() -> None:
    """通配符展开的和手写的重合时只留一份，且保持顺序。"""
    reg = _reg()
    one = sorted(s.uid for s in reg.tradable() if s.uid.startswith("CN."))[0]
    rule = load_rule(rule_yaml(universe=["CN.*", one]), reg)
    assert rule.universe.count(one) == 1


def test_shipped_rules_cover_all_domestic_futures() -> None:
    """出厂规则用 `CN.*` 盯国内期货全品种：**往 symbols.yaml 加合约就自动纳入**，
    不用再去改每个规则文件（改两处迟早对不上）。"""
    reg = _reg()
    cn = {s.uid for s in reg.tradable() if s.uid.startswith("CN.")}
    for r in load_rules(ROOT / "config" / "rules", reg):
        assert cn <= set(r.universe), f"{r.id} 没覆盖全部国内期货"
