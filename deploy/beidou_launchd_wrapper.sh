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

if [ -f "$HOME/beidou/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$HOME/beidou/.env"
  set +a
fi

"$@"
rc=$?
case "$rc" in
  5) exit 0 ;; # LOCKED — 需人工处置, 不得自动重启
  *) exit "$rc" ;;
esac
