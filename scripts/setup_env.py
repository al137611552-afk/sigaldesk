#!/usr/bin/env python
"""一次性把凭据写进**用户级** `.env`，以后换新包不用重配。

    python scripts/setup_env.py            # 交互填写
    python scripts/setup_env.py --show     # 只看当前状态（**不显示值**）
    python scripts/setup_env.py --path     # 打印文件位置

为什么写到 `~/.signal-desk/.env` 而不是项目目录：
**分发包里不含 `.env`**（凭据不进包），所以每换一个新包、解压到新目录，
项目内的 `.env` 就没了。用户级目录跟着这台机器走，只配一次。

**这里不做前端界面**：面板没有鉴权，"改规则文件"和"写 API 密钥"是两个风险等级。
密钥只从本机命令行进，不经过 HTTP，也不回显。
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import pathlib
import stat
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sigdesk.core.env import USER_ENV, candidate_paths, load_env, parse_env  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]

# (键, 说明, 是否敏感)。敏感的用 getpass 读，不回显、不进 shell 历史。
FIELDS: list[tuple[str, str, bool]] = [
    ("QUOTE_API_BASE", "期货行情 API 地址，如 https://host:port", False),
    ("QUOTE_API_KEY", "期货行情 API 密钥", True),
    ("QUOTE_API_TLS_FINGERPRINT", "自签名证书指纹（scripts/pin_tls.py 生成，可留空）", False),
    ("TELEGRAM_BOT_TOKEN", "Telegram 推送 token（可留空）", True),
    ("TELEGRAM_CHAT_ID", "Telegram chat id（可留空）", False),
    ("BARK_URL", "Bark 推送地址（可留空）", True),
]


def show() -> int:
    """只报告**有没有配**，绝不打印值 —— 值进了终端就等于进了滚动缓冲区。"""
    load_env(ROOT)
    print("查找顺序（先找到的先用）：")
    for p in candidate_paths(ROOT):
        print(f"  {'✅' if p.is_file() else '  '} {p}")
    print("\n当前生效的配置项：")
    for key, desc, _ in FIELDS:
        value = os.environ.get(key, "")
        mark = "已设置" if value else "未设置"
        print(f"  {'✅' if value else '  '} {key:28s} {mark:6s}  {desc}")
    print("\n（只显示有没有值，不显示值本身）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="配置 signal-desk 的凭据（写入用户级 .env）")
    ap.add_argument("--show", action="store_true", help="只看状态，不修改")
    ap.add_argument("--path", action="store_true", help="打印用户级 .env 的位置")
    args = ap.parse_args()

    if args.path:
        print(USER_ENV)
        return 0
    if args.show:
        return show()

    existing: dict[str, str] = {}
    if USER_ENV.is_file():
        existing = parse_env(USER_ENV.read_text(encoding="utf-8"))
        print(f"已有配置：{USER_ENV}（回车 = 保留原值）\n")
    else:
        print(f"将写入：{USER_ENV}\n")

    out = dict(existing)
    for key, desc, secret in FIELDS:
        has = "已有值" if existing.get(key) else "空"
        prompt = f"{key}\n  {desc}\n  当前：{has} > "
        value = getpass.getpass(prompt) if secret else input(prompt)
        if value.strip():
            out[key] = value.strip()
        print()

    USER_ENV.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}={v}\n" for k, v in out.items() if v)
    USER_ENV.write_text(body, encoding="utf-8")
    # 只有本人可读写。Windows 上这行是空操作，但那边有 ACL 兜着。
    with contextlib.suppress(OSError):
        USER_ENV.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"已写入 {USER_ENV}（{len([v for v in out.values() if v])} 项）")
    print("以后任何脚本、任何目录、任何新版本的包，都会自动读到它。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
