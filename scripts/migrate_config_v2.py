#!/usr/bin/env python3
"""BD-T02: 配置迁移工具 V2 — 将旧配置映射到新 schema，仅转换非敏感字段。"""

import sys, os, yaml


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/migrate_config_v2.py --check <config.yaml>", file=sys.stderr)
        return 1
    action = sys.argv[1]
    if action == "--check":
        path = sys.argv[2] if len(sys.argv) > 2 else "config/env.template.yaml"
        if not os.path.isfile(path):
            print(f"ERROR: {path} not found")
            return 1
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        # Validate no plaintext keys
        ex = cfg.get("exchange", {}).get("binance_usdm", {})
        if "api_key" in ex and ex["api_key"] and len(str(ex["api_key"])) > 10:
            print(f"WARNING: plaintext api_key detected in {path} — migrate to api_key_ref")
        if "api_secret" in ex and ex["api_secret"] and len(str(ex["api_secret"])) > 10:
            print(f"WARNING: plaintext api_secret detected in {path} — migrate to api_secret_ref")
        print(f"Config check complete: {path}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
