#!/usr/bin/env python
"""抓取 Quote API 证书指纹，写进用户级 `.env`。

自签名证书无法靠 CA 链验证，只能固定指纹。**请在可信网络下执行**，
之后每次连接都比对该指纹（ADR-0002）。

    python scripts/pin_tls.py            # 只打印
    python scripts/pin_tls.py --write    # 直接写进 ~/.signal-desk/.env

**证书轮换后要重抓一次**：换证之后指纹对不上，客户端会拒绝连接
（这是设计如此 —— 拒绝比蒙着眼睛连上安全）。症状是期货连不上而加密正常。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sigdesk.core.env import USER_ENV, load_env, parse_env  # noqa: E402
from sigdesk.feed.quote_api import fetch_tls_fingerprint  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description="抓 Quote API 证书指纹")
    ap.add_argument("--write", action="store_true",
                    help=f"直接写进 {USER_ENV}，不用手抄")
    args = ap.parse_args()

    # **自己读 .env**。原来只看 os.environ，于是配好用户级 .env 之后跑它
    # 还是会报"请先 set -a; . ./.env"——那是一句 bash 语法，Windows 上照做也没用。
    load_env(ROOT)
    base = os.environ.get("QUOTE_API_BASE")
    if not base:
        print("没有 QUOTE_API_BASE。先跑：python scripts/setup_env.py", file=sys.stderr)
        return 1
    u = urlparse(base)
    fp = fetch_tls_fingerprint(u.hostname or "", u.port or 443)
    print(f"{u.hostname}:{u.port} 证书 sha256 指纹:\n  {fp}\n")

    if not args.write:
        print("写进用户级 .env：  python scripts/pin_tls.py --write")
        print(f"或手动加到 {USER_ENV}：\n  QUOTE_API_TLS_FINGERPRINT={fp}")
        return 0

    from setup_env import write_env

    values = parse_env(USER_ENV.read_text(encoding="utf-8")) if USER_ENV.is_file() else {}
    old = values.get("QUOTE_API_TLS_FINGERPRINT")
    values["QUOTE_API_TLS_FINGERPRINT"] = fp
    write_env(values)
    if old and old != fp:
        print("⚠️  指纹**变了**（证书轮换，或有人在中间）。")
        print("   确认是你自己换的证再用，别在不可信的网络上跑这条。")
    print(f"已写入 {USER_ENV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
