"""面板前端 JS 的桩化冒烟。node 不可用时跳过。

面板是无构建单页（ADR-0009），逻辑尽量放在了服务端，但仍有约 600 行 JS 无人看管：
渲染、时区换算、多周期联动都在那里。这条测试用桩替掉 DOM / fetch / 图表库，
把 `boot()` 完整跑一遍，再直接调用另两个视图的渲染函数 ——
抓的是语法错、接口名写错、渲染函数抛异常这类会让面板整个白屏的问题。

**它抓不到视觉问题**（堆叠、截断、留白）。那些要靠 `scripts/shoot_panel.mjs` 截图看。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "panel_smoke.mjs"
APP = ROOT / "src" / "sigdesk" / "web" / "static" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node，跳过前端冒烟")


@pytest.fixture(scope="module")
def smoke() -> dict[str, object]:
    proc = subprocess.run(  # noqa: S603
        [shutil.which("node") or "node", str(HARNESS), str(APP)],
        capture_output=True, text=True, timeout=60, cwd=ROOT,
    )
    assert proc.returncode == 0, f"面板冒烟失败:\n{proc.stderr}"
    return dict(json.loads(proc.stdout.strip().splitlines()[-1]))


def test_app_js_parses() -> None:
    node = shutil.which("node") or "node"
    proc = subprocess.run(  # noqa: S603
        [node, "--check", str(APP)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr


def test_boot_does_not_blow_up(smoke: dict[str, object]) -> None:
    assert smoke["boot_failed"] is False, "boot() 抛异常，面板会整页显示启动失败"


def test_stats_and_ops_views_render(smoke: dict[str, object]) -> None:
    """这两个视图由标签点击触发，桩里点不了，所以直接调渲染函数 ——
    不这么做的话它们就完全没有覆盖。"""
    assert smoke["stats_error"] == "", smoke["stats_error"]
    assert smoke["ops_error"] == "", smoke["ops_error"]


def test_trade_view_renders_account_positions_fills_and_rejects(
    smoke: dict[str, object],
) -> None:
    """纸上账户页：账户、持仓、成交流水、被风控拒掉的单，四块都要能渲染。"""
    assert smoke["trade_error"] == "", smoke["trade_error"]
    banner = str(smoke["trade_banner_html"])
    assert "权益" in banner and "收益率" in banner and "手续费" in banner
    pos = str(smoke["trade_positions_html"])
    assert "SHFE.rb2610" in pos and "空" in pos, "持仓方向要能看出多空"
    fills = str(smoke["trade_fills_html"])
    assert "开仓" in fills and "止盈" in fills, "成交性质要翻成人话"
    assert "k1" in fills, "每笔成交要标明来源信号（成交与信号一一对应）"
    rejects = str(smoke["trade_rejects_html"])
    assert "单笔风险超限" in rejects, "拒单原因要翻成人话，不能只显示 per_trade_risk"


def test_panel_calls_every_endpoint_it_needs(smoke: dict[str, object]) -> None:
    """接口名写错在浏览器里只会安静地少一块内容，这里要当场发现。"""
    assert set(smoke["endpoints"]) >= {  # type: ignore[arg-type]
        "/api/meta", "/api/signals", "/api/bars", "/api/markers",
        "/api/health", "/api/events", "/api/chains", "/api/trade",
    }


def test_direction_is_drawn_as_svg_not_a_dingbat(smoke: dict[str, object]) -> None:
    """方向用 SVG 画，不用 ▲▼ 字符：字符在不同字体下大小与基线会飘，也不好上色。"""
    html = str(smoke["feed_html"]) + str(smoke["detail_html"])
    assert "<svg" in html and "polygon" in html
    assert "▲" not in html and "▼" not in html


def test_feed_rows_carry_symbol_price_rule_and_time(smoke: dict[str, object]) -> None:
    html = str(smoke["feed_html"])
    assert "SHFE.rb2610" in html
    assert "3,120" in html
    assert "r1" in html


def test_selected_signal_shows_per_level_evidence(smoke: dict[str, object]) -> None:
    """信号详情展开成「为什么触发」：每一级别一行证据，而不是一行截断的 k=v。"""
    html = str(smoke["detail_html"])
    assert html.count('class="ev"') == 2, "两级规则应当给出两行证据"
    assert "close &gt; ema(close,5)" in html or "close > ema(close,5)" in html
    assert "trend 15m" in html and "trigger 1m" in html


def test_warmup_values_render_as_dash_not_zero(smoke: dict[str, object]) -> None:
    """预热期的 None 必须显示破折号 —— 显示 0 会被当成真实数字读（ADR-0006）。"""
    html = str(smoke["detail_html"])
    assert "rsi14" in html and "—" in html
    assert "rsi14 </span>0" not in html


def test_times_render_in_market_local_timezone(smoke: dict[str, object]) -> None:
    """NFR-5：展示层按市场本地时区渲染。期货标 CST、加密标 UTC。"""
    html = str(smoke["feed_html"]) + str(smoke["detail_html"])
    assert "CST" in html, "期货信号没有按北京时间显示"
    assert "UTC" in html, "加密信号没有按 UTC 显示"


def test_chain_strip_shows_phase_and_ttl(smoke: dict[str, object]) -> None:
    """链路状态条是这一版的核心新增：引擎知道"正在酝酿什么"，要显示出来。"""
    html = str(smoke["chains_html"])
    assert smoke["chains_hidden"] is False
    assert "已布防" in html and "冷却中" in html
    assert "TTL 4/6" in html, "已布防的卡片要显示 TTL 剩余"
    assert "trend 15m" in html and "trigger 1m" in html


def test_markers_come_from_the_server(smoke: dict[str, object]) -> None:
    """分桶与筛选都在服务端做，前端只画。夹具给 4 条信号（1 条落在本周期外）+ 2 笔成交，
    所以图上应有 3 个信号标注 + 2 个成交标注 = 5 个。"""
    assert smoke["markers"] == 5
    assert "标注 3/4" in str(smoke["chart_note"])
    assert "不在本周期序列内" in str(smoke["chart_note"]), "被丢弃的信号没有如实提示"


def test_stats_lead_with_the_verdict(smoke: dict[str, object]) -> None:
    """期望收益是结论，要排在第一张卡 —— 旧版把它埋在 10 张一样的卡片里。"""
    html = str(smoke["heroes_html"])
    assert html.index("期望收益") < html.index("胜率"), "结论没有排在最前"
    assert "+0.100%" in html or "0.100%" in html


def test_exit_reasons_are_directly_labelled(smoke: dict[str, object]) -> None:
    """三段状态色必须全部直接标注 —— 状态色不能单独表意（dataviz 规范）。"""
    html = str(smoke["exit_legend_html"])
    for label in ("持有到期", "触及止损", "触及止盈"):
        assert label in html


def test_excursion_note_explains_the_horizon_rate(smoke: dict[str, object]) -> None:
    assert "持有到期" in str(smoke["exc_note_html"])


def test_hour_chart_warns_when_the_sample_is_too_small(smoke: dict[str, object]) -> None:
    """n=2 的时段显示"胜率 100%"会误导人 —— 样本不足时必须明说。"""
    assert "样本量不足" in str(smoke["hour_warn"])


def test_ops_shows_banner_timeline_and_rules(smoke: dict[str, object]) -> None:
    assert "需要关注" in str(smoke["ops_banner_html"]) or "正常" in str(smoke["ops_banner_html"])
    assert "tl-row" in str(smoke["ops_timeline_html"]), "标的连续性时间线没渲染出来"
    assert "r1" in str(smoke["ops_rules_html"])


# ---- 规则页（FR-5.3）------------------------------------------------------


def test_rules_tab_renders_without_error(smoke: dict[str, object]) -> None:
    """桩化冒烟保证不白屏：少一个桩方法整页就废。"""
    assert smoke["rules_error"] == "" and smoke["trial_error"] == ""
    assert "r1" in str(smoke["rule_items_html"])


def test_validate_result_says_what_passed(smoke: dict[str, object]) -> None:
    """校验通过要说清楚"通过了什么"，光一个绿勾等于没说。"""
    msg = str(smoke["rule_msg_text"])
    assert "语法通过" in msg and "扳机 1m" in msg and "2 级" in msg


def test_trial_reports_scale_and_hit_count(smoke: dict[str, object]) -> None:
    assert "1200 根 1m" in str(smoke["trial_sub_text"])
    assert "1 条信号" in str(smoke["trial_sub_text"])


def test_trial_names_symbols_without_data(smoke: dict[str, object]) -> None:
    """"0 条信号"是规则太严还是数据没回补，是两个完全不同的结论 —— 必须点名。"""
    html = str(smoke["trial_body_html"])
    assert "CN.SHFE.nope" in html and "backfill.py" in html


def test_condition_bands_are_drawn_per_role(smoke: dict[str, object]) -> None:
    """各级别条件成立区间：每个角色一条轨道，成立/未知分色，信号**单独一条**轨道。

    信号原先画在每条轨道上，1m 那条 1000+ 根挤进几百像素时，
    绿段与蓝线糊成一片噪声 —— 重复画同一份信息只会互相遮蔽（截图才看得出来）。
    """
    html = str(smoke["trial_body_html"])
    assert html.count("band-track") == 3, "两个角色各一条 + 信号单独一条"
    assert html.count("band-track fires") == 1, "信号轨道只该有一条"
    assert 'class="true"' in html and 'class="unknown"' in html
    assert 'class="fire"' in html, "信号位置没有标出来"


def test_bands_legend_labels_unknown_as_warmup(smoke: dict[str, object]) -> None:
    """灰色是"预热期未知"不是"不成立" —— 混淆这两个会把人引向完全错误的排查方向。"""
    assert "预热期" in str(smoke["trial_body_html"])


def test_trial_states_its_params_and_limits(smoke: dict[str, object]) -> None:
    """一份不写明口径的胜率没有意义（ADR-0008）；试算的性质也要写明。"""
    html = str(smoke["trial_body_html"])
    assert "止损" in html and "止盈" in html
    assert "不落盘" in html and "同一个引擎" in html


def test_zero_signal_trial_shows_dashes_not_zeros() -> None:
    """0 条信号时"胜率 0.0%""期望收益 +0.000%"是误导 —— 那不是不赚钱，是算不出来。
    与 ADR-0006「预热期是 None 不是 0」同一条原则；M4 已经在收益率上栽过一次。

    顺带钉住空区间的表现：不撂一句死话，而是把逐级判定次数摊开 ——
    全 unknown（预热没跑完）和全 false（条件太严）的下一步完全不同。
    """
    import json
    import os
    import subprocess

    empty = {
        "/api/rules/trial": {
            "bars_scanned": 2594, "symbols_scanned": ["CRYPTO.OKX.BTCUSDT.PERP"],
            "symbols_without_data": [], "warmup_bars": 0,
            "signals": [], "outcomes": [],
            "report": {"overall": {
                "signals": 0, "evaluated": 0, "directional": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "avg_return": 0.0, "median_return": 0.0, "total_return": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0, "payoff": 0.0, "false_rate": 0.0,
                "target_rate": 0.0, "horizon_rate": 0.0, "avg_mfe": 0.0, "avg_mae": 0.0,
                "avg_bars_held": 0.0,
            }, "by_rule": {}, "by_symbol": {}, "by_hour": {}, "by_direction": {},
                "params": {"horizon_bars": 20}},
            "condition_bands": {"CRYPTO.OKX.BTCUSDT.PERP": []},
            "condition_counts": {"CRYPTO.OKX.BTCUSDT.PERP": {
                "15m": {"true": 0, "false": 87, "unknown": 0}}},
            "rule": {"id": "r1", "timeframe": "15m", "universe": [], "levels": []},
            "range": [0, 2147483648],
        }
    }
    out = subprocess.run(
        ["node", "tests/panel_smoke.mjs"],
        capture_output=True, text=True, timeout=120, check=True,
        env={**os.environ, "SMOKE_FIX": json.dumps(empty)},
    )
    body = str(json.loads(out.stdout.strip().splitlines()[-1])["trial_body_html"])

    assert "0.0%" not in body and "+0.000%" not in body, "算不出来的指标显示成了 0"
    assert body.count("—") >= 3, "胜率/期望收益/盈亏比都该是破折号"
    assert "判过" in body and "87" in body, "空区间要摊开逐级判定次数"
    assert "放宽最严的那一级" in body, "全 false 时要给出下一步，而不是笼统说没结果"


def test_continuous_symbols_are_labelled_in_the_picker(smoke: dict[str, object]) -> None:
    """主连可回测/可看图但**不可下单**，下拉框里必须标出来，别与可交易合约混淆。"""
    html = str(smoke["symbol_options_html"])
    assert "rb.CONT" in html and "主连" in html
    assert "BTCUSDT.PERP" in html and html.count("主连") == 1


def test_daily_timeframe_button_is_offered(smoke: dict[str, object]) -> None:
    assert 'data-tf="1d"' in str(smoke["tf_buttons_html"])


def test_chart_footer_states_how_old_the_last_bar_is(smoke: dict[str, object]) -> None:
    """"你正在看三天前的数据"必须一眼可见 —— 盯盘进程没在跑时，
    图看着完全正常、只是永远不动，会被当成「行情连不上」（真踩过）。"""
    note = str(smoke["chart_note"])
    assert "末根" in note
    assert any(w in note for w in ("刚刚", "分钟前", "小时前", "天前")), note


def test_textarea_is_covered_by_the_base_form_styling() -> None:
    """表单控件**不继承**页面的 color。`select,input,button` 里漏掉 textarea，
    结果就是浏览器默认的纯黑字配深色底（实测 1.17:1，基本看不见）。

    这条截图本该抓到却漏了 —— 因为当时只截到 `#rule-src:disabled` 那一瞬，
    而 :disabled 恰好是唯一显式设过颜色的状态。真正的护栏是
    `shoot_panel.mjs` 里的对比度检查；这里再钉一道静态的，跑测试就能发现。
    """
    css = pathlib.Path("src/sigdesk/web/static/styles.css").read_text(encoding="utf-8")
    base = next(ln for ln in css.splitlines() if ln.startswith("select,input,button"))
    assert "textarea" in base, f"基础表单样式漏了 textarea: {base}"
    assert "color:var(--fg)" in base
    assert "::placeholder" in css, "占位文字也要显式给色，否则各浏览器默认不一"


# ---- 图上标注：信号与成交要分得开 ----------------------------------------


def test_signal_directions_render_differently(smoke: dict[str, object]) -> None:
    """买卖点用**圆点 + B/S 字母**，不用箭头。

    箭头只有朝向之分：一屏全是多头信号时看着就是一模一样的一片（用户原话
    「现在 K 线图上全部是向上的箭头」）。字母是直接可读的，扫一眼就知道买还是卖。
    中性信号**不写字母** —— 它既不是买也不是卖，硬安一个字母是撒谎。
    """
    marks = [str(m) for m in smoke["marker_detail"]]  # type: ignore[union-attr]
    assert "circle|belowBar|#26a69a|B" in marks, f"多头：绿圆 + B，画在 bar 下方: {marks}"
    assert "circle|aboveBar|#ef5350|S" in marks, f"空头：红圆 + S，画在 bar 上方: {marks}"
    neutral = [m for m in marks if "#8b949e" in m]
    assert neutral and all(m.startswith("circle") and m.endswith("|") for m in neutral), (
        f"中性是无字母的灰圆: {neutral}")


def test_fills_are_drawn_and_priced_only_for_the_selected_signal(
    smoke: dict[str, object],
) -> None:
    """信号是"我认为该进场"，成交是"实际以什么价成交了"，两件事分开画。

    但**价格文字只给选中信号的那几笔**：几十笔成交挤在几百根 bar 里全写字会叠成一团，
    互相遮蔽（截图里当场看到 5 个标签压在一起）—— 信号标注当初不写文字就是这个原因。
    常态只画点表示"这里有成交"，点开某条信号后它那几笔才显示价格。
    """
    plain = [str(m) for m in smoke["marker_detail"]]  # type: ignore[union-attr]
    picked = [str(m) for m in smoke["marker_detail_selected"]]  # type: ignore[union-attr]

    # **三种形状各司其职**：圆点=信号、方块=开仓、箭头=离场。
    # 信号改成圆点之后，离场原本也是圆点、同色时完全分不开，所以换成箭头。
    assert any(m.startswith("square") for m in plain), "开仓用方块"
    assert any(m.startswith(("arrowUp", "arrowDown")) for m in plain), "离场用箭头"
    signals = [m for m in plain if m.startswith("circle")]
    assert signals, "信号用圆点"

    fills = [m for m in plain if not m.startswith("circle")]
    assert all(m.endswith("|") for m in fills), f"未选中时成交不该有价格文字: {fills}"

    entry = [m for m in picked if "开仓" in m]
    exit_ = [m for m in picked if "止盈" in m]
    assert entry and "77,010.2" in entry[0], picked
    assert exit_ and "77,400" in exit_[0], picked
    assert exit_[0].startswith("arrowDown"), "卖出离场箭头朝下"


def test_chart_note_counts_fills_separately(smoke: dict[str, object]) -> None:
    note = str(smoke["chart_note"])
    assert "成交 2 笔" in note
    # 常态不标价，就得告诉人怎么才能看到价 —— 否则"看不到成交价"这个抱怨会原样回来
    assert "已标价" in note or "点信号看成交价" in note, note


def test_rule_filter_can_be_refilled_without_a_page_reload() -> None:
    """信号流的「规则」筛选框数据来自页面加载时那一次 /api/meta。
    新建规则后不重填，那条规则要刷新整个页面才看得到 —— 用户真踩到过。

    这里只钉住「有一个可重填的入口，且重填不冲掉当前选中项」；
    端到端行为由真实浏览器验证（见 DEVLOG 2026-09-01）。
    """
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    assert "function fillRuleFilter()" in js, "填下拉框的逻辑要抽成函数，否则只能在 boot 里跑一次"
    assert "const keep = $(\"#f-rule\").value" in js, "重填必须保住用户已选的筛选项"
    body = js[js.index("async function saveRule()"):]
    body = body[: body.index("\n}\n")]
    assert "refreshMeta()" in body, "保存规则后要重新拉 meta，让盯盘页跟着变"


# ---- 信号提醒：弹窗 / 声音 / 语音 / 桌面通知 -------------------------------


def test_alerts_do_not_fire_for_historical_signals(smoke: dict[str, object]) -> None:
    """**最要命的一条**：开机时 /api/signals 会灌进几十条历史信号。
    那些一条都不能响 —— 否则每次打开面板都被一串弹窗和响声糊脸。
    只有 SSE 推来的才算新信号。"""
    a = smoke["alerts"]
    assert isinstance(a, dict), smoke["alert_error"]
    assert a["toasts_before"] == 0, "历史信号触发了弹窗"


def test_a_batch_of_signals_alerts_only_once(smoke: dict[str, object]) -> None:
    """多标的同刻收盘很常见。一根 bar 响五声等于噪音。
    （beeps 数是振荡器个数：一次 long 提示音由两个音符组成，所以 2 = 响了一次）"""
    a = smoke["alerts"]
    assert isinstance(a, dict)
    assert a["toasts_after"] == 2, "两条信号应各有一个弹窗"
    assert a["beeps_from_signals"] == 2, "两条信号只该响一次（一次 = 两个音符）"
    assert len(a["spoken"]) == 1, "语音也只播一次"
    assert "另有 1 条" in a["spoken"][0], "合并播报要说清楚还有几条"


def test_speech_says_symbol_direction_and_price(smoke: dict[str, object]) -> None:
    """播报要能不看屏幕就听懂：标的、方向、价格。"""
    a = smoke["alerts"]
    assert isinstance(a, dict)
    said = a["spoken"][0]
    assert "BTCUSDT" in said and "多" in said and "78,000" in said, said


def test_desktop_notification_stays_off_until_enabled(smoke: dict[str, object]) -> None:
    """桌面通知要授权，默认关。没开就一条都不该发。"""
    a = smoke["alerts"]
    assert isinstance(a, dict)
    assert a["notifications"] == 0


def test_enabling_sound_demonstrates_itself(smoke: dict[str, object]) -> None:
    """开关打开时立刻响一声 —— 否则你不知道它到底会不会响，
    而且这一下点击正是 AudioContext 需要的用户手势。"""
    a = smoke["alerts"]
    assert isinstance(a, dict)
    assert a["beeps_on_toggle"] == 2


def test_alert_apis_are_all_guarded() -> None:
    """无痕模式、非 https、用户拒绝授权、沙箱 —— 每个浏览器 API 都可能缺席，
    任何一个缺失都不能让面板起不来。"""
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    for api in ("localStorage", "Notification", "speechSynthesis", "AudioContext"):
        assert api in js
    alerts = js[js.index("const ALERT_KEYS"):js.index("function connectSSE()")]
    assert alerts.count("try {") >= 5, "每处浏览器 API 调用都要有 try/catch"
    assert 'typeof Notification !== "undefined"' in alerts
    assert 'typeof speechSynthesis === "undefined"' in alerts


def test_every_queried_id_exists_in_the_markup(smoke: dict[str, object]) -> None:
    """**DOM 桩对任意选择器都返回节点** —— HTML 里少一个元素，桩化冒烟完全看不出来，
    真实浏览器一跑就白屏。`#grid-toggle` 就是这么漏过去的：字符串替换没匹配上
    （缩进差两格），按钮压根没插进 index.html，而冒烟照样绿。

    所以把 app.js 真正查过的 #id 拿去和 index.html 对一遍。
    """
    assert smoke["missing_ids"] == [], (
        f"app.js 查了这些 id，但 index.html 里没有：{smoke['missing_ids']}")


# ---- 九宫格：同标的 9 个周期同屏 ------------------------------------------


def test_grid_builds_one_cell_per_timeframe(smoke: dict[str, object]) -> None:
    """看大做小要的是**同时**看见大级别形态和小级别时机 ——
    来回切周期会丢掉"大级别此刻长什么样"，那正是这个视图要解决的事。"""
    g = smoke["grid"]
    assert isinstance(g, dict), smoke["grid_error"]
    assert g["cells"] == ["分时", "1m", "5m", "15m", "30m", "1h", "1d", "1w", "1mon"]
    assert g["loaded"] == 9, "每格都要真的把数据画上去"


def test_zooming_a_cell_toggles_and_returns(smoke: dict[str, object]) -> None:
    """放大是"这一格铺满、其余隐藏"，再点一次还原 ——
    不是切回单图页：看完这一格还要回到九宫格继续扫。"""
    g = smoke["grid"]
    assert isinstance(g, dict)
    assert g["zoomed_before"] is None
    assert g["zoomed_after"] == "1d"
    assert g["zoomed_toggled_off"] is None
    assert g["off"] is False, "退出九宫格后要能切回单图"


def test_grid_and_single_chart_are_mutually_exclusive() -> None:
    """两个视图共用同一块区域，必须互斥显示，且**周期按钮在九宫格里没有意义**。"""
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    body = js[js.index("async function toggleGrid("):]
    body = body[: body.index("\n}\n")]
    assert '$("#grid").hidden = !G.on' in body
    assert '$("#chart").hidden = G.on' in body
    assert '$("#tf-group").hidden = G.on' in body


def test_grid_cells_do_not_reuse_the_timeframe_button_class() -> None:
    """格子标题曾用 `.tf`，与顶部周期按钮撞名 —— 选择器互相误伤，
    截图脚本点 `.tf` 直接超时。两个不同的东西不该共用类名。"""
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    assert 'class="cell-tf mono"' in js
    css = pathlib.Path("src/sigdesk/web/static/styles.css").read_text(encoding="utf-8")
    assert ".cell-head .cell-tf" in css and ".cell-head .tf{" not in css


def test_grid_cells_zoom_on_double_click_not_single() -> None:
    """单击要留给"停在这一格看十字线、对比多周期" —— 单击就放大等于
    根本没法在小格里看盘（用户实际用起来第一件事就撞上了）。"""
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    cell = js[js.index("function gridCell("):]
    cell = cell[: cell.index("\n}\n")]
    assert "root.ondblclick" in cell
    assert "root.onclick" not in cell, "单击不能放大"


def test_grid_skips_markers_when_there_are_too_few_bars() -> None:
    """lightweight-charts 的标注尺寸随 bar 宽度缩放：只有一两根 bar 时
    箭头会被撑到半个格子，把 K 线盖住，还让人误以为那是个特别重要的信号。
    （加密只有两天数据，1d/1w/1mon 三格当场就撞上了。）"""
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    assert "MARKER_MIN_BARS" in js
    load = js[js.index("async function loadCell("):]
    load = load[: load.index("\n}\n")]
    assert "data.bars.length < MARKER_MIN_BARS ? []" in load


def test_intraday_cell_draws_two_lines_not_candles() -> None:
    """分时是当日 1m 的一种画法（价格线 + 均价线），不是 K 线 ——
    也不是一个"周期"，所以不占 Timeframe 枚举。"""
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    cell = js[js.index("function gridCell("):]
    cell = cell[: cell.index("\n}\n")]
    assert "addLineSeries" in cell and "isIntraday" in cell
    load = js[js.index("async function loadIntradayCell("):]
    load = load[: load.index("\n}\n")]
    assert "/api/intraday" in load
    assert "p.avg !== null" in load, "均价算不出来的点不该画，更不能用价格冒充"


def test_symbol_picker_marks_unwatched_symbols() -> None:
    """两个标记各回答一个问题：`主连` = 不可下单；`未盯` = 盯盘不采它的行情。
    少了「未盯」，用户选中一个没人盯的标的只会看到空图，然后以为是行情连不上。"""
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    assert 's.watched === false ? " · 未盯" : ""' in js
    load = js[js.index("async function loadChart("):]
    load = load[: load.index("\n}\n")]
    assert "meta.watched === false" in load, "空状态要按有没有人盯分开说"
    assert "不采集" in load and "universe" in load, "要说清楚为什么空、以及怎么办"


# ---- 多周期十字线同步 -----------------------------------------------------


def test_crosshair_syncs_to_the_containing_bar_of_each_timeframe(
    smoke: dict[str, object],
) -> None:
    """**跨周期必须对齐到"包含该时刻的那根 bar"**：1m 上的 10:03 在 1d 上不是一根 bar，
    把原时刻直接塞给日线图，十字线要么不显示、要么落在错的位置。
    取"最后一根 time <= 目标"的 bar 才对。

    真实浏览器验证过：锁定 30m 的 22 May 10:00，1d 落在 21 May 15:00、
    1w 落在 15 May 15:00、1mon 落在 30 Apr 15:00（见 DEVLOG 2026-09-01）。
    """
    sync = smoke["sync"]
    assert isinstance(sync, dict), smoke["grid_error"]
    assert sync["set"], "其余格子应被设置十字线"
    assert sync["all_at_or_before"] is True, "对齐到的 bar 不能晚于目标时刻"
    assert sync["source_untouched"] is True, "源格子由图表自己画，不该被覆盖"


def test_clearing_the_crosshair_clears_every_cell(smoke: dict[str, object]) -> None:
    sync = smoke["sync"]
    assert isinstance(sync, dict)
    assert sync["cleared"] == 9, "九格都要清"


def test_crosshair_sync_guards_against_reentry() -> None:
    """程序设置十字线会**再次触发** crosshairMove 回调 —— 不加锁就是无限递归。"""
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    assert "G.syncing" in js
    fn = js[js.index("function syncCrosshair("):]
    fn = fn[: fn.index("\n}\n")]
    assert "G.syncing = true" in fn and "finally" in fn, "异常时也必须解锁"


def test_shift_click_pins_and_escape_releases() -> None:
    """悬停跟随、shift+单击锁定、Esc 返回。
    放大后没有可见的关闭按钮，只能双击 —— 全屏状态下很不直觉（用户反馈）。"""
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    assert "ev.shiftKey" in js and "G.pinned" in js
    esc = js[js.index("function onGridKey("):]
    esc = esc[: esc.index("\n}\n")]
    assert 'ev.key !== "Escape"' in esc
    assert "G.pinned = null" in esc and "zoomCell(G.zoomed)" in esc, "Esc 先解锁，再退出放大"
    assert 'document.addEventListener("keydown", onGridKey)' in js


def test_rule_filter_includes_retired_rules_with_counts(smoke: dict[str, object]) -> None:
    """**信号流里的规则 ∪ 当前加载的规则**，不能只取后者。

    实测：库里 50 条信号有 44 条来自 `multi-level-verify`，而它早已不在
    `config/rules/` 里 —— 只列已加载的规则，那 44 条就永远筛不出来；
    而列表里的 `ema-cross-long` 一条信号都没有，选中是空的。
    两种情况都要能一眼看出来，所以每条后面带条数、已删的标「已下线」。
    """
    html = str(smoke["rule_filter_html"])
    assert "retired-rule（1） · 已下线" in html, html
    assert "r1（4）" in html, "已加载的规则也要显示条数"
    assert html.index('value=""') < html.index("r1"), "「全部规则」要排在最前"


def test_empty_feed_explains_which_kind_of_empty() -> None:
    """选了某条规则却没内容，和"完全没有信号"是两回事，要分开说。"""
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    fn = js[js.index("function renderFeed()"):]
    fn = fn[: fn.index("\n}\n")]
    assert "还没有产生过信号" in fn and "还没有信号" in fn
    assert '$("#f-rule").value' in fn, "空态文案要看当前筛选的是哪条规则"


def test_rule_filter_is_refilled_after_signals_load() -> None:
    """`fillRuleFilter` 要统计信号条数，就必须在 `S.signals` 之后再填一次 ——
    boot 前面那次拿不到信号，计数会全是 0、下线规则也进不来（真踩过）。"""
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    boot = js[js.index("async function boot()"):]
    load_at = boot.index('S.signals = (await api("/api/signals')
    assert "fillRuleFilter();" in boot[load_at:load_at + 400], "信号加载后要重填筛选框"


def test_moving_averages_skip_warmup_instead_of_drawing_zero(
    smoke: dict[str, object],
) -> None:
    """预热期服务端返回 null，前端必须**跳过**这些点。
    补 0 会在图左端画出一条从零飙起来的假线（ADR-0006 的同一条原则）。
    夹具：SMA5 是 [null, 1.65] -> 只画 1 个点；SMA20 全 null -> 0 个点。"""
    lines = smoke["ma_lines"]
    assert isinstance(lines, list) and lines[:2] == [1, 0], lines


def test_volume_moving_average_is_drawn_on_the_volume_scale(
    smoke: dict[str, object],
) -> None:
    """量能均线必须挂在**成交量那条价格轴**上（priceScaleId 与量柱相同），
    否则它会按价格刻度画，直接飞出画面。

    窗口 20 也不是随便挑的：内置规则 `volume-spike` 判的就是
    `volume > sma(volume, 20) * 2.5` —— 图上这条线和规则看的必须是同一个数。
    """
    vlines = smoke["vma_lines"]
    assert isinstance(vlines, list) and vlines, "量均线没画出来"
    assert vlines[0] == 1, "夹具 VSMA20 是 [null, 3.5]，跳过 null 后只画 1 个点"
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    fn = js[js.index("function drawVolumeMa("):]
    fn = fn[: fn.index("\n}\n")]
    assert "priceScaleId: VOLUME_SCALE" in fn


def test_volume_bars_follow_candle_direction(smoke: dict[str, object]) -> None:
    """量柱跟着当根阴阳走 —— 和 K 线一个颜色语言，不用另记一套。"""
    assert smoke["volume_points"] == 2
    assert smoke["volume_colors"] >= 1


def test_ma_series_are_reused_not_recreated() -> None:
    """均线 series 要复用：每次载入都 addLineSeries，切几次周期就堆出几十条隐形序列。"""
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    fn = js[js.index("function drawOverlays("):]
    fn = fn[: fn.index("\n}\n")]
    assert "while (store.length < lines.length)" in fn, "只在不够时才新建"
    assert "for (let i = lines.length; i < store.length" in fn, "多余的要清空而不是留着"


def test_ma_legend_floats_over_the_chart_not_in_the_header() -> None:
    """四条均线值放进 chart-head 会把标的选择器和周期按钮挤到换行
    （截图当场看到「九宫格」按钮被压成竖排）。浮在图左上角是看盘软件的惯例位置。"""
    html = pathlib.Path("src/sigdesk/web/static/index.html").read_text(encoding="utf-8")
    assert 'id="ma-legend"' in html
    assert html.index('id="ma-legend"') > html.index('<div id="chart"'), "要在图表容器内"
    css = pathlib.Path("src/sigdesk/web/static/styles.css").read_text(encoding="utf-8")
    legend = css[css.index(".ma-legend{"):]
    assert "z-index:3" in legend, "要盖过 lightweight-charts 的 canvas（z-index 1/2）"
