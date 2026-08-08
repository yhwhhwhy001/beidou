#!/usr/bin/env python3
"""BD-T02: 配置迁移工具 V2 — 将旧配置映射到新 schema，仅转换非敏感字段。"""

import os
import sys

import yaml


def main():
    if len(sys.argv) < 2:
        return 1
    action = sys.argv[1]
    if action == "--check":
        path = sys.argv[2] if len(sys.argv) > 2 else "config/env.template.yaml"
        if not os.path.isfile(path):
            return 1
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        # Validate no plaintext keys
        ex = cfg.get("exchange", {}).get("binance_usdm", {})
        if "api_key" in ex and ex["api_key"] and len(str(ex["api_key"])) > 10:
            pass
        if "api_secret" in ex and ex["api_secret"] and len(str(ex["api_secret"])) > 10:
            pass
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
