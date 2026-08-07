#!/bin/zsh
# 北斗因子挖掘定时任务脚本
# cron: 0 3 * * * /Users/maguannan/beidou/scripts/mining_cron.sh >> /tmp/beidou_mining.log 2>&1

set -e
cd /Users/maguannan/beidou

# 从 ~/.zshrc 提取并加载北斗环境变量
eval "$(grep '^export BEIDOU_' ~/.zshrc 2>/dev/null)" 2>/dev/null || true
export BEIDOU_ENV="${BEIDOU_ENV:-testnet}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 北斗因子挖掘开始"

python -m apps.factor_miner run \
    --policy config/factor_mining_policy.yaml \
    --symbols BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT \
    --interval 1h \
    --limit 500

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 北斗因子挖掘完成"
