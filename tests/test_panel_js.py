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
import re
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
    assert "rb2610" in pos and "空" in pos, "持仓方向要能看出多空"
    fills = str(smoke["trade_fills_html"])
    assert "开仓" in fills and "止盈" in fills, "成交性质要翻成人话"
    assert "k1" in fills, "每笔成交要标明来源信号（成交与信号一一对应）"
    rejects = str(smoke["trade_rejects_html"])
    assert "单笔风险超限" in rejects, "拒单原因要翻成人话，不能只显示 per_trade_risk"


def test_panel_calls_every_endpoint_it_needs(smoke: dict[str, object]) -> None:
    """接口名写错在浏览器里只会安静地少一块内容，这里要当场发现。"""
    assert set(smoke["endpoints"]) >= {  # type: ignore[arg-type]
        "/api/meta", "/api/signals", "/api/bars", "/api/markers",
        "/api/health", "/api/events", "/api/trade",
    }


def test_direction_is_drawn_as_svg_not_a_dingbat(smoke: dict[str, object]) -> None:
    """方向用 SVG 画，不用 ▲▼ 字符：字符在不同字体下大小与基线会飘，也不好上色。"""
    html = str(smoke["feed_html"]) + str(smoke["detail_html"])
    assert "<svg" in html and "polygon" in html
    assert "▲" not in html and "▼" not in html


def test_feed_rows_carry_symbol_price_rule_and_time(smoke: dict[str, object]) -> None:
    html = str(smoke["feed_html"])
    assert "rb2610" in html
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

def test_markers_come_from_the_server(smoke: dict[str, object]) -> None:
    """分桶、折叠与配对都在服务端做，前端只画。夹具给 4 条信号（1 条落在本周期外、
    2 条叠在同一根 bar 上折成一枚）+ 2 笔成交，所以图上是 3 枚信号标记 + 2 个成交标记。"""
    ops = smoke["marker_ops"]
    assert isinstance(ops, list)
    assert len([o for o in ops if str(o).startswith("signal|")]) == 3, ops
    assert len([o for o in ops if str(o).startswith("fill|")]) == 2, ops
    note = str(smoke["chart_note"])
    # 折叠后"标记枚数 < 信号条数"是正常的，必须说清差额去哪了 ——
    # 光写"标注 3/4"会被读成"1 条丢了"。
    assert "标注 3 枚 / 信号 4 条" in note, note
    assert "折成 ×N" in note, f"折叠了却没说，差额会被误读成丢信号: {note}"
    assert "不在本周期序列内" in note, "被丢弃的信号没有如实提示"


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


def _ops(smoke: dict[str, object], kind: str) -> list[list[str]]:
    """自绘层录下的绘制记录，按类型取。格式见 createMarkerLayer 里的 ops.push。"""
    out = []
    for op in smoke["marker_ops"]:  # type: ignore[union-attr]
        parts = str(op).split("|")
        if parts[0] == kind:
            out.append(parts)
    return out


# 夹具里坐标是**确定的假映射**：priceToCoordinate = 1000 - 价格 × 0.01
def _y(price: float) -> str:
    return f"{1000 - price * 0.01:.1f}"


def test_markers_are_anchored_to_the_actual_price(smoke: dict[str, object]) -> None:
    """**买卖点画在真实价格上。**

    这是内置 `setMarkers` 做不到的一条：它的位置只能是 aboveBar / belowBar / inBar，
    徽章贴的是那根 bar 的最高/最低点，**不是触发价、也不是成交价**。
    换了形状和文字也还是"跟之前一样"（用户原话："这个买卖点也没有和准确的价格对应"）。
    自绘层用 priceToCoordinate，几何才真正锚在价格上。
    """
    sig = _ops(smoke, "signal")
    assert sig, "一个信号都没画"
    by_dir = {p[1]: p for p in sig}
    # 夹具三条信号的触发价：多 77000.5、空 77100.0、中性 77050.0
    assert by_dir["long"][2].split(",")[1] == _y(77000.5), by_dir["long"]
    assert by_dir["short"][2].split(",")[1] == _y(77100.0), by_dir["short"]
    assert by_dir["neutral"][2].split(",")[1] == _y(77050.0), by_dir["neutral"]

    fills = {p[1]: p for p in _ops(smoke, "fill")}
    assert fills["entry"][2].split(",")[1] == _y(77010.2), fills["entry"]
    assert fills["target"][2].split(",")[1] == _y(77400.0), fills["target"]


def test_markers_are_anchored_to_the_right_bar(smoke: dict[str, object]) -> None:
    """横向也要对：分桶在服务端算好，前端按 timeToCoordinate 落位，不做就近吸附。
    夹具里每分钟 20px，起点是第一根 bar。"""
    by_dir = {p[1]: p for p in _ops(smoke, "signal")}
    assert by_dir["long"][2].split(",")[0] == "0.0", by_dir["long"]      # 1788139800
    assert by_dir["short"][2].split(",")[0] == "20.0", by_dir["short"]   # 1788139860
    assert by_dir["neutral"][2].split(",")[0] == "40.0", by_dir["neutral"]  # 1788139920


def test_signal_directions_render_differently(smoke: dict[str, object]) -> None:
    """买卖点用**圆形徽章 + B/S 字母**，不用箭头。

    箭头只有朝向之分：一屏全是多头信号时看着就是一模一样的一片（用户原话
    「现在 K 线图上全部是向上的箭头」）。字母是直接可读的。
    中性信号**不写字母** —— 它既不是买也不是卖，硬安一个字母是撒谎。
    """
    by_dir = {p[1]: p for p in _ops(smoke, "signal")}
    assert by_dir["long"][3].startswith("B"), by_dir["long"]
    assert by_dir["short"][3] == "S", by_dir["short"]
    assert by_dir["neutral"][3] == "", f"中性不该有字母: {by_dir['neutral']}"


def test_collapsed_markers_show_a_multiplier(smoke: dict[str, object]) -> None:
    """同一根 bar 同方向的多条信号折成一枚「B×2」。

    密集处原本是几枚标记完全重叠、只看得见最上面那个 —— 看着像一条，实际是三条。
    """
    by_dir = {p[1]: p for p in _ops(smoke, "signal")}
    assert by_dir["long"][3] == "B×2", by_dir["long"]
    assert by_dir["long"][4] == "2"


def test_collapsed_marker_expands_into_a_clickable_list(smoke: dict[str, object]) -> None:
    """图上「×2」只画得下一枚代表，另外那条必须在详情里点得到。

    **缺了这一步，折叠就等于把信号藏起来** —— 那比不折叠更糟。
    """
    html = str(smoke["detail_html"])
    assert "同一根 bar 上共" in html, f"折叠标记没有展开列表: {html[:400]}"
    assert html.count('class="sib') >= 2, "展开列表没把每条成员都列出来"


def test_fill_markers_split_price_and_pnl_between_the_two_ends(
    smoke: dict[str, object],
) -> None:
    """成交两端分工：**开仓写价格，离场写盈亏**。

    两端都写"价格 + 盈亏"试过了 —— 密集处两个离场标签直接压在一起。
    胶囊有底片压得住 K 线了，但底片不解决"两个胶囊互相盖"，字短才解决。
    夹具里 k1 是选中的那条，所以两端都写全（档位词 + 价格 + 盈亏）。
    """
    fills = {p[1]: p for p in _ops(smoke, "fill")}
    assert "77,010.2" in fills["entry"][3], fills["entry"]
    assert "+0.47%" in fills["target"][3], fills["target"]
    assert fills["target"][4] == "pill", "选中那笔的成交要带胶囊，不能只剩锚点"


def test_fill_pills_degrade_to_a_dot_when_they_would_collide() -> None:
    """胶囊撞了就退化成只留锚点。

    **锚点永远画** —— 它才是"成交发生在这个价"的证据；能省的只有价格文字。
    密集处宁可少几个价格，也不要糊成一团（内置 marker 时代就是糊成一团）。
    """
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    fn = js[js.index("function pill("):]
    fn = fn[: fn.index("\n}\n")]
    assert "rects.some(" in fn, "没有做碰撞检测"
    assert "return false" in fn, "撞了要告诉调用方，好退化成锚点"


def test_exit_marker_carries_the_trade_pnl(smoke: dict[str, object]) -> None:
    """离场那枚挂上这笔的盈亏 —— 方案 C 的核心："这笔赚还是亏"一眼看完。"""
    fills = {p[1]: p for p in _ops(smoke, "fill")}
    assert "+0.47%" in fills["target"][3], fills["target"]


def test_trade_band_connects_entry_to_exit(smoke: dict[str, object]) -> None:
    """成对交易带：开仓与它的离场连成一笔，两端锚在**成交价**上，颜色随盈亏。

    夹具里 k1 盈利平仓、k3 仍持仓 —— **持仓中的不画连线**：它还没有终点，
    从开仓拉一条线到最后一根 bar 会被读成"在那里平掉了"，那是假的。
    """
    bands = _ops(smoke, "band")
    assert len(bands) == 1, f"只有一笔已平交易，就该只有一条带: {bands}"
    b = bands[0]
    assert b[1] == f"20.0,{_y(77010.2)}", b      # 开仓：时刻 + 成交价
    assert b[2] == f"200.0,{_y(77400.0)}", b     # 平仓：时刻 + 成交价
    assert b[3] == "#26a69a", f"这笔盈利，应当是绿的: {b}"


def test_every_chart_uses_the_same_marker_layer(smoke: dict[str, object]) -> None:
    """九宫格与单图**共用一套画法**。

    两处各画一套的话，同一条信号在单图和格子里会落在不同高度 ——
    那是最容易被读错的一种不一致，而且不会报错。
    夹具：单图 1 层 + 九周期模式 8 层（分时那格是折线，没有信号标记）
    + 预警组 3 层（夹具里三个标的）= 12。
    """
    assert smoke["marker_layers"] == 12, smoke["marker_layers"]


def test_chart_note_counts_trades_and_open_positions(smoke: dict[str, object]) -> None:
    note = str(smoke["chart_note"])
    assert "交易 2 笔" in note, note
    # 持仓中的交易**不画连线**（它还没有终点，画到最后一根会被读成"在那里平掉了"），
    # 所以必须在文字里说清楚，否则会被当成漏画。
    assert "持仓中" in note, f"有持仓中的交易却没说明为什么图上没连线: {note}"


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
    元素也可以由 app.js 自己生成（如链路折叠按钮），那种同样算"存在"——
    只认静态 HTML 会把动态元素误报成缺失。
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
    cell = js[js.index("function makeCell("):]
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
    cell = js[js.index("function makeCell("):]
    cell = cell[: cell.index("\n}\n")]
    assert "addLineSeries" in cell and "isIntraday" in cell
    load = js[js.index("async function loadIntradayCell("):]
    load = load[: load.index("\n}\n")]
    assert "/api/intraday" in load
    assert "p.avg !== null" in load, "均价算不出来的点不该画，更不能用价格冒充"


def test_symbol_picker_is_grouped_by_whether_it_ever_fired(
    smoke: dict[str, object],
) -> None:
    """图表的标的下拉框分两组。

    「有信号」那组才是平时要切的（跟信号流、预警组同一批标的）；
    「其他已注册」保留"去看一个从没预警过的标的"的能力 —— 那是这个框
    唯一独有的作用（用来判断是规则太严还是行情真没走出形态）。

    两类混在一起时，冷启动看到的是一串一模一样的选项，点进去全是空图，
    用户会以为面板坏了（实际发生过）。
    """
    html = str(smoke["symbol_options_html"])
    assert '<optgroup label="有信号">' in html and '<optgroup label="其他已注册">' in html
    fired = html[html.index("有信号"):html.index("其他已注册")]
    assert "BTCUSDT.PERP（2）" in fired, f"有信号的要带条数: {fired}"
    assert "rb.CONT" not in fired, "从没触发过的不该进「有信号」组"


def test_symbol_picker_says_why_a_symbol_would_be_empty(
    smoke: dict[str, object],
) -> None:
    """**每一项都要标出"点进去可能是空的"的原因。**

    - `无数据`：本地一根 bar 都没有（行情没接入 / 没回补）
    - `数据止于 X`：有数据但停更了（主连这种派生序列不随盘更新）
    - `未盯`：没有规则盯它 ⇒ 盯盘进程根本不采它
    - `主连`：拼接序列，可看图可回测但**不可下单**

    前三个都是空图的成因。不标出来的话用户只会以为行情连不上 ——
    冷启动时这是最误导人的一点（用户实际撞上了：加密没接入，
    但规则盯着 BTC/ETH，于是下拉框里干干净净什么标记都没有）。
    """
    html = str(smoke["symbol_options_html"])
    assert "· 无数据" in html, f"一根 bar 都没有的标的没标出来: {html}"
    assert "· 数据止于 05-29" in html, "停更的没标出来"
    assert "· 主连" in html


def test_signal_filter_is_built_from_the_signals_it_filters(
    smoke: dict[str, object],
) -> None:
    """信号流的「全部标的」筛选按**信号条数**生成，与旁边的「全部规则」同源。

    原来它照抄注册表 —— 筛选项和被筛的数据不是一回事，选一个从没触发过的
    标的必然是空列表，而下拉框刚才还让你以为那里有东西。
    """
    html = str(smoke["symbol_filter_html"])
    assert '<option value="">全部标的</option>' in html
    assert "（2）" in html, f"筛选项要带条数: {html}"
    assert "rb.CONT" not in html, "从没触发过的标的不该出现在筛选框里"
    assert "optgroup" not in html, "筛选框不分组 —— 它本来就只列有信号的"


def test_chart_empty_state_explains_why(smoke: dict[str, object]) -> None:
    """空状态要说清楚**为什么**空、以及下一步做什么。
    只说"没有数据"的话，用户只会以为行情连不上（真发生过）。"""
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    load = js[js.index("async function loadChart("):]
    load = load[: load.index("\n}\n")]
    assert "meta.watched === false" in load, "空状态要按有没有人盯分开说"
    assert "不采集" in load and "universe" in load, "要说清楚为什么空、以及怎么办"
    # 一个标的都没有数据时（全新安装）不该默认选一个空的让人以为坏了
    assert "function showNoDataAtAll(" in js
    assert "backfill.py" in js and "--crypto-only" in js, "要给出两个市场各自的下一步"


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
    assert 'ev.key === "Escape"' in esc
    # 断言的是**顺序**不是写法：先解锁十字线，再退放大。倒过来就得按两次 Esc 才回得去。
    assert esc.index("G.pinned = null") < esc.index("zoomCell(G.zoomed)")
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
    夹具 5 根 bar：SMA5 首根 null -> 画 4 个点；SMA20 全 null -> 0 个点。"""
    lines = smoke["ma_lines"]
    assert isinstance(lines, list) and lines[:2] == [4, 0], lines


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
    assert vlines[0] == 4, "夹具 VSMA20 首根是 null，跳过后 5 根画 4 个点"
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    fn = js[js.index("function drawVolumeMa("):]
    fn = fn[: fn.index("\n}\n")]
    assert "priceScaleId: VOLUME_SCALE" in fn


def test_volume_bars_follow_candle_direction(smoke: dict[str, object]) -> None:
    """量柱跟着当根阴阳走 —— 和 K 线一个颜色语言，不用另记一套。"""
    assert smoke["volume_points"] == 5
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


# ------------------------------------------- 预警组（网格的第二种模式）


def _w(smoke: dict[str, object]) -> dict[str, object]:
    w = smoke["watch"]
    assert isinstance(w, dict), smoke["grid_error"]
    return w


def test_watchlist_is_the_default_grid_mode(smoke: dict[str, object]) -> None:
    """打开网格默认落在**预警组**：日常最先要回答的是"现在有哪几个值得看"，
    而不是"这一个标的的九个周期长什么样"。后者是下一步。"""
    w = _w(smoke)
    assert w["mode"] == "watch"
    assert w["tf"] == "5m", "默认周期是找买点的级别"


def test_watchlist_cells_are_one_per_symbol(smoke: dict[str, object]) -> None:
    """九标的×一周期 —— 与「一标的×九周期」正好互补。"""
    w = _w(smoke)
    assert w["symbols"] == ["CN.SHFE.rb2610", "CN.SHFE.rb.CONT", "CN.SHFE.ag2612"]
    # 夹具给 3 个标的、9 个槽位 -> 6 个空槽。空槽要看着像「留着位子」，不像「坏了」
    assert w["slots"] == 6


def test_pinned_cells_are_marked(smoke: dict[str, object]) -> None:
    """钉住 = 人工判断「还需要观察」，是这个功能里唯一需要人动手的操作。"""
    assert _w(smoke)["pinned"] == ["CN.SHFE.rb2610", "CN.SHFE.ag2612"]


def test_unread_clears_on_click_and_the_tab_count_follows(
    smoke: dict[str, object],
) -> None:
    """**未读状态是这个视图的关键。**

    没有它，扫第二遍时分不清哪个是新触发的、哪个是上次就看过的 ——
    九个小图长得都一样。点开即已读，tab 上的未读数跟着减。
    """
    w = _w(smoke)
    assert set(w["unread"]) == {"CN.SHFE.rb2610", "CN.SHFE.rb.CONT"}  # type: ignore[arg-type]
    assert w["unread_after"] == ["CN.SHFE.rb.CONT"], "点开的那格没有转成已读"
    assert '<span class="wl-n">2</span>' in str(w["tabs_html"])
    assert '<span class="wl-n">1</span>' in str(w["tabs_after"]), "tab 上的未读数没跟着减"


def test_market_tabs_only_badge_when_there_is_something_unread(
    smoke: dict[str, object],
) -> None:
    """未读数只在有未读时出现 —— 常驻一个 0 会让人以为一直有东西没看。"""
    html = str(_w(smoke)["tabs_html"])
    assert "期货" in html and "加密" in html
    assert 'class="wl-tab active" data-k="CN"' in html
    assert ">0<" not in html


def test_cell_says_why_it_is_in_the_group(smoke: dict[str, object]) -> None:
    """每格写出触发理由（规则 + 多久之前）。不写的话九个格子就是一堆
    无差别的缩略图，看不出谁为什么在这。

    没有信号的钉住项显示「手动钉住」，**不编一条不存在的规则出来**。
    """
    why = [str(x) for x in _w(smoke)["why"]]  # type: ignore[union-attr]
    assert "CN.SHFE.rb2610|kdzx-long|" in why[0], why
    assert why[2] == "CN.SHFE.ag2612|手动钉住|无规则", why


def test_unwatched_symbol_says_why_it_is_empty(smoke: dict[str, object]) -> None:
    """**没有规则盯 ⇒ 不采集 ⇒ 图永远是空的。**

    静默的空是这个项目栽过的坑（用户当时以为是行情连不上）。
    钉住一个没人盯的品种正好会撞上它，所以必须当场说清楚。
    """
    assert _w(smoke)["nodata"] == ["CN.SHFE.ag2612"]


def test_switching_modes_detaches_the_other_modes_cells(
    smoke: dict[str, object],
) -> None:
    """切模式时另一种模式的格子要从网格里摘掉（**不销毁图表**，切回来还要用）。

    摘不干净的话两种模式的格子会同时挂在网格里，越切越多。
    """
    w = _w(smoke)
    assert w["mode_after"] == "tf"
    assert w["wcells_detached"] is True, "切到周期模式后预警组的格子还挂在网格里"
    grid = smoke["grid"]
    assert isinstance(grid, dict) and len(grid["cells"]) == 9  # type: ignore[arg-type]


def test_loading_the_watchlist_does_not_clobber_the_selected_symbol(
    smoke: dict[str, object],
) -> None:
    """**载入预警组不许改全局 `S.symbol`。**

    九个格子是并发加载的。曾经靠"临时把 S.symbol 改成本格的标的、await 完再改回来"
    给 loadCell 传参 —— 并发下每个格子捕获到的"原值"是别人的值，最后一个还原的
    落地什么就剩什么。表现：下拉框切换标的后，左上角一直显示某个格子的标的
    （用户报的 bug）。修法不是加锁，是**格子自己带标的**，根本不碰全局。
    """
    w = _w(smoke)
    assert w["symbol_kept"] is True, "载入预警组把全局选中的标的改掉了"
    assert w["cell_symbols"] == w["symbols"], "格子没有各自带上自己的标的"


def test_grid_cells_allow_wheel_zoom() -> None:
    """九宫格里每一格都能滚轮缩放 / 拖动。

    原来是全关的（怕小格里误拖把视图弄乱），但那等于"九宫格里根本没法细看" ——
    用户第一件想做的事就是滚轮放大某一格。双击仍然留给"放大这一格"，
    所以要**显式关掉坐标轴的双击复位**，否则两个手势会打架。
    """
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    fn = js[js.index("function makeCell("):]
    fn = fn[: fn.index("\n}\n")]
    assert "mouseWheel: true" in fn, "滚轮缩放没打开"
    assert "axisDoubleClickReset: false" in fn, "双击要留给放大格子，不能被坐标轴复位抢走"
    # zoomCell 不该再开关交互 —— 现在一直开着
    zoom = js[js.index("function zoomCell("):]
    zoom = zoom[: zoom.index("\n}\n")]
    assert "handleScroll" not in zoom, "交互开关已经不归 zoomCell 管了"


def test_reference_mas_are_drawn_thicker(smoke: dict[str, object]) -> None:
    """跨周期均线（5m 图上叠 1h / 1d）要画得**明显比本级别均线粗**。

    它们代表的是更大的力量，视觉权重要相称；线细了就淹在本级别那几条里，
    等于没画（用户原话："粗一点方便我做判断"）。
    """
    widths = smoke["line_widths"]
    assert isinstance(widths, list)
    assert 1 in widths, "本级别均线仍是细线"
    assert max(widths) >= 3, f"跨周期均线没有加粗: {widths}"


def test_reference_mas_skip_warmup_like_normal_mas(smoke: dict[str, object]) -> None:
    """预热期的 None 同样要跳过，不能补 0 —— 补 0 会在图左端画出一条
    从零飙起来的假线（与本级别均线同一条纪律，ADR-0006）。

    夹具：1h EMA20 是 [null,…] 共 5 个值 -> 画 4 个点；1d SMA20 前两个 null -> 3 个点。
    """
    lines = smoke["ma_lines"]
    assert isinstance(lines, list)
    assert lines[:4] == [4, 0, 4, 3], lines


def test_reference_mas_are_drawn_as_steps() -> None:
    """跨周期均线画成**明确的阶梯**（LineType.WithSteps）。

    它本来就是台阶：一根 1h 均线的值在整个下一小时里都是同一个数，
    到下一根 1h 收盘才跳。用普通折线画，每次跳变会连出一段陡斜线，
    缩小看就是一片锯齿 —— 看着像"想画平滑却在抖"，其实是画对了。

    **不许为了好看改成平滑曲线**：那等于在两次收盘之间显示一个当时还不知道的
    中间值，是视觉上的未来泄露（与 align_as_of 守的是同一件事）。
    """
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    fn = js[js.index("function drawRefMa("):]
    fn = fn[: fn.index("\n}\n")]
    assert "lineType: 1" in fn, "没画成阶梯线"
    assert "lineType: 2" not in fn, "曲线插值会在两次收盘之间显示未知的中间值"


def test_short_symbol_drops_market_and_exchange() -> None:
    """显示用短名只留合约代码，去掉市场与交易所。

    交易所名对看盘没有信息量（合约代码本身就唯一），而 60 多个标的时它占的宽度
    会把预警组格子头部和信号流挤到换行。

    **主连是四段**（`CN.SHFE.rb.CONT`），只取末段会变成孤零零的 `CONT`、丢掉品种 ——
    所以规则是"去掉前两段"，不是"取最后一段"。
    """
    js = pathlib.Path("src/sigdesk/web/static/app.js").read_text(encoding="utf-8")
    line = next(ln for ln in js.splitlines() if ln.startswith("const shortSym"))
    assert "slice(2)" in line, f"应当去掉前两段: {line}"
    assert "slice(-1)" not in line and "[parts.length - 1]" not in line


def _code_lines(src: str) -> str:
    """去掉整行注释再扫调用点：注释里提到 `send()` 会被当成一处真调用。

    按行过滤，不做词法分析 —— 手写 JS 词法器会被正则字面量和模板串里的引号带偏
    （试过，它在某处卡进了引号状态，于是后面的注释一条都没剥掉）。
    调用点本来就不写在行尾注释里，按行足够。
    """
    keep = [ln for ln in src.splitlines()
            if not ln.lstrip().startswith(("//", "*", "/*"))]
    return "\n".join(keep)


def _args_at(src: str, call: str) -> list[list[str]]:
    """把 `call(` 的每处调用切成顶层实参列表。

    用括号配对而不是正则：实参里有模板串和嵌套调用
    （`` api(`/x?s=${encodeURIComponent(v)}`) ``），正则按逗号切会切错。
    """
    out: list[list[str]] = []
    i = 0
    while (i := src.find(call + "(", i)) != -1:
        before = src[i - 1] if i else " "
        i += len(call) + 1
        if before.isalnum() or before in "_$.":   # 跳过 xxxsend( / obj.api( 这类同名尾巴
            continue
        depth, cur, args, quote = 0, "", [], ""
        while i < len(src):
            ch = src[i]
            if quote:
                if ch == "\\":
                    cur += src[i:i + 2]
                    i += 2
                    continue
                if ch == quote:
                    quote = ""
            elif ch in "\"'`":
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    args.append(cur.strip())
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                args.append(cur.strip())
                cur = ""
                i += 1
                continue
            cur += ch
            i += 1
        out.append(args)
    return out


def test_send_and_api_call_sites_match_their_signatures() -> None:
    """两个 helper 长得像、签名不一样，**用混了两条都是静默失败**。

    `api(path)` 只会 GET；带动词必须用 `send(method, path, body)`。
    钉住按钮曾经两条分支各错各的：钉住把 path 传成了 method（fetch 拿到非法动词，
    请求根本没发出去），取消钉住给 api 传了个它压根不看的 `{method:"DELETE"}`
    （于是发成 GET，撞 405）。两条都不报错、不进控制台，按钮看着就是"点了没反应"。

    所以这里不点名某一行，而是**把两个 helper 的调用点都过一遍**。

    局限：只看 `await` 过的调用点（这样才不用分辨注释和字符串里提到的 "send()"）。
    没 await 的调用扫不到 —— 但那本身就是另一个 bug（错误没人接得住）。
    """
    js = _code_lines(APP.read_text(encoding="utf-8"))
    verbs = {'"GET"', '"POST"', '"PUT"', '"DELETE"', '"PATCH"'}

    # 扫 `await send(` 而不是 `send(`：函数定义本身、以及错误消息字符串里
    # 提到的 "send()" 都自然被排除，不用去分辨引号和注释。
    bad_send = [a for a in _args_at(js, "await send") if not a or a[0] not in verbs]
    assert not bad_send, f"send() 第一个参数必须是 HTTP 动词字面量，这些不是：{bad_send}"

    bad_api = [a for a in _args_at(js, "await api") if len(a) > 1]
    assert not bad_api, f"api() 只接受 path，多给的参数会被静默忽略：{bad_api}"

    # 光靠测试守不住 —— 运行时也得炸，不然下次直接在浏览器里静默走 GET
    assert "api() 只接受 path" in APP.read_text(encoding="utf-8"), \
        "api() 里要有多参即抛的运行时保护"


def test_pin_button_is_a_reachable_hit_target() -> None:
    """图标 11px，但**可点区域不能也是 11px** —— 九宫格里几乎点不中（用户反馈"点击不了"）。

    `flex:none` 同样是必须的：头部是 nowrap + overflow:hidden，
    能压缩的 flex item 会在窄格子里被挤到没有宽度，那就真的点不着了。
    """
    css = pathlib.Path("src/sigdesk/web/static/styles.css").read_text(encoding="utf-8")
    block = css[css.index(".cell-pin{"):]
    block = block[: block.index("}")]
    assert "flex:none" in block, "钉图标会被窄格子挤扁"
    size = [int(v) for v in re.findall(r"(?:width|height):(\d+)px", block)]
    assert size and min(size) >= 24, f"点击靶子太小：{size}（至少 24px）"


def test_chart_head_buttons_do_not_wrap() -> None:
    """chart-head 是 flex 行，**不写 flex:none 就会被压到最小宽度**，
    「九宫格」三个字竖着排成三行。加「⌨」之前它已经折成两行了，只是没人注意到。

    同一个坑在 cell-head 上也踩过（那边靠 flex-wrap:nowrap 钉住）。
    """
    css = pathlib.Path("src/sigdesk/web/static/styles.css").read_text(encoding="utf-8")
    for sel in ("#grid-toggle", "#keys-btn"):
        block = css[css.index(sel + "{"):]
        block = block[: block.index("}")]
        assert "flex:none" in block, f"{sel} 会在窄头部里被压扁"
    assert "white-space:nowrap" in css[css.index("#grid-toggle{"):][:120]


def test_clock_shows_market_time_not_utc() -> None:
    """右上角时钟要走**市场时间（CST）**，不是 UTC。

    原来显示 `toISOString()` 的 UTC 时间，比国内用户的墙上钟慢 8 小时；
    而面板上期货的每个时间戳都是 CST —— 全屏就这一个表跟别人不一致（用户报的）。
    """
    js = APP.read_text(encoding="utf-8")
    tick = js[js.index("const tick = () => {"):]
    tick = tick[: tick.index("\n  };")]
    assert "8 * 3600 * 1000" in tick, "时钟没有换算到 CST"
    assert "CST" in tick and "UTC" in tick, "要标出时区，并把 UTC 留在 title 里"
