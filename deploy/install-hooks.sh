#!/bin/bash
# 北斗 V5 — 安装本地 git hooks
#
#   bash deploy/install-hooks.sh
#
# 每个新 clone 跑一次。`git worktree` 不用再跑——`core.hooksPath` 是仓库级配置，
# worktree 共享同一份 `.git/config`。
#
# 为什么不用 `cp .githooks/* .git/hooks/`：那是复制，会漂。改了 `.githooks/pre-commit`
# 之后没人会想起去同步那份副本，于是本地跑的是三个月前的旧 hook 而看起来一切正常。
# `core.hooksPath` 是指向，不是副本。

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

git config core.hooksPath .githooks
chmod +x .githooks/* 2>/dev/null || true

echo "hooks 已指向 .githooks/（core.hooksPath）"
echo "  当前值: $(git config core.hooksPath)"
for h in .githooks/*; do
    [ -f "$h" ] || continue
    if [ -x "$h" ]; then
        echo "  ✓ $(basename "$h")"
    else
        echo "  ✗ $(basename "$h") 没有可执行位——git 会静默跳过它"
    fi
done

if command -v gitleaks >/dev/null 2>&1; then
    echo "  gitleaks: $(gitleaks version 2>&1 | head -1)"
else
    echo
    echo "  ⚠  gitleaks 没装，pre-commit 会放行并打一行提示。"
    echo "     装它：brew install gitleaks"
fi

echo
echo "自检（应当拦下一个伪造的凭据形态）:"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if command -v gitleaks >/dev/null 2>&1; then
    # 造一个 Binance 形态的假值。不用真密钥，也不写进仓库目录。
    python3 - "$tmp" <<'PY'
import random, string, sys, pathlib
random.seed()
a, A, d = string.ascii_lowercase, string.ascii_uppercase, string.digits
v = ([random.choice(a) for _ in range(28)]
     + [random.choice(A) for _ in range(26)]
     + [random.choice(d) for _ in range(10)])
random.shuffle(v)
pathlib.Path(sys.argv[1], "canary.txt").write_text(
    "BEIDOU_BINANCE_API_KEY=%s\n" % "".join(v), encoding="utf-8")
PY
    # 有命中时让 gitleaks 用这个码退出（`--exit-code`），与两个 hook 同一个判据。只认它才打 ✓。
    # gitleaks 自己出错时也是非 0：配置不在或写坏是 FTL、退 1，与有命中时的默认退出码相同；
    # 规则里有 lookahead 是 panic、退 2。2026-09-29 之前这里只看非 0，这两种情况都打 ✓，
    # gitleaks 的原话进了 /dev/null。SECURITY.md 让人「看到 ✓ 才算装好」，防的正是这两种。
    leaks_rc=42
    rc=0
    scan_out="$(gitleaks detect --source "$tmp" --config "$repo_root/.gitleaks.toml" \
            --no-git --redact --no-banner --no-color --exit-code "$leaks_rc" 2>&1)" || rc=$?
    case "$rc" in
        "$leaks_rc")
            echo "  ✓ 伪造的凭据形态被拦下，扫描是活的。"
            ;;
        0)
            echo "  ✗ 自检失败：伪造的凭据形态没有被拦下。"
            echo "    这说明 .gitleaks.toml 的 allowlist 被放得太宽，扫描已经失去意义。"
            exit 1
            ;;
        *)
            # `${rc}` 的花括号不能省：UTF-8 locale 下 macOS 的 bash 3.2 会把紧跟的全角括号
            # 读进变量名，`set -u` 下脚本当场崩掉。
            echo "  ✗ 自检没有跑完：gitleaks 没有扫完就退出了（退出码 ${rc}），它的原话："
            printf '%s\n' "$scan_out" | sed 's/^/    /'
            echo '    按上面的原话修。常见的两种都是一行没扫：.gitleaks.toml 不在或写坏了；'
            echo '    规则里写了 RE2 不支持的正则，例如 lookahead `(?=`，gitleaks 会直接 panic。'
            echo '    修好之后重跑这个脚本，看到 ✓ 才算装好。'
            exit 1
            ;;
    esac
else
    echo "  (跳过：gitleaks 未安装)"
fi
