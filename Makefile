.PHONY: install lint typecheck test test-unit test-integration test-architecture test-all clean build verify

BEIDOU_PACKAGES := beidou_shared beidou_safety beidou_strategy beidou_research beidou_policy beidou_security beidou_observability beidou_exchange beidou_lifecycle beidou_delivery beidou_autonomy beidou_infra beidou_data beidou_control beidou_chaos beidou_production beidou_reporting beidou_certification beidou_core beidou_launcher
TEST_DIR := tests
APPS_DIR := apps

# 安装
install:
	pip install -e ".[dev]"
	pre-commit install

# 代码质量
lint:
	ruff check $(BEIDOU_PACKAGES) $(APPS_DIR) $(TEST_DIR)
	ruff format --check $(BEIDOU_PACKAGES) $(APPS_DIR) $(TEST_DIR)

lint-fix:
	ruff check --fix $(BEIDOU_PACKAGES) $(APPS_DIR) $(TEST_DIR)
	ruff format $(BEIDOU_PACKAGES) $(APPS_DIR) $(TEST_DIR)

typecheck:
	mypy $(BEIDOU_PACKAGES) $(APPS_DIR)

# 安全扫描
security-scan:
	bandit -r $(BEIDOU_PACKAGES) -c pyproject.toml
	pip-audit

# 死代码检测
dead-code:
	vulture $(BEIDOU_PACKAGES) --min-confidence 80

# 测试
test-unit:
	pytest $(TEST_DIR)/unit -v

test-integration:
	pytest $(TEST_DIR)/integration -v

test-architecture:
	pytest $(TEST_DIR)/architecture -v

test-all:
	pytest $(TEST_DIR) -v

test-coverage:
	pytest $(TEST_DIR) -v --cov --cov-report=html --cov-report=term-missing --cov-report=xml

# 测试质量扫描
test-quality:
	python scripts/scan_test_quality.py $(TEST_DIR)

# 硬编码扫描
hardcoded-scan:
	python scripts/scan_hardcoded.py $(BEIDOU_PACKAGES)

# 构建
build:
	python -m build

# 构建并 smoke test
build-smoke:
	python -m build
	python -m venv /tmp/beidou-smoke-venv
	/tmp/beidou-smoke-venv/bin/pip install dist/*.whl
	/tmp/beidou-smoke-venv/bin/python -c "import beidou_shared; import beidou_core; print('SMOKE TEST PASSED')"
	rm -rf /tmp/beidou-smoke-venv

# SBOM + lockfile hash
sbom:
	@echo "=== SBOM Generation ==="
	pip freeze > requirements.lock
	sha256sum requirements.lock > requirements.lock.sha256
	@echo "Lockfile hash: $$(cat requirements.lock.sha256)"
	@echo "SBOM written to requirements.lock"

# 清理
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null; true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null; true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null; true
	rm -rf build/ dist/ htmlcov/ .coverage

# 校验 — 任一失败返回非零，不存在软失败
verify:
	@echo "=== LINT ===" && ruff check $(BEIDOU_PACKAGES) $(APPS_DIR) $(TEST_DIR) || exit 1
	@echo "=== TYPECHECK ===" && mypy $(BEIDOU_PACKAGES) --no-error-summary || exit 1
	@echo "=== UNIT TESTS ===" && pytest $(TEST_DIR)/unit -q || exit 1
	@echo "=== INTEGRATION TESTS ===" && pytest $(TEST_DIR)/integration -q || exit 1
	@echo "=== ARCHITECTURE TESTS ===" && pytest $(TEST_DIR)/architecture -q || exit 1
	@echo "=== TEST QUALITY SCAN ===" && python scripts/scan_test_quality.py $(TEST_DIR) || exit 1
	@echo "=== HARDCODED SCAN ===" && python scripts/scan_hardcoded.py $(BEIDOU_PACKAGES) || exit 1
	@echo "=== VERIFY PASSED ==="

# 运行
run-safety:
	@echo "apps.safety_executor is retired (M00-F06); use: beidou start --mode testnet"
	@exit 2

run-strategy:
	@echo "apps.strategy_engine is retired (M00-F06); use: beidou start --mode paper"
	@exit 2

run-research:
	@echo "apps.research_lab is retired (M00-F06); use: python -m apps.factor_miner run --policy config/factor_mining_policy.yaml"
	@exit 2
