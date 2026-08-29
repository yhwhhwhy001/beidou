#!/bin/bash
# 北斗 Testnet 唯一验证入口 (apps.testnet_verify) 的 launchd wrapper。
#
# 设计约束:
#   - 密钥只存在于仓库 .env (已 gitignore); 本脚本与 plist 均不携带秘密
#   - 有界写入上限 (单笔 $25 / 杠杆 3x / 账户总暴露 $25) 由 CLI 显式传入
#   - close-after-verify: 每笔验证单成交后 reduce-only 平仓, 不留过夜仓位
set -euo pipefail
cd /Users/maguannan/beidou
set -a
# shellcheck disable=SC1091
source .env
set +a
exec .venv/bin/python -m apps.testnet_verify \
    --confirm-testnet \
    --close-after-verify \
    --max-notional 25 \
    --max-leverage 3
