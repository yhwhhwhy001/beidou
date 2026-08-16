# TASK-M00-F02 证据：--no-self-heal 语义修复（P0-01）

## 问题

`--no-self-heal` 显式关闭自动恢复，但两条路径未检查 `self.self_heal`：

1. `_maybe_testnet_auto_reauthorize`（supervisor.py:1006）：testnet 防抖清洁后自动重新授权 + RESUME —— **完全不检查 self_heal**；
2. `_recover_if_validated` 的 ACTIVE 捷径（supervisor.py:945-956）：`self_heal` 检查在 RECOVERING 路径（:960），ACTIVE 捷径在检查之前直接补发 RESUME。

后果：launchd 实装以 `--no-self-heal` 启动，但 testnet 故障自愈后仍会自动 RESUME —— 配置语义与行为不符（P0-01）。

## 修改

- `beidou_launcher/supervisor.py`：
  - `_recover_if_validated`：ACTIVE 捷径之前新增 `if not self.self_heal: return False`（含审计 print）
  - `_maybe_testnet_auto_reauthorize`：mode 检查之后新增同样的 self_heal 门（含审计 print）
  - 两处 docstring 更新（记录 M00-F02 语义）
- `tests/unit/test_supervisor_testnet_auto_resume.py`：
  - helper 增加 `self_heal: bool = True` 参数
  - 新增 3 个测试：`test_no_self_heal_disables_testnet_auto_reauthorize`、`test_no_self_heal_disables_active_shortcut_resume`、`test_self_heal_active_shortcut_resume_preserved`（对照）

## 测试证据（TDD 红灯→绿灯）

| 阶段 | 命令 | 结果 |
|---|---|---|
| 红灯 | `.venv/bin/pytest tests/unit/test_supervisor_testnet_auto_resume.py -q` | 2 failed（恰好是新测试）、6 passed |
| 绿灯 | 同上（修复后） | 8 passed in 0.76s |
| 回归（自然顺序） | `pytest tests/unit/test_launcher.py tests/unit/test_launcher_cli_contracts.py tests/unit/test_supervisor_heartbeat.py tests/unit/test_supervisor_testnet_auto_resume.py tests/unit/test_reconciliation_contract.py tests/architecture/test_architecture.py -q` | 89 passed in 3.10s |
| lint | `ruff check beidou_launcher/supervisor.py tests/unit/test_supervisor_testnet_auto_resume.py` | All checks passed |
| format | `ruff format` 应用于新测试文件 | 已格式化 |

## 行为影响声明

- live/canary/paper：无变化（本方法仅 testnet 进入，且原本无自动 RESUME）。
- testnet + `--no-self-heal`：故障撤销授权后不再自动 RESUME，需人工/重启授权 —— 这是用户显式配置的语义。
- 当前运行引擎（PID 77694，18:59 启动）运行的是修复前代码；本修复在下次重启后生效。当前引擎因 15 条保护 blocker 本就无法触发自动重授权（`report.blockers` 非空短路），**本修复不会改变当前运行行为**。

## 附带发现（登记至 M21）

- 测试污染：`test_launcher_cli_contracts` 改变进程 CWD 未恢复 → 若在其后运行 `test_launcher`，相对路径（pyproject.toml / deploy/*.plist / 日志目录）读取失败。全量套件按字母序恰好不触发（test_launcher 先于 test_launcher_cli_contracts）。属测试卫生问题，M21 修复。
- supervisor.py:485 附近存在既有 format 漂移（非本次引入），M21 统一处理。
