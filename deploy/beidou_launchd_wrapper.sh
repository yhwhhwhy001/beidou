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
#
# BD-FIX-2 (kickstart 孤儿复发, 实测 2026-08-25 01:52): 上述路径仍有
# 失效窗口 —— wrapper 先于兜底任务死亡时(trap 未执行/launchd 直接
# SIGKILL wrapper), launchd 会清理 wrapper 进程组内的残留后台任务,
# 25s 兜底随之消失, 忽略 TERM 的子进程变孤儿占用端口, 新实例
# preflight "北斗实例已运行" 循环 exit 3。双保险:
#   1) 独立孤儿守护(python os.setsid 脱离 job 进程组): wrapper 死而
#      child 未死 → kill -9 child;
#   2) 启动前端口清理: 目标端口仍被 beidou 进程占用时校验命令行后
#      强杀(可用 BEIDOU_NO_ORPHAN_CLEANUP=1 禁用)。
# LOCKED exit-5 → 0 的映射语义不变。

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

# The G5 producer may use only listen-key session control for its own user
# stream.  Keep its engine terminal-write hold hard even when the shared .env
# carries the normal Testnet unknown-only setting.
if [ "${BEIDOU_G5_PRODUCER:-}" = "1" ]; then
  export BEIDOU_TERMINAL_WRITE_HOLD="hard"
fi

# BD-FIX-2 (kickstart 孤儿兜底, 2026-08-25 实测): 启动前若目标端口仍被
# 上一代 beidou 孤儿实例占用, 校验命令行后强杀 —— 保证新实例第一次
# preflight 即通过。不依赖旧 wrapper 的信号转发(实测该路径可失效:
# wrapper 先于兜底任务死亡时, launchd 会清理其残留后台任务)。
# 语义: 占用本实例目标端口的 beidou 进程必然阻碍 preflight("北斗实例
# 已运行"), 清理是启动的必要前提; 用 BEIDOU_NO_ORPHAN_CLEANUP=1 可禁用。
# BD-FIX-2b (2026-08-25 认证误伤实测): 只杀"父进程已死"(PPID=1)的
# 真孤儿 —— 并行实例(G5 producer 等)的引擎父 wrapper 存活, 属受管
# 进程, 必须放行; 否则 autopilot 重启循环会每 30s 误杀一次 producer,
# G5 认证 readiness 恒超时。
if [ "${BEIDOU_NO_ORPHAN_CLEANUP:-0}" != "1" ]; then
  _port=""
  _args=("$@")
  for ((_i = 0; _i < ${#_args[@]}; _i++)); do
    if [ "${_args[$_i]}" = "--port" ]; then
      _port="${_args[$((_i + 1))]:-9090}"
      break
    fi
  done
  _port="${_port:-9090}"
  for _pid in $(lsof -ti tcp:"$_port" -sTCP:LISTEN 2>/dev/null || true); do
    _cmd=$(ps -p "$_pid" -o command= 2>/dev/null || true)
    case "$_cmd" in
      *beidou*)
        _ppid=$(ps -p "$_pid" -o ppid= 2>/dev/null | tr -d ' ')
        if [ -n "$_ppid" ] && [ "$_ppid" != "1" ] && kill -0 "$_ppid" 2>/dev/null; then
          echo "BD-FIX-2: port $_port held by managed beidou pid=$_pid (ppid=$_ppid) — not an orphan, leaving" >&2
          continue
        fi
        echo "BD-FIX-2: killing orphan beidou pid=$_pid on port $_port" >&2
        kill -9 "$_pid" 2>/dev/null || true
        sleep 1
        ;;
    esac
  done
fi

"$@" &
child=$!

# BD-FIX-2 (独立孤儿守护): 用 python os.setsid 脱离 launchd job 的
# 进程组/session —— 实测 launchd 会在 wrapper 退出后清理其进程组内的
# 残留后台任务(trap 里的 25s 兜底因此失效), 脱离后的守护不受影响。
# 守护契约: child 先死 → 守护自然退出(正常路径); wrapper 先死而 child
# 未死 → kill -9 child(防孤儿), 随后退出。绝不改变 wrapper 的退出码
# 映射(LOCKED exit 5 → 0 语义不受影响)。
if command -v python3 >/dev/null 2>&1; then
  # 注意: 必须用 $$(bash 子 shell 中保持为主 shell 即 wrapper 的 PID),
  # 实测 $PPID 在后台子 shell 中展开为 wrapper 的父进程 PID —— 守护会
  # 误判 wrapper 存活而永不触发强杀。
  (
    python3 - "$child" "$$" >/dev/null 2>&1 <<'PYEOF'
import os, signal, subprocess, sys, time

child_pid = int(sys.argv[1])
wrapper_pid = int(sys.argv[2])
try:
    os.setsid()
except OSError:
    pass  # 已脱离或无法脱离时继续尽力而为

def alive(pid):
    """kill(pid, 0) 对僵尸进程仍成功; 用 ps stat 排除 Z 态。"""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        out = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return not out.startswith("Z")
    except Exception:
        return True  # 无法判定时按存活处理(宁可多杀一次)

for _ in range(1200):  # 最长约 20 分钟, 超过即放弃(极端场景交给端口清理)
    if not alive(child_pid):
        break
    if not alive(wrapper_pid):
        try:
            os.kill(child_pid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(2)
        if not alive(child_pid):
            break
        # child 未死则继续循环再杀(信号竞态下的重试)
    time.sleep(1)
PYEOF
  ) &
fi

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
