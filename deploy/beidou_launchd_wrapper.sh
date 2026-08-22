#!/bin/bash
# Governed launchd wrapper (M00-F07, P0-02).
#
# 重启语义（与 supervisor 实际返回码对齐，M00-F07-R2 对抗审查修正）:
#   - supervisor return 5 = LOCKED（需人工处置的终态）→ wrapper 映射为 0,
#     launchd 不重启 —— 阻止无限重启循环;
#   - supervisor return 4 = 启动失败（凭据/预检/深度门禁）, return 6 =
#     引擎运行失败, 以及其他非零退出（崩溃）→ 原码透传, launchd
#     KeepAlive={SuccessfulExit:false} 自动重启（瞬时故障如 PG 短暂
#     不可用需要持续重试直至恢复 —— autopilot 既有运维语义）。
#
# 凭据: 从 ~/beidou/.env 注入(如存在); plist 不内嵌任何密钥、不做 shell eval。
#
# BD-FIX (kickstart 孤儿进程): 旧实现 "$@" 前台运行, launchd kickstart -k
# 只向 wrapper(bash) 发 TERM, bash 等待前台子进程期间不转发信号 ——
# 实测 2026-08-19 kickstart 后旧 python 变孤儿继续占用 9090 端口,
# 新进程绑定失败, 必须人工 kill -9。改为后台运行 + trap 转发
# TERM/INT, 并设 25s 兜底强杀(先于 launchd ExitTimeOut=30 的 SIGKILL),
# 保证 kickstart 语义可靠: 旧进程必然退出、新进程必然接管端口。

if [ -f "$HOME/beidou/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$HOME/beidou/.env"
  set +a
fi

# G5's database-backed scenarios require an explicit isolated Testnet DSN.
# The local Testnet environment already declares that DSN as DATABASE_URL;
# expose the governed alias when an operator has not supplied a separate one.
# There is no fallback to a network or production database.
if [ -z "${BEIDOU_G5_PG_DSN:-}" ] && [ -n "${DATABASE_URL:-}" ]; then
  export BEIDOU_G5_PG_DSN="$DATABASE_URL"
fi

"$@" &
child=$!

forward_term() {
  kill -TERM "$child" 2>/dev/null || true
  # 兜底: 25s 后仍未退出则强杀,避免优雅关闭卡死在挂起的网络调用上。
  (sleep 25; kill -9 "$child" 2>/dev/null || true) &
}
trap forward_term TERM INT

wait "$child"
rc=$?
if [ "$rc" -gt 128 ]; then
  # wait 被 TERM/INT 中断(返回 128+信号)。此时 trap 已向子进程转发
  # 信号,再 wait 一次收集子进程真实退出码 —— 否则 LOCKED(exit 5)
  # 语义会被 143 吞掉,launchd 误判为非零退出而无限重启 LOCKED 引擎。
  wait "$child"
  rc=$?
fi
trap - TERM INT

case "$rc" in
  5) exit 0 ;; # LOCKED — 需人工处置, 不得自动重启
  *) exit "$rc" ;;
esac
