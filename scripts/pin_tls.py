#!/usr/bin/env python
"""引导脚本：抓取 Quote API 证书指纹并打印，供写入 .env 的 QUOTE_API_TLS_FINGERPRINT。

自签名证书无法靠 CA 链验证，只能固定指纹。**请在可信网络下执行一次**，
之后每次连接都比对该指纹（ADR-0002）。
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from sigdesk.feed.quote_api import fetch_tls_fingerprint  # noqa: E402


def main() -> int:
    base = os.environ.get("QUOTE_API_BASE")
    if not base:
        print("请先 `set -a; . ./.env; set +a`", file=sys.stderr)
        return 1
    u = urlparse(base)
    fp = fetch_tls_fingerprint(u.hostname or "", u.port or 443)
    print(f"{u.hostname}:{u.port} 证书 sha256 指纹:\n  {fp}\n")
    print("写入 .env：")
    print(f"  QUOTE_API_TLS_FINGERPRINT={fp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
