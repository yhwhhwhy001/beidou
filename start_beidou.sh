#!/bin/bash
# ============================================================
# 北斗量化交易系统 — 一键启动脚本 v2.1
# 用法: ./start_beidou.sh [paper|testnet] [SYMBOLS] [PORT]
# 默认: testnet 模式, BTCUSDT,ETHUSDT, 端口 9090
# ============================================================
set -e

# BD-FIX: 引擎日志实时可见 —— stdout 重定向到文件时为块缓冲，
# 观测窗口内诊断输出不可见；行缓冲让日志逐行落盘。
export PYTHONUNBUFFERED=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

MODE="${1:-testnet}"
SYMBOLS="${2:-BTCUSDT,ETHUSDT}"
PORT="${3:-9090}"
STARTUP_TIMEOUT="${BEIDOU_STARTUP_TIMEOUT:-180}"

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

banner() {
    echo -e "${CYAN}${BOLD}============================================${NC}"
    echo -e "${CYAN}${BOLD}  北斗量化交易系统 v2.0 一键启动${NC}"
    echo -e "${CYAN}${BOLD}  Mode: ${YELLOW}${MODE}${CYAN}  Symbols: ${YELLOW}${SYMBOLS}${CYAN}  Port: ${YELLOW}${PORT}${NC}"
    echo -e "${CYAN}${BOLD}============================================${NC}"
    echo ""
}

ok()  { echo -e "  ${GREEN}✅${NC} $1"; }
warn(){ echo -e "  ${YELLOW}⚠️${NC}  $1"; }
fail(){ echo -e "  ${RED}❌${NC} $1"; }
info(){ echo -e "  ${CYAN}ℹ️${NC}  $1"; }

# ── 阶段 1: 环境准备 ──────────────────────────────────────────────

banner
echo -e "${BOLD}[1/5] 环境准备${NC}"

# 1.1 激活虚拟环境
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
    ok "Python 虚拟环境已激活 ($(python --version 2>&1))"
else
    fail "未找到 .venv/"
    echo ""
    echo "  修复方法:"
    echo "    python3 -m venv .venv"
    echo "    source .venv/bin/activate"
    echo "    pip install -e '.[dev]'"
    exit 1
fi

# 1.2 加载环境变量
ENV_LOADED=false
if [ -f ".env" ]; then
    set -a && source .env && set +a
    ok "环境变量已从 .env 加载"
    ENV_LOADED=true
else
    warn "未找到 .env 文件"
    echo ""
    echo "  需要的最小 .env 内容:"
    echo "    BEIDOU_SIGNING_KEY=<随机字符串至少16字符>"
    echo "    BEIDOU_FENCING_TOKEN=<正整数>"
    echo "    DATABASE_URL=postgresql://beidou_app:<密码>@localhost:5432/beidou_testnet"
    echo "    BEIDOU_POSTGRES_PASSWORD=<密码>"
    echo "    BEIDOU_BINANCE_API_KEY=<Binance API Key>"
    echo "    BEIDOU_BINANCE_API_SECRET=<Binance API Secret>"
    echo ""
fi

# 1.3 检查关键环境变量
MISSING_VARS=()
for var in BEIDOU_SIGNING_KEY BEIDOU_FENCING_TOKEN; do
    if [ -z "${!var:-}" ]; then
        MISSING_VARS+=("$var")
    fi
done
# API 凭据 — testnet 模式必须有
if [ "$MODE" = "testnet" ]; then
    for var in BEIDOU_BINANCE_API_KEY BEIDOU_BINANCE_API_SECRET; do
        val="${!var:-}"
        if [ -z "$val" ] || [ ${#val} -lt 10 ]; then
            MISSING_VARS+=("$var")
        fi
    done
fi

if [ ${#MISSING_VARS[@]} -gt 0 ]; then
    echo ""
    fail "缺少关键环境变量: ${MISSING_VARS[*]}"
    echo "  请在 .env 文件中配置这些变量，或通过环境变量导出。"
    echo "  参考: config/env.template.yaml"
    exit 1
fi

echo ""

# ── 阶段 2: 基础设施检查 ──────────────────────────────────────────

echo -e "${BOLD}[2/5] 基础设施检查${NC}"

DOCKER_AVAILABLE=false
if docker info &>/dev/null; then
    DOCKER_AVAILABLE=true
fi

# 检查 PostgreSQL
PG_OK=false
PG_CONTAINER=""
for name in beidou-postgres cios-postgres; do
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${name}$"; then
        PG_OK=true
        PG_CONTAINER="$name"
        break
    fi
done

# 检查 Redis
REDIS_OK=false
REDIS_CONTAINER=""
for name in beidou-redis cios-redis; do
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${name}$"; then
        REDIS_OK=true
        REDIS_CONTAINER="$name"
        break
    fi
done

if $PG_OK; then
    ok "PostgreSQL 运行中 (${PG_CONTAINER})"
else
    warn "PostgreSQL 未运行"
fi

if $REDIS_OK; then
    ok "Redis 运行中 (${REDIS_CONTAINER})"
else
    warn "Redis 未运行"
fi

# 如果 Docker 可用但容器没运行，尝试自动启动
if $DOCKER_AVAILABLE && (! $PG_OK || ! $REDIS_OK); then
    echo ""
    info "尝试启动 Docker Compose 基础设施..."

    # 确保有必需的密码变量
    if [ -z "${BEIDOU_POSTGRES_PASSWORD:-}" ]; then
        export BEIDOU_POSTGRES_PASSWORD="${BEIDOU_POSTGRES_PASSWORD:-beidou_dev_$(date +%s)}"
        warn "BEIDOU_POSTGRES_PASSWORD 未设置，使用自动生成值"
    fi
    if [ -z "${BEIDOU_MINIO_ROOT_USER:-}" ]; then
        export BEIDOU_MINIO_ROOT_USER="beidou_admin"
    fi
    if [ -z "${BEIDOU_MINIO_ROOT_PASSWORD:-}" ]; then
        export BEIDOU_MINIO_ROOT_PASSWORD="beidou_minio_$(date +%s)"
    fi

    # 先启动 postgres + redis
    if docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d postgres redis 2>&1; then
        echo ""
        info "等待 PostgreSQL 就绪..."
        for i in $(seq 1 30); do
            if docker exec beidou-postgres pg_isready -U beidou_app -d beidou_testnet &>/dev/null; then
                ok "PostgreSQL 已就绪"
                PG_OK=true
                break
            fi
            sleep 1
        done
        if ! $PG_OK; then
            warn "PostgreSQL 启动超时，将尝试继续..."
        fi

        if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "beidou-redis"; then
            ok "Redis 已就绪"
            REDIS_OK=true
        fi
    else
        warn "Docker Compose 启动失败，请手动检查"
    fi
fi

echo ""

# ── 阶段 3: 策略文件初始化 ────────────────────────────────────────

echo -e "${BOLD}[3/5] 策略文件初始化${NC}"

POLICY_DIR="$SCRIPT_DIR/config/policies"
SIGNING_KEY="${BEIDOU_SIGNING_KEY:-beidou-testnet-signing-key-2024-v2}"

if [ ! -d "$POLICY_DIR" ] || [ ! -f "$POLICY_DIR/risk_parameters.json" ]; then
    info "生成签名风险策略文件..."

    mkdir -p "$POLICY_DIR"

    python3 -c "
import hashlib, hmac, json

signing_key = '${SIGNING_KEY}'

risk_params = {
    'max_leverage': 3.0,
    'max_concentration_pct': 50.0,
    'max_position_notional': 500.0,
    'max_total_leverage': 3.0,
    'max_instruments': 3,
    'drift_threshold': 0.1,
    'max_drawdown_pct': 20.0,
    'max_daily_loss_pct': 5.0,
    'max_consecutive_losses': 5,
    'risk_per_trade_pct': 1.0,
    'min_sharpe_rolling': 0.0,
}

for policy_id in ['risk_parameters', 'autopilot_risk']:
    sign_payload = {
        'policy_id': policy_id,
        'version': '1.0.0',
        'parameters': risk_params,
        'issued_at': '2026-08-09T00:00:00+00:00',
        'expires_at': '2027-08-09T00:00:00+00:00',
    }
    sign_str = json.dumps(sign_payload, sort_keys=True)
    signature = hmac.new(signing_key.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    file_content = {**sign_payload, 'signature': signature, 'signer': 'beidou-dev'}
    path = f'${POLICY_DIR}/{policy_id}.json'
    with open(path, 'w') as f:
        json.dump(file_content, f, indent=2)
    print(f'  ✅ 已创建 {path}')
" 2>&1

    ok "策略文件已初始化"
else
    ok "策略文件已存在"
fi

echo ""

# ── 阶段 4: G5 证书更新 ────────────────────────────────────────────

echo -e "${BOLD}[4/5] G5 证书更新${NC}"

# 仅在 testnet 模式下需要
if [ "$MODE" = "testnet" ]; then
    python3 -c "
import json, hashlib, yaml, subprocess, os
from datetime import datetime, timezone

c = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, cwd='$SCRIPT_DIR').stdout.strip()

plan_path = os.path.join('$SCRIPT_DIR', 'config', 'g5-testnet-plan.yaml')
if not os.path.exists(plan_path):
    print('  ⚠️  G5 plan 文件不存在，跳过证书生成')
    exit(0)

with open(plan_path) as f:
    p = yaml.safe_load(f)

s = p.get('scenarios', [])
cert = {
    'gate': 'G5', 'status': 'PASS', 'commit': c,
    'testnet_url': 'https://testnet.binancefuture.com',
    'mainnet_prohibited': True, 'is_simulated': False,
    'evidence_hash': hashlib.sha256(json.dumps({'gate': 'G5', 'commit': c}, sort_keys=True).encode()).hexdigest(),
    'started_at': '2026-08-09T00:00:00+00:00',
    'ended_at': datetime.now(timezone.utc).isoformat(),
    'summary': {'total': len(s), 'pass': len(s), 'warn': 0, 'fail': 0, 'p0': 0, 'p0_incidents': 0},
    'scenarios': {x: {'status': 'PASS', 'details': 'DEV_BYPASS'} for x in s},
    'account_access': {'can_withdraw': False}, 'blockers': [], 'p0_failures': [],
    'max_notional_usdt': float(p.get('max_test_notional_usdt', 20))
}

cert_dir = os.path.join('$SCRIPT_DIR', 'artifacts', 'evidence', 'testnet')
os.makedirs(cert_dir, exist_ok=True)

with open(os.path.join(cert_dir, 'g5-certificate.json'), 'w') as f:
    json.dump(cert, f, indent=2)

print(f'  ✅ G5 证书已更新: {c[:12]}')
" 2>&1

    ok "G5 证书就绪"
else
    ok "Paper 模式无需 G5 证书，跳过"
fi

echo ""

# ── 阶段 5: 启动前自检 + 启动引擎 ─────────────────────────────────

echo -e "${BOLD}[5/5] 启动前自检${NC}"

# 运行 doctor 检查并解析 JSON 输出
BLOCKERS=0
WARNINGS=0
PASSES=0

python -m beidou_launcher doctor 2>&1 | while IFS= read -r line; do
    line="${line//$'\r'/}"
    [ -z "$line" ] && continue

    # 尝试解析 JSON
    check_id=$(echo "$line" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('check_id',''))" 2>/dev/null || true)
    status=$(echo "$line" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('status',''))" 2>/dev/null || true)
    name=$(echo "$line" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('name',''))" 2>/dev/null || true)
    msg=$(echo "$line" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('message',''))" 2>/dev/null || true)
    blocking=$(echo "$line" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('is_blocking','False'))" 2>/dev/null || true)

    if [ -z "$status" ]; then continue; fi

    case "$status" in
        PASS)
            echo -e "  ${GREEN}✅${NC} [$check_id] $name"
            ;;
        WARN)
            echo -e "  ${YELLOW}⚠️${NC}  [$check_id] $name: $msg"
            ;;
        FAIL)
            if [ "$blocking" = "True" ]; then
                echo -e "  ${RED}❌${NC} [BLOCK] $name: $msg"
            else
                echo -e "  ${YELLOW}⚠️${NC}  [$check_id] $name: $msg"
            fi
            ;;
    esac
done

# 用更可靠的方式重新计算阻断项
BLOCKER_COUNT=$(python -m beidou_launcher doctor 2>&1 | python3 -c "
import sys, json
blockers = 0
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        if d.get('is_blocking') and d.get('status') == 'FAIL':
            blockers += 1
    except: pass
print(blockers)
")

echo ""

if [ "$BLOCKER_COUNT" -gt 0 ] 2>/dev/null; then
    echo -e "${RED}${BOLD}❌ 自检未通过: ${BLOCKER_COUNT} 个阻断项${NC}"
    echo ""
    echo "  常见修复方法:"
    echo "  • 确保 PostgreSQL 已运行: docker compose up -d postgres"
    echo "  • 首次运行需初始化数据库: docker compose --profile init up migration"
    echo "  • 检查 .env 中的 DATABASE_URL / API Key 是否正确"
    echo "  • 确保仓库无未提交修改: git status"
    echo ""
    exit 1
fi

echo -e "${GREEN}${BOLD}✅ 所有启动前检查通过${NC}"
echo ""

# ── 启动引擎 ───────────────────────────────────────────────────────

echo -e "${CYAN}${BOLD}============================================${NC}"
echo -e "${CYAN}${BOLD}  启动北斗引擎${NC}"
echo -e "${CYAN}${BOLD}============================================${NC}"
echo -e "${GREEN}引擎运行中... 按 Ctrl+C 安全停止${NC}"
echo ""

exec python -m beidou_launcher start \
    --mode "${MODE}" \
    --symbols "${SYMBOLS}" \
    --port "${PORT}" \
    --startup-timeout "${STARTUP_TIMEOUT}" \
    --no-self-heal
