#!/usr/bin/env python
"""独立只读面板：只连 SQLite 与 Parquet，不碰行情。

用途是"回头看"：历史信号、K 线标注、质量统计。它**不接实时引擎**，
所以健康页会如实显示"未接入实时引擎"，而不是假装一切正常。

想要实时信号流与运行健康，用 `scripts/watch.py --web`（引擎与 Web 同进程，ARCHITECTURE §7）。

用法：
    .venv/bin/python scripts/serve.py                       # http://127.0.0.1:8000
    .venv/bin/python scripts/serve.py --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import uvicorn  # noqa: E402

from sigdesk.core.env import load_env  # noqa: E402
from sigdesk.core.registry import load_registry  # noqa: E402
from sigdesk.rules.loader import load_rules  # noqa: E402
from sigdesk.store.runtime_store import RuntimeStore  # noqa: E402
from sigdesk.web.api import ServiceState, create_app  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 脚本自己读 .env：`set -a; . ./.env` 是 bash 专有写法，Windows 上没有对应物。
# 查找顺序 SIGDESK_ENV -> ./.env -> ~/.signal-desk/.env（换新包也不用重配）。
ENV = load_env(ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description="signal-desk 只读面板")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--state-db", type=pathlib.Path, default=ROOT / "data" / "runtime.sqlite3")
    ap.add_argument("--data-root", type=pathlib.Path, default=ROOT / "data" / "bars")
    ap.add_argument("--rules-dir", type=pathlib.Path, default=ROOT / "config" / "rules")
    ap.add_argument(
        "--allow-edit", action="store_true",
        help="开启规则增删改与历史试算（FR-5.3）。面板没有鉴权，所以默认关闭，"
             "且只允许绑在回环地址上",
    )
    args = ap.parse_args()

    if args.allow_edit and args.host not in ("127.0.0.1", "::1", "localhost"):
        # 写端点没有鉴权。绑到 0.0.0.0 再开写，等于把规则文件的读写权交给整个网段。
        print(
            f"拒绝：--allow-edit 只能绑回环地址，当前 --host {args.host}。\n"
            f"要远程编辑请自己开 SSH 隧道：ssh -L 8000:127.0.0.1:8000 <host>"
        )
        return 2
    state = ServiceState(
        runtime=RuntimeStore(args.state_db),
        data_root=args.data_root,
        registry=load_registry(ROOT / "config"),
        rules=load_rules(args.rules_dir),
        live=False,
        rules_dir=args.rules_dir,
        edit_enabled=bool(args.allow_edit),
    )
    print(f"只读面板: http://{args.host}:{args.port}")
    print(f"  规则   {args.rules_dir}" + ("（可编辑 + 可试算）" if args.allow_edit else "（只读）"))
    print(f"  运行态 {args.state_db}（{state.runtime.count_signals()} 条历史信号）")
    print(f"  行情   {args.data_root}")
    uvicorn.run(create_app(state), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
