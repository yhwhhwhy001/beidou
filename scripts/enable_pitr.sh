#!/bin/bash
# M16-F01: 宿主机 Homebrew PostgreSQL 开启 PITR(WAL 归档)。
#
# 现状: archive_mode=off,wal_level=replica —— 无 WAL 归档,PITR 不可用。
# 本脚本修改 postgresql.conf 并创建归档目录,不重启服务(重启步骤与
# 验证见末尾说明 —— PG 重启会打断运行中的引擎,必须在受控重启窗口
# 执行,部署阶段 M22 负责)。
set -euo pipefail

PGDATA="${BEIDOU_PGDATA:-/opt/homebrew/var/postgresql@16}"
CONF="${PGDATA}/postgresql.conf"
ARCHIVE_DIR="${PGDATA}/wal_archive"

if [ ! -f "$CONF" ]; then
    echo "ERROR: postgresql.conf not found at $CONF" >&2
    exit 1
fi

mkdir -p "$ARCHIVE_DIR"
chmod 700 "$ARCHIVE_DIR"

# BSD sed 的原地编辑要求 -i 后跟一个独立的后缀参数(空串表示不备份),
# GNU sed 则把后缀粘在 -i 上,拿到 '' 会当成脚本、进而把 -E 的表达式
# 当文件名读 —— 两边形式不通用,按实现选。--version 只有 GNU 认。
if sed --version >/dev/null 2>&1; then
    sed_inplace() { sed -i "$@"; }
else
    sed_inplace() { sed -i '' "$@"; }
fi

set_conf() {
    local key="$1" value="$2"
    # M16-R2: macOS BSD sed -E 不支持 \s(实测静默 no-op 仍打印 OK)——
    # 用 [[:space:]] 字符类;写后 grep -F 校验生效,未生效非零退出。
    # M22-F01: 替换文本中的 & 必须转义 —— sed 里 & 展开为整个匹配
    # 文本,archive_command 的 && 曾把原注释拼进值(实测 conf 写坏)。
    local escaped
    escaped=$(printf '%s' "$value" | sed 's/[&\\]/\\&/g')
    if grep -qE "^[[:space:]]*#?[[:space:]]*${key}[[:space:]]*=" "$CONF"; then
        sed_inplace -E "s|^([[:space:]]*)#?[[:space:]]*${key}[[:space:]]*=.*|${key} = ${escaped}|" "$CONF"
    else
        echo "${key} = ${value}" >> "$CONF"
    fi
    grep -Fq "${key} = ${value}" "$CONF" || {
        echo "ERROR: failed to set ${key} in ${CONF}" >&2
        exit 1
    }
}

# replica 级别已足够(archive 命令需要 archive_mode);PITR 恢复需
# 连续 WAL 段,wal_level=replica 保留行级变更。
set_conf "wal_level" "replica"
set_conf "archive_mode" "on"
set_conf "archive_command" "'test ! -f ${ARCHIVE_DIR}/%f && cp %p ${ARCHIVE_DIR}/%f'"
set_conf "archive_timeout" "300"

echo "OK: postgresql.conf updated. 生效步骤(受控重启窗口执行,勿在引擎运行中执行):"
echo "  brew services restart postgresql@16"
echo "验证:"
echo "  psql -h localhost -U beidou_app -d beidou_testnet -c 'SHOW archive_mode;'   # 期望 on"
echo "  ls ${ARCHIVE_DIR}   # 若干 000000010000... 文件"
echo "恢复验证(破坏性,独立环境演练): pg_ctl promote/restore_command 演练见 docs"
