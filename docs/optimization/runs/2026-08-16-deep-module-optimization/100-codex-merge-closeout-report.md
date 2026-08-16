# codex merge 收尾验收报告（2026-08-17）

Merge commit: `5c39fc9`（parent: `708885d` main + `9e5279d` codex/full-system-optimization）

## 背景

codex 会话发起 merge 后中断，留下 2 个 UU 冲突文件（含已进入工作树的冲突标记）与
39 个测试 collection errors。本会话接手：解决冲突 → 修复失败 → 全量门禁 → 提交推送。

## 关键处置

| # | 问题 | 处置 | 证据 |
|---|---|---|---|
| 1 | 冲突标记污染（preflight.py:245 / rest_client.py:399 等） | codex 会话自行解决后由本会话验证 | `pytest --collect-only`: 2916 collected, 0 errors |
| 2 | 注册表与 main 52 commits 脱节（198+ issues） | 新增 `scripts/rebuild_write_registry.py` 治理工具并重建 | 主扫描器 `{"issues": [], "status": "PASS"}` |
| 3 | 独立 oracle 233 项 mismatch（94 未注册/94 stale/40 scope） | findings 重绑（复用仍允许的旧绑定，新 finding 绑定首个允许记录） | `verify_coverage` 返回 `[]` |
| 4 | network purpose 契约不符（3 项） | ALERT_DELIVERY/FAULT_INJECTION_READ/LOCAL_NETWORK_BIND_CHECK 修正 | `test_network_import_inventory_is_exact_and_policy_bound` 通过 |
| 5 | M00-F06 引擎入口误判（扫描器 marker 文本） | 测试改 AST 判定（ast.Call + Name） | `test_no_second_engine_entry_outside_main_chain` 通过 |
| 6 | plist 模板携带 --no-self-heal（P0-01 复发源） | 模板移除 + XML 注释规范（禁双连字符） | `test_launchagent_template_is_direct_and_fail_closed` 通过；plutil+plistlib 双校验 OK |
| 7 | ruff 24 errors（S110/S112 吞异常等） | 吞异常日志化、autofix、format 统一 | `ruff check`: All checks passed |
| 8 | 实装 plist 缺 `BEIDOU_TERMINAL_WRITE_HOLD`（重启后 hard 模式锁死全部终端写） | 实装 plist 补配 `unknown-only`（备份 `.bak-pre-writehold`） | `plutil -lint` OK |

## 全量门禁实测（commit 5c39fc9）

- pytest: **2922 passed / 0 failed**（556.73s）
- coverage: **78.52%**（CI fail-under 78 通过）
- ruff check: All checks passed；ruff format --check: 463 files already formatted
- mypy（CI 目标 + beidou_launcher）: 0 errors
- write_registry 主扫描器: PASS；独立 oracle: 0 issues

## 遗留事项（显式登记）

- 运行中的引擎仍是 merge 前旧代码；新代码需受控重启后生效。重启前已确认：
  实装 plist 已配 `BEIDOU_TERMINAL_WRITE_HOLD=unknown-only`（否则 hard 模式锁死写路径）。
- `deploy/` 模板为 safety_only 定位（codex 合并语义）；实装 plist 的受控重启 wrapper
  语义不变，preflight 漂移检查保留。
- codex 分支遗留工作树（`/Users/maguannan/beidou-worktrees/...`）未清理，不属本次范围。

## 结论

**PASS_WITH_CONDITIONS** —— 条件：引擎受控重启由操作者按既有 SOP 执行（SIGKILL 旧进程 +
`launchctl kickstart gui/501/com.beidou.autopilot`），重启后观察 trading_ready 与 recon MATCHED。
