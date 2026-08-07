#!/bin/zsh
# 北斗因子挖掘定时任务脚本
# 用法: crontab -e 添加定时调用
# 示例: 0 3 * * * /Users/maguannan/beidou/scripts/mining_cron.sh >> /tmp/beidou_mining.log 2>&1

set -e

cd /Users/maguannan/beidou

# 加载环境变量
source ~/.zshrc

# 记录开始
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始因子挖掘..."

# 运行挖掘（4 个主流品种）
python -m apps.factor_miner run \
    --policy config/factor_mining_policy.yaml \
    --symbols BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT \
    --interval 1h \
    --limit 500

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成"
