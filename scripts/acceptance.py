#!/usr/bin/env python
"""离线验收：一条命令跑完，输出通过/失败清单。

**给 agent 用的**：不需要理解散文清单，跑完看 exit code 与末尾表格即可。
全程离线 —— 不联网、不需要任何凭据、不下任何单。

用法：
    python scripts/acceptance.py              # 全部
    python scripts/acceptance.py --no-visual  # 跳过截图（没装 node/浏览器时）
    python scripts/acceptance.py --only api   # 只跑某一组

Windows 注意：先 `set PYTHONUTF8=1`（脚本里有 ✅ 这类字符，
cp936 控制台不设会 UnicodeEncodeError）。run_acceptance.bat 已经代劳。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = sys.executable
# (组, 项, 状态, 说明)；状态 = pass / fail / skip
results: list[tuple[str, str, str, str]] = []


def check(group: str, name: str, ok: bool, detail: str = "") -> bool:
    results.append((group, name, "pass" if ok else "fail", detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail and not ok else ""))
    return ok


def skip(group: str, name: str, reason: str) -> None:
    """**环境缺东西不算失败。** 记成失败会让每台新机器都冒出假红，
    真正的缺陷就淹没在里面了 —— 跳过项单独列出，并写清楚怎么补齐。"""
    results.append((group, name, "skip", reason))
    print(f"  ⏭️  {name}  —— 跳过：{reason}")


# 离线验收的样本标的。**不在 git 里**（data/ 是数据不是代码），而且它已经从
# symbols.yaml 摘掉了（主连不可下单），所以任何一台新机器 backfill 都不会生成它。
SAMPLE_UID = "CN.SHFE.rb.CONT"
SAMPLE_HOWTO = (
    f"缺少离线样本数据 {SAMPLE_UID}。它不随仓库分发，用 "
    f"`scripts/build_continuous.py {SAMPLE_UID} 2025-01-01 2026-08-31` 生成后再跑。"
)


def sample_ready() -> bool:
    """样本数据在不在。**不在就整组跳过，不是记失败** —— 见 skip() 的说明。

    踩过：Windows 上没有这份数据，于是数据组两条红、引擎组 KeyError、
    API 组连锁超时，**一共 7 条假红**，真正该看的东西全淹了。
    脚本自己的原则写在 skip() 里，这里当时没照做。
    """
    base = ROOT / "data" / "bars" / SAMPLE_UID.split(".", 1)[0] / SAMPLE_UID
    return base.is_dir() and any(base.rglob("*.parquet"))


def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}, **kw,  # type: ignore[arg-type]
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ------------------------------------------------------------------ 各组


def group_quality() -> None:
    print("\n[1/5] 代码质量")
    r = run([PY, "-m", "pytest", "-q"])
    tail = (r.stdout or r.stderr).strip().splitlines()[-1:] or [""]
    check("质量", "单元测试全绿", r.returncode == 0, tail[0])
    for tool, args in (("ruff", ["check", "src", "tests", "scripts"]), ("mypy", ["src"])):
        r = run([PY, "-m", tool, *args])
        check("质量", f"{tool} 无告警", r.returncode == 0,
              (r.stdout or r.stderr).strip().splitlines()[-1:] or [""])


def group_data() -> None:
    # rb.CONT **不再登记在 symbols.yaml 里**（主连不可下单、不随盘更新）。
    # 它在这里的角色只剩一个：离线验收的样本数据（三个月、跨一次换月）。
    # 这一组直接读 Parquet，不经过注册表，所以摘掉注册项不影响它。
    print("\n[2/5] 数据与日历（离线，用包内样本数据）")
    sys.path.insert(0, str(ROOT / "src"))
    import datetime as dt

    from sigdesk.core.models import CST, Timeframe
    from sigdesk.core.registry import load_registry
    from sigdesk.store.parquet_io import read_range

    reg = load_registry(ROOT / "config")
    cal = reg.calendars["cn_night_23"]

    def at(y: int, m: int, d: int, h: int, mi: int = 0) -> int:
        return int(dt.datetime(y, m, d, h, mi, tzinfo=CST).timestamp())

    cases = [
        ("周三日盘在盘中", cal.in_session(at(2026, 9, 2, 10)), True),
        ("周六不在盘中", cal.in_session(at(2026, 9, 5, 10)), False),
        ("国庆不在盘中", cal.in_session(at(2026, 10, 1, 10)), False),
        ("节前 9/30 不开夜盘", cal.in_session(at(2026, 9, 30, 21, 30)), False),
        ("周五夜盘照开", cal.in_session(at(2026, 9, 4, 21, 30)), True),
    ]
    for label, got, want in cases:
        check("数据", f"交易日历：{label}", got is want, f"得到 {got}")
    check("数据", "2026 节假日表非空", len(cal.holidays) >= 15, f"{len(cal.holidays)} 天")

    if not sample_ready():
        for tf in ("1m", "1d"):
            skip("数据", f"样本数据 {SAMPLE_UID} {tf}", SAMPLE_HOWTO)
        skip("数据", "主连元数据含换月平移量", SAMPLE_HOWTO)
        return

    root = ROOT / "data" / "bars"
    for uid, tf, least in ((SAMPLE_UID, Timeframe.M1, 10000),
                           (SAMPLE_UID, Timeframe.D1, 50)):
        n = len(read_range(root, uid, tf, 0, 2**31))
        check("数据", f"样本数据 {uid} {tf.value}", n >= least, f"{n} 根")

    meta = ROOT / "data" / "bars" / "_continuous" / f"{SAMPLE_UID}.json"
    if meta.exists():
        m = json.loads(meta.read_text(encoding="utf-8"))
        check("数据", "主连元数据含换月平移量",
              bool(m.get("rollovers")) and m.get("adjust") == "back_diff",
              json.dumps(m.get("rollovers", []), ensure_ascii=False)[:80])


def group_engine() -> None:
    print("\n[3/5] 规则引擎（跨级别 + 日线 + 双底，离线试算）")
    sys.path.insert(0, str(ROOT / "src"))
    import yaml

    from sigdesk.core.models import Timeframe
    from sigdesk.rules.loader import load_rule
    from sigdesk.rules.trial import run_trial
    from sigdesk.store.parquet_io import read_range

    src = """
id: acceptance
universe: [CN.SHFE.rb.CONT]
timeframes: {daily: 1d, trend: 1h, setup: 5m}
conditions:
  - {on: daily, mode: state, when: "close > ema(close, 20)"}
  - {on: trend, mode: state, when: "abs(close - at('1d', ema(close, 20))) / close < 0.02"}
  - {on: setup, mode: event, when: "double_bottom(5, 0.002) or cross_up(macd_dif(), macd_dea())"}
emit: {direction: long, ttl: 48 bars, dedup_key: "{symbol}:{rule}:{trend_bar_close_ts}"}
"""
    rule = load_rule(yaml.safe_load(src))
    need = {t.value for t in rule.required_timeframes}
    check("引擎", "跨级别 at() 与日线一起编译", need == {"1d", "1h", "5m"}, str(sorted(need)))

    if not sample_ready():
        for name in ("试算跑出信号", "同一批输入结果可复现", "各级别条件区间有数据",
                     "日线均线预热期是「未知」不是「不成立」"):
            skip("引擎", name, SAMPLE_HOWTO)
        return

    bars = read_range(ROOT / "data" / "bars", SAMPLE_UID, Timeframe.M1, 0, 2**31)
    a = run_trial(rule, {SAMPLE_UID: bars})
    b = run_trial(rule, {SAMPLE_UID: bars})
    check("引擎", "试算跑出信号", len(a.signals) > 0, f"{len(a.signals)} 条")
    check("引擎", "同一批输入结果可复现",
          [s.as_dict() for s in a.signals] == [s.as_dict() for s in b.signals])
    check("引擎", "各级别条件区间有数据", bool(a.condition_bands.get("CN.SHFE.rb.CONT")))
    check("引擎", "日线均线预热期是「未知」不是「不成立」",
          a.condition_counts["CN.SHFE.rb.CONT"]["trend"]["unknown"] > 0)


def _get(url: str, timeout: float = 30.0) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def _send(method: str, url: str, body: object = None) -> tuple[int, object]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(  # noqa: S310
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def _serve(port: int, rules_dir: pathlib.Path, *, allow_edit: bool):
    cmd = [PY, str(ROOT / "scripts" / "serve.py"), "--port", str(port),
           "--rules-dir", str(rules_dir)]
    if allow_edit:
        cmd.append("--allow-edit")
    proc = subprocess.Popen(  # noqa: S603
        cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    for _ in range(60):
        time.sleep(0.5)
        try:
            if _get(f"http://127.0.0.1:{port}/api/meta", timeout=2)[0] == 200:
                return proc
        except Exception:  # noqa: BLE001, S110
            pass
    proc.kill()
    raise RuntimeError("面板起不来：\n" + (proc.stdout.read() if proc.stdout else ""))


def group_api(tmp: pathlib.Path) -> None:
    print("\n[4/5] 面板 API 与规则读写")
    rules = tmp / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    for f in (ROOT / "config" / "rules").glob("*.yaml"):
        shutil.copy(f, rules / f.name)

    # --- 先验安全门：不加 --allow-edit 时写端点必须 403 ---
    port = free_port()
    proc = _serve(port, rules, allow_edit=False)
    base = f"http://127.0.0.1:{port}"
    try:
        for method, path in (("POST", "/api/rules"), ("PUT", "/api/rules/x"),
                             ("DELETE", "/api/rules/x"), ("POST", "/api/rules/trial")):
            code, _ = _send(method, base + path, {"source": "id: x\n"})
            check("安全", f"{method} {path} 默认拒绝", code == 403, f"得到 {code}")
        check("安全", "读端点仍开放", _get(base + "/api/rules")[0] == 200)
    finally:
        proc.kill()

    # --- 再验功能：加了 --allow-edit ---
    port = free_port()
    proc = _serve(port, rules, allow_edit=True)
    base = f"http://127.0.0.1:{port}"
    try:
        code, meta = _get(base + "/api/meta")
        assert isinstance(meta, dict)
        check("API", "/api/meta", code == 200 and bool(meta["symbols"]))
        check("API", "周期含日线 1d", "1d" in meta["timeframes"])
        # 主连**不再登记**（不可下单、不随盘更新，登记只会让下拉框多一个停更的选项）。
        # 但字段要在 —— 万一有人手工加回一条，前端得能标出来。
        # 「meta 会标 is_continuous」这条行为由单测覆盖（自带夹具，不依赖出厂配置）。
        check("API", "注册表里没有主连",
              not any(s["is_continuous"] for s in meta["symbols"]))
        check("API", "每个标的都带 is_continuous / watched / last_day 三个字段",
              all({"is_continuous", "watched", "last_day"} <= set(s) for s in meta["symbols"]))

        for path in ("/api/health", "/api/signals", "/api/stats", "/api/trade"):
            check("API", path, _get(base + path)[0] == 200)

        if sample_ready():
            for tf, least in (("1m", 10000), ("1h", 300), ("1d", 50)):
                code, body = _get(f"{base}/api/bars?symbol={SAMPLE_UID}&timeframe={tf}&limit=5")
                assert isinstance(body, dict)
                check("API", f"/api/bars {tf}", code == 200 and body["total"] >= least,
                      f"{body.get('total')} 根")
            check("API", "/api/markers 日线不再除零",
                  _get(base + f"/api/markers?symbol={SAMPLE_UID}&timeframe=1d")[0] == 200)
        else:
            for tf in ("1m", "1h", "1d"):
                skip("API", f"/api/bars {tf}", SAMPLE_HOWTO)
            skip("API", "/api/markers 日线不再除零", SAMPLE_HOWTO)

        # --- 规则 CRUD 全流程 ---
        good = (f"id: acceptance-tmp\nuniverse: [{SAMPLE_UID}]\n"
                "conditions:\n  - on: 5m\n    mode: state\n    when: close > 0\n"
                'emit: {direction: long, dedup_key: "{symbol}:{rule}:{bar_close_ts}"}\n')
        code, body = _send("POST", base + "/api/rules/validate", {"source": good})
        assert isinstance(body, dict)
        check("规则", "语法校验通过", code == 200 and body["ok"] is True)

        bad = good.replace("close > 0", "close.__class__")
        code, body = _send("POST", base + "/api/rules/validate", {"source": bad})
        assert isinstance(body, dict)
        check("规则", "沙箱逃逸被拒（200 + ok:false，不是 500）",
              code == 200 and body["ok"] is False)

        code, body = _send("POST", base + "/api/rules/validate",
                           {"source": good.replace("close > 0", "ema(close, 20, '1h')")})
        assert isinstance(body, dict)
        check("规则", "参数个数错误在编译期被拒",
              code == 200 and body["ok"] is False and "参数不对" in str(body.get("error")))

        check("规则", "新建", _send("POST", base + "/api/rules", {"source": good})[0] == 201)
        check("规则", "新建重复被拒",
              _send("POST", base + "/api/rules", {"source": good})[0] == 400)
        code, body = _get(base + "/api/rules/acceptance-tmp/source")
        assert isinstance(body, dict)
        check("规则", "读回源码", code == 200 and "close > 0" in body["source"])
        check("规则", "更新",
              _send("PUT", base + "/api/rules/acceptance-tmp",
                    {"source": good.replace("close > 0", "close > 1")})[0] == 200)

        # 试算的 universe 就是上面那条临时规则里的 SAMPLE_UID，没数据就没得试算
        if sample_ready():
            code, body = _send("POST", base + "/api/rules/trial",
                               {"source": good, "horizon_bars": 20})
            assert isinstance(body, dict)
            check("规则", "历史试算", code == 200 and body["bars_scanned"] > 0,
                  f"{body.get('bars_scanned')} 根")
            check("规则", "试算含各级别条件区间", bool(body.get("condition_bands")))
        else:
            skip("规则", "历史试算", SAMPLE_HOWTO)
            skip("规则", "试算含各级别条件区间", SAMPLE_HOWTO)

        code, body = _send("DELETE", base + "/api/rules/acceptance-tmp")
        assert isinstance(body, dict)
        check("规则", "删除后归档进 _trash（不是真删）",
              code == 200 and "_trash" in str(body.get("archived_to")))
        check("规则", "回收站不会被规则加载器扫到",
              not (rules / "acceptance-tmp.yaml").exists()
              and any((rules / "_trash").glob("acceptance-tmp*.yaml")))
        check("规则", "删除不存在的返回 404",
              _send("DELETE", base + "/api/rules/nope")[0] == 404)
    finally:
        proc.kill()


def _playwright_ready() -> str:
    """截图要 playwright 的 node 模块（浏览器用系统 Chrome/Edge，不用 playwright install）。
    返回空串表示就绪，否则返回缺什么。"""
    probe = (
        "const {execSync}=require('child_process');const fs=require('fs');"
        "const path=require('path');let r='';"
        "try{r=execSync('npm root -g',{encoding:'utf8'}).trim()}catch(e){}"
        "const hit=['playwright','@playwright/mcp/node_modules/playwright']"
        ".some(x=>r&&fs.existsSync(path.join(r,x)));"
        "process.stdout.write(hit?'ok':'missing')"
    )
    r = run(["node", "-e", probe])
    return "" if r.stdout.strip() == "ok" else "全局 npm 里没有 playwright 模块"


def group_visual(tmp: pathlib.Path) -> None:
    print("\n[5/5] 面板视觉（需要 node + Chrome/Edge）")
    if not shutil.which("node"):
        for name in ("桩化冒烟：面板不白屏", "无头截图跑通", "控制台无错误 + 无元素遮挡"):
            skip("视觉", name, "没装 Node.js")
        return
    r = run(["node", str(ROOT / "tests" / "panel_smoke.mjs")])
    ok = r.returncode == 0 and '"boot_failed":false' in r.stdout.replace(" ", "")
    check("视觉", "桩化冒烟：面板不白屏", ok, (r.stderr or r.stdout)[-200:])

    missing = _playwright_ready()
    if missing:
        for name in ("无头截图跑通", "控制台无错误 + 无元素遮挡"):
            skip("视觉", name, f"{missing}；装一次即可：npm i -g playwright"
                               "（浏览器用系统 Chrome/Edge，**不用**跑 playwright install）")
        return

    port = free_port()
    rules = tmp / "rules"
    proc = _serve(port, rules, allow_edit=True)
    try:
        out = tmp / "shots"
        r = run(["node", str(ROOT / "scripts" / "shoot_panel.mjs"),
                 f"http://127.0.0.1:{port}", str(out)])
        shots = sorted(p.name for p in out.glob("*.png")) if out.exists() else []
        check("视觉", "无头截图跑通", len(shots) >= 7, f"{len(shots)} 张: {shots[:3]}")
        check("视觉", "控制台无错误 + 无元素遮挡", r.returncode == 0,
              (r.stdout or "").split("❌")[-1][:300])
        if shots:
            print(f"     截图在 {out} —— 人工看一眼有没有明显难看的地方")
    finally:
        proc.kill()


class _Tee:
    """同时打屏与落盘。放在 Python 里而不是批处理的管道里 ——
    cmd 的管道会让 %ERRORLEVEL% 取到管道**最后一个**命令的返回值，
    验收脚本的退出码就丢了，非交互环境下正好是最需要它的时候。"""

    def __init__(self, stream: object, path: pathlib.Path) -> None:
        self._s = stream
        self._f = path.open("w", encoding="utf-8")

    def write(self, text: str) -> int:
        self._f.write(text)
        return int(self._s.write(text))  # type: ignore[attr-defined]

    def flush(self) -> None:
        self._f.flush()
        self._s.flush()  # type: ignore[attr-defined]

    def close(self) -> None:
        self._f.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="signal-desk 离线验收")
    ap.add_argument("--only", choices=["quality", "data", "engine", "api", "visual"])
    ap.add_argument("--no-visual", action="store_true")
    ap.add_argument("--log", type=pathlib.Path, default=ROOT / "acceptance-log.txt",
                    help="完整输出另存一份；非交互环境下这是唯一证据")
    args = ap.parse_args()

    tee = _Tee(sys.stdout, args.log)
    sys.stdout = tee  # type: ignore[assignment]

    print(f"signal-desk 离线验收\n  Python {sys.version.split()[0]}  平台 {sys.platform}"
          f"\n  项目 {ROOT}")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sigdesk-accept-"))
    try:
        groups = {
            "quality": group_quality, "data": group_data, "engine": group_engine,
            "api": lambda: group_api(tmp), "visual": lambda: group_visual(tmp),
        }
        todo = [args.only] if args.only else list(groups)
        if args.no_visual and "visual" in todo:
            todo.remove("visual")
        for key in todo:
            try:
                groups[key]()
            except Exception as e:  # noqa: BLE001
                check(key, f"{key} 组整体", False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [r for r in results if r[2] == "fail"]
    skipped = [r for r in results if r[2] == "skip"]
    checked = len(results) - len(skipped)
    print(f"\n{'=' * 60}\n{checked - len(bad)}/{checked} 项通过"
          + (f"，{len(skipped)} 项因环境缺失跳过" if skipped else ""))
    if bad:
        print("\n失败项（这些是真问题，请如实报告，不要改代码去凑通过）：")
        for group, name, _, detail in bad:
            print(f"  ❌ [{group}] {name}\n       {detail}")
    if skipped:
        print("\n跳过项（环境缺东西，不是代码缺陷）：")
        for group, name, _, reason in skipped:
            print(f"  ⏭️  [{group}] {name}\n       {reason}")
    print("\n下面这些**自动化验不了**，需要人看（见 TESTING.md「只能人工确认的」）：")
    print("  · 信号点是不是你想要的进场点")
    print("  · 推送真投递（要你自己的 TG/Bark 凭据）")
    print("  · 期货实时轮询跑满一个交易日（要行情 API 凭据 + 时间）")
    print(f"\n完整输出已存到 {args.log}")
    sys.stdout = tee._s  # type: ignore[assignment, attr-defined]  # noqa: SLF001
    tee.close()
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
