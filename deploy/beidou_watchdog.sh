#!/bin/bash
# 北斗引擎看守进程 (watchdog)
#
# 职责: 检测引擎静默停机, 按熔断策略有限次自动重启, 并发出本机告警。
#
# 背景与设计依据:
#   supervisor 判定 LOCKED 后以 exit 5 退出, wrapper 将其映射为 exit 0,
#   launchd 的 KeepAlive={SuccessfulExit:false} 因此不再拉起 —— 这是为阻止
#   "拉起→立刻 LOCKED→再拉起" 循环而做的有意设计 (M00-F07-R2), 不应推翻。
#   但 2026-08-24 的实测数据显示: 19 次 LOCKED 中, 最近三次 (#17/#18/#19)
#   重启后分别运行了 26152/17547 行日志才再次出问题, 属"重启即可恢复"的
#   内存态问题; 而 8-22 曾出现 11 次连环 LOCKED (间隔仅 247~3210 行), 属
#   真实故障, 无条件自动重启会造成循环。
#   故本脚本承担熔断判断, 不修改 wrapper 的退出码语义:
#     首次停机          → 自动 kickstart
#     重启后存活 ≥30min → 熔断计数清零 (视为已自愈)
#     存活不足即再停机  → 计数+1, 退避翻倍 (2→4→8 分钟)
#     连续 3 次未存活   → 停止自动重启, 仅告警, 转人工
#
# 自动维护模式: 无需任何手工操作。引擎停机时若 git 工作区有未提交变更,
#   预检 preflight.git_worktree 必然拒绝启动, 看守据此自动跳过重启且不计入
#   熔断额度, 提交后自动恢复 —— 开发期间不必记得暂停看守。
# 手动覆盖 (可选): touch <STATE_DIR>/pause 完全停用看守, rm 即恢复。
#   仅在需要连引擎一起停掉做别的事时使用。

set -uo pipefail

SERVICE="gui/501/com.beidou.autopilot"
# 仓库根目录: 本脚本位于 <repo>/deploy/ 下, 上跳一级即仓库根。
# 允许 BEIDOU_REPO 覆盖 (供测试使用)。解析结果必须通过标志文件校验 ——
# 脚本若被复制到别处运行, 相对定位会指向错误目录, 导致 git 检查恒失败、
# 看守静默失效, 必须让这种情况明确暴露而不是悄悄跳过。
REPO="${BEIDOU_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# 运行时状态与日志固定写到仓库之外。
#   - 本脚本随仓库分发 (deploy/), 若状态文件与脚本同目录会持续污染 git 树,
#     进而触发引擎启动预检的 git_worktree 检查, 阻断引擎启动。
#   - 该路径亦不得置于 ~/Documents / ~/Desktop / ~/Downloads —— macOS TCC
#     会使 launchd 后台服务以 exit 126 "Operation not permitted" 静默失败
#     (2026-08-24 实测: 服务能加载、runs 会增长, 但脚本从未真正执行)。
STATE_DIR="$HOME/Library/Application Support/beidou-watchdog"
STATE_FILE="$STATE_DIR/state"
LOG_FILE="$STATE_DIR/watchdog.log"
PAUSE_FILE="$STATE_DIR/pause"

MAX_FAILS=3          # 连续失败上限, 达到即熔断
BASE_BACKOFF=120     # 基础退避 2 分钟
HEALTHY_RESET=1800   # 重启后存活满 30 分钟即认定自愈

mkdir -p "$STATE_DIR"
now=$(date +%s)

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE"; }

# 告警通道 (2026-08-24 实测选定):
#   通知中心 (display notification) 在本机不可达 —— macOS 26.7 下 launchd
#   后台进程没有授权入口, osascript 返回成功但通知从不出现, 属静默失败,
#   曾据此误以为告警已接通。改用:
#     display dialog —— 持久停留直至点击, 人不在场时回来仍能看到
#   已验证在 launchd 后台上下文可用。语音播报 (say) 曾一并使用, 应用户
#   要求移除 —— 弹窗本身已足够且不打扰。
# 必须异步: display dialog 会阻塞至用户点击, 而本脚本由 StartInterval=60
# 周期调度, 阻塞将导致巡检实例重叠。子 shell 在父进程退出后独立存活 (已验证)。
notify() {
  local title="$1" msg="$2"
  osascript -e "display dialog \"${msg//\"/\\\"}\" with title \"${title//\"/\\\"}\" buttons {\"知道了\"} default button 1 with icon caution giving up after 1800" >/dev/null 2>&1 &
}

fail_count=0
last_restart=0
down_since=0
[ -f "$STATE_FILE" ] && . "$STATE_FILE"

save_state() {
  printf 'fail_count=%s\nlast_restart=%s\ndown_since=%s\n' \
    "$fail_count" "$last_restart" "$down_since" >"$STATE_FILE"
}

# --- 0. 维护模式 ---
if [ -f "$PAUSE_FILE" ]; then
  exit 0
fi

# --- 0.5 引擎卡死告警 (A 类永不 LOCKED 的配套) ---
# 引擎每轮扫描 protection_exposure, 存在 ≥30 分钟未建起保护的持仓时
# 写 stuck 标记 (引擎侧已按 mtime 续写, 所以以文件 mtime 判断时效);
# 此处读到即弹窗, 用 stuck_last_alert 标记节流 30 分钟。
# R11: 健康引擎 30s 内会刷新/删除 stuck —— mtime 过期 >1800s 意味着
# 引擎循环已死 (冻结但进程活着, 能过下方存活判定), 旧逻辑直接 rm 等于
# 销毁唯一证据且永不告警。现改为: 引擎主动清除时写 stuck.cleared 墓碑,
# watchdog 据此区分"正常清除"(rm 两文件) 与"疑似冻结"(升级告警 +
# kickstart 一次, 不 rm stuck)。
STUCK_FILE="$STATE_DIR/stuck"
CLEARED_FILE="$STATE_DIR/stuck.cleared"
if [ -f "$STUCK_FILE" ]; then
  stuck_mtime=$(stat -f %m "$STUCK_FILE" 2>/dev/null || echo 0)
  if [ $(( now - stuck_mtime )) -le 1800 ]; then
    # 新鲜标记 (引擎循环仍在续写): 现有节流弹窗逻辑不变
    last_alert_ts=0
    [ -f "$STATE_DIR/stuck_last_alert" ] && last_alert_ts=$(stat -f %m "$STATE_DIR/stuck_last_alert" 2>/dev/null || echo 0)
    if [ $(( now - last_alert_ts )) -ge 1800 ]; then
      notify "北斗引擎保护卡死" "有持仓超过 30 分钟未能建立保护（详见 ${STUCK_FILE}）。引擎已停止开新仓但仍在尝试修复；请检查后决定是否人工处置。"
      touch "$STATE_DIR/stuck_last_alert"
    fi
  elif [ -f "$CLEARED_FILE" ] && [ $(( now - $(stat -f %m "$CLEARED_FILE" 2>/dev/null || echo 0) )) -le 1800 ]; then
    # 引擎留下新鲜清除墓碑 → 卡死已正常消除, 清理两个文件
    rm -f "$STUCK_FILE" "$CLEARED_FILE"
    log "CLEARED 卡死已消除 (stuck.cleared 墓碑在 30 分钟内), 移除 stuck 标记"
  else
    # 标记过期且无新鲜墓碑 → 引擎循环已死 (冻结但进程活着)。仅当进程
    # 存活时升级告警 + kickstart 一次; 进程已死则主重启路径 (含熔断计数)
    # 处理, 此处不得绕过熔断账本重复 kickstart。stuck_last_alert 节流
    # 30 分钟, 不 rm stuck —— 保留唯一证据。
    if pgrep -f "beidou start" >/dev/null 2>&1; then
      last_alert_ts=0
      [ -f "$STATE_DIR/stuck_last_alert" ] && last_alert_ts=$(stat -f %m "$STATE_DIR/stuck_last_alert" 2>/dev/null || echo 0)
      if [ $(( now - last_alert_ts )) -ge 1800 ]; then
        log "FROZEN 卡死标记超 30 分钟未刷新 (引擎循环疑似停止), 发起 kickstart"
        notify "北斗引擎疑似冻结" "卡死标记超过 30 分钟未刷新, 引擎循环可能已停; watchdog 将尝试重启"
        touch "$STATE_DIR/stuck_last_alert"
        if launchctl kickstart "$SERVICE" >>"$LOG_FILE" 2>&1; then
          log "FROZEN kickstart 成功"
        else
          log "ERROR 冻结恢复 kickstart 失败"
        fi
      fi
    fi
  fi
fi

# --- 1. 服务是否仍在 launchd 域内 ---
# 被 bootout (人工下线) 时不干预 —— 那是运维意图, 不是故障。
if ! launchctl print "$SERVICE" >/dev/null 2>&1; then
  log "SKIP 服务不在 launchd 域内 (人工下线), 不干预"
  exit 0
fi

# --- 2. 存活判定 ---
# 进程存在即视为存活: 引擎深度启动自检可达数分钟, 期间 :9090 尚未监听,
# 若以 /status 可达为判据会在启动途中被误杀。
if pgrep -f "beidou start" >/dev/null 2>&1; then
  if [ "$down_since" -ne 0 ]; then
    log "UP 引擎已恢复 (本次停机 $(( (now - down_since) / 60 )) 分钟)"
    down_since=0
    save_state
  fi
  # 存活足够久 → 清零熔断计数
  if [ "$fail_count" -gt 0 ] && [ "$last_restart" -gt 0 ] &&
     [ $((now - last_restart)) -ge "$HEALTHY_RESET" ]; then
    log "RESET 重启后已存活 $(( (now - last_restart) / 60 )) 分钟 → 熔断计数 $fail_count → 0"
    fail_count=0
    save_state
  fi
  exit 0
fi

# --- 3. 确认停机 ---
if [ "$down_since" -eq 0 ]; then
  down_since=$now
  save_state
  log "DOWN 未检测到引擎进程 (fail_count=$fail_count)"
fi

# --- 3.5 源码树可启动性 (自动维护模式) ---
# 引擎预检 preflight.git_worktree 在 testnet(写模式) 下对
# `git status --porcelain` 非空即判 P0 FAIL 拒绝启动 (含未跟踪文件)。
# 此时 kickstart 必然失败, 重启只会空耗熔断额度, 故直接跳过 ——
# 开发者改代码期间无需任何手工操作, 提交后自动恢复看守。
if [ ! -f "$REPO/beidou_launcher/preflight.py" ]; then
  log "ERROR 仓库路径校验失败: $REPO 不是北斗仓库 —— 看守无法判断可启动性, 跳过重启"
  notify "北斗看守配置异常" "仓库路径解析为 ${REPO} —— 非北斗仓库；自动重启已停用，需人工检查 watchdog 安装位置"
  exit 0
fi
if ! dirty=$(git -C "$REPO" status --porcelain 2>/dev/null); then
  log "SKIP 无法读取 git 工作区状态 ($REPO), 保守跳过自动重启"
  exit 0
fi
if [ -n "$dirty" ]; then
  n=$(printf '%s\n' "$dirty" | wc -l | tr -d ' ')
  log "SKIP 工作区有 $n 项未提交变更, 引擎预检会拒绝启动, 跳过自动重启 (不计入熔断)"
  if [ $(( (now - down_since) % 1800 )) -lt 60 ]; then
    notify "北斗引擎停机（源码未提交）" "工作区有 $n 项未提交变更，引擎预检会拒绝启动，已跳过自动重启；提交后自动恢复"
  fi
  exit 0
fi

if [ "$fail_count" -ge "$MAX_FAILS" ]; then
  # 熔断态: 每 30 分钟提醒一次, 避免通知轰炸
  if [ $(( (now - down_since) % 1800 )) -lt 60 ]; then
    log "HALT 熔断中 (连续 $fail_count 次重启后仍无法存活), 已停机 $(( (now - down_since) / 60 )) 分钟"
    notify "北斗引擎停机（熔断）" "连续 $fail_count 次自动重启均未稳定运行，已停止自动重启，需人工处置"
  fi
  exit 0
fi

# --- 4. 退避 ---
# 首次停机 (fail_count=0, last_restart=0) 立即重启 —— LOCKED 是终态,
# 引擎不会自行恢复, 等待无收益。退避只作用于"重启后没撑住"的后续尝试:
# 第 2 次前等 2 分钟, 第 3 次前等 4 分钟。
backoff=$(( BASE_BACKOFF * (1 << (fail_count > 0 ? fail_count - 1 : 0)) ))
if [ "$last_restart" -gt 0 ] && [ $((now - last_restart)) -lt "$backoff" ]; then
  log "WAIT 退避中 ($((now - last_restart))s / ${backoff}s)"
  exit 0
fi

# --- 5. 自动重启 ---
fail_count=$((fail_count + 1))
last_restart=$now
save_state
log "RESTART kickstart $SERVICE (第 $fail_count/$MAX_FAILS 次)"
if launchctl kickstart "$SERVICE" >>"$LOG_FILE" 2>&1; then
  notify "北斗引擎已自动重启" "检测到停机，已发起第 $fail_count/$MAX_FAILS 次自动重启"
else
  log "ERROR kickstart 返回非零"
  notify "北斗引擎重启失败" "kickstart 执行失败，需人工检查"
fi
