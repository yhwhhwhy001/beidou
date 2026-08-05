.PHONY: install lint typecheck test test-unit test-integration test-architecture test-all clean build run-safety run-strategy run-research verify

PACKAGE_DIR := packages
TEST_DIR := tests

# 安装
install:
	pip install -e ".[dev]"
	pre-commit install

# 代码质量
lint:
	ruff check $(PACKAGE_DIR) $(TEST_DIR)
	ruff format --check $(PACKAGE_DIR) $(TEST_DIR)

lint-fix:
	ruff check --fix $(PACKAGE_DIR) $(TEST_DIR)
	ruff format $(PACKAGE_DIR) $(TEST_DIR)

typecheck:
	mypy $(PACKAGE_DIR)

# 安全扫描
security-scan:
	bandit -r $(PACKAGE_DIR) -c pyproject.toml
	pip-audit

# 死代码检测
dead-code:
	vulture $(PACKAGE_DIR) --min-confidence 80

# 测试
test-unit:
	pytest $(TEST_DIR)/unit -v -m unit

test-integration:
	pytest $(TEST_DIR)/integration -v -m integration

test-architecture:
	pytest $(TEST_DIR)/architecture -v -m architecture

test-all:
	pytest $(TEST_DIR) -v

test-coverage:
	pytest $(TEST_DIR) -v --cov --cov-report=html --cov-report=term-missing

# 构建
build:
	python -m build

# 清理
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf build/ dist/ htmlcov/ .coverage

# 校验包
verify:
	python tests/architecture/test_architecture.py || true
	ruff check $(PACKAGE_DIR)
	mypy $(PACKAGE_DIR) --no-error-summary || true

# 运行
run-safety:
	python -m apps.safety_executor

run-strategy:
	python -m apps.strategy_engine

run-research:
	python -m apps.research_lab
