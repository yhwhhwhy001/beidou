#!/bin/bash
# Governed launchd wrapper (M00-F07, P0-02).
#
# 重启语义:
#   - 崩溃/异常退出(非零)由 launchd KeepAlive={SuccessfulExit:false} 自动重启;
#   - 监督器终态退出码 5 (LOCKED) / 6 (FAILED) 表示需人工处置的终态 ——
#     wrapper 将其映射为 0, 阻止 launchd 无限重启循环(旧实装 KeepAlive=true
#     + ThrottleInterval=10 会把 LOCKED 状态变成 10s 一次的崩溃循环)。
#
# 凭据: 从 ~/beidou/.env 注入(如存在); plist 不内嵌任何密钥、不做 shell eval。

if [ -f "$HOME/beidou/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$HOME/beidou/.env"
  set +a
fi

"$@"
rc=$?
case "$rc" in
  5 | 6) exit 0 ;; # LOCKED/FAILED — 需人工处置, 不得自动重启
  *) exit "$rc" ;;
esac
