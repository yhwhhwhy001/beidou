#!/bin/bash
# 周报的周任务（执行手册 §3.9 D-PR03；操作者 2026-09-28 裁定 Q5 = 是）。
#
# **为什么它要是一个 job。** `beidou report weekly` 从来没有调用者：deploy/ 里没有它的 plist，所以
# 2026-09-16 之后一份周报也没有产出。现在它还多了一件差事：周报的「Plan budget gap」一节是 M-PR01 的
# 记录器——七包行数、非 alpha 合计、alpha 树占比、近 7 天非 alpha 增长。这个缺口原本由
# `test_the_plans_budget_is_recorded_as_breached_rather_than_quietly_redefined` 每天断言「已越界」，
# 却推动不了任何决定；操作者裁定增长率被接受之后，那条测试只钉 PLAN_BUDGET 的字面量，读数挪到这里，
# 只印不告警。没有闹钟的记录器和没有记录器是一回事——这正是 run_check.sh 里那句
# 「a file in deploy/ is not a job」。
#
# **它不发告警，也不碰交易所。** `report weekly`（beidou_cli/live_cmd.py 的 `report_weekly`）不构造
# WebhookAlerts，也不读 profile 的 alerts 块；它读实盘状态文件、ledger 与 git，只写
# `<paths.reports_dir>/weekly/<日期>.md|json`（shipped profile 下是 reports/weekly/，已在 .gitignore 里）。
# 所以这里没有 run_check.sh 那个 notify：失败只进本任务的日志，退出码照实交给 launchd。
#
# **`--date` 传昨天（UTC），不用命令的默认值。** 周报的窗口是「`--date` 那天结束之前的 7 天」，默认
# `--date` 是今天（UTC）。本机时区 +08，周日 03:00 是周六 19:00Z：用默认值，窗口最后 5 小时还没发生，
# 而下一份周报从周日 00:00Z 起算，于是每周有 5 根 bar 不进任何一份周报。传昨天，每份周报都是完整的
# 7 个 UTC 日（周六到周五），一周接一周首尾相接。
#
# 凭据与环境的读法与 run_check.sh 同形：env.sh 存在就只读它，否则读 ~/.zshrc 里的 `export BEIDOU_*` 行。
#
# 安装（操作者动作）：
#   cp deploy/com.beidou.weekly.plist ~/Library/LaunchAgents/
#   launchctl load -w ~/Library/LaunchAgents/com.beidou.weekly.plist
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUPPORT="$HOME/Library/Application Support/beidou"
if [ -f "$SUPPORT/env.sh" ]; then
  # shellcheck disable=SC1091
  source "$SUPPORT/env.sh"
elif [ -f "$HOME/.zshrc" ]; then
  eval "$(grep -E '^export BEIDOU_[A-Z0-9_]+=' "$HOME/.zshrc" || true)"
fi
cd "$REPO" || exit 78
stamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

WEEK_ENDING="$(date -u -v-1d '+%Y-%m-%d')"
if output="$("$REPO/.venv/bin/beidou" report weekly --date "$WEEK_ENDING" 2>&1)"; then
  echo "[$(stamp)] ok   weekly (week ending $WEEK_ENDING)"
  echo "$output" | sed "s/^/           /"
else
  echo "[$(stamp)] FAIL weekly (week ending $WEEK_ENDING)"
  echo "$output" | tail -n 20 | sed "s/^/           /"
  exit 1
fi
