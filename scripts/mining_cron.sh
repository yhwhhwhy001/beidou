#!/bin/zsh
# 北斗因子挖掘定时任务脚本
# cron: 0 3 * * * /Users/maguannan/beidou/scripts/mining_cron.sh >> /tmp/beidou_mining.log 2>&1

set -e
cd /Users/maguannan/beidou

# 研究定时任务固定为零写环境；品种必须由调度配置显式提供。
export BEIDOU_ENV="research"
: "${BEIDOU_MINING_SYMBOLS:?BEIDOU_MINING_SYMBOLS must be explicitly configured}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 北斗因子挖掘开始"

python -m apps.factor_miner run \
    --policy config/factor_mining_policy.yaml \
    --symbols "$BEIDOU_MINING_SYMBOLS" \
    --interval 1h \
    --limit 500

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 北斗因子挖掘完成"
