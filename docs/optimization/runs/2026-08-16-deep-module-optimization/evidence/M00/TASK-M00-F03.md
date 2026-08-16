# TASK-M00-F03 证据：重复 blocker 去重与告警抑制（P0-19）

## 问题

1. 保护覆盖类检查按持仓逐条产出相同 (check_id, message) 的 P0 blocker：当前运行态 15 持仓 MISSING_SL/MISSING_TP = 15 条重复条目，全量拼进 `_fail_closed` 理由串与告警文本 → 日志巨型重复行 + webhook 风暴。
2. `_monitor` 中 `debounce_action == "DEGRADED"/"LOCKED"` 分支**每个 5s 周期都执行**（非状态转移时也执行）→ 重复 `_fail_closed`（含巨型 print）+ 重复 HIGH 告警。实测 launchd 日志连续周期重复打印 15 条 blocker 拼接串。

## 修改

- `beidou_launcher/models.py`：
  - `StartupReport.blocker_summary` 属性：按 (check_id, message) 聚合，保留 entity_ids 明细（前 50）与 count/severity
  - `to_dict()` 新增非破坏性 `blocker_summary` 字段（原始 `blockers` 明细保留）
- `beidou_launcher/supervisor.py`：
  - 新增模块级 `summarize_blockers()`：聚合为 `check_id(×N) entities=M: message[:80]`，总长 ≤400 字符
  - `_send_supervisor_alert()` 使用聚合摘要
  - 防抖分支链抽为 `_apply_debounce_action()`（可独立测试）：
    - LOCKED/DEGRADED 的降级+告警仅在**状态转移**时执行一次
    - 已在 DEGRADED 期间保留**静默 fail-closed 背压**：控制面若被意外 RESUME，立即拉回 NO_NEW_RISK（不打印不告警）——防御纵深不回退
  - UNCHANGED 分支理由串同样使用聚合摘要

## 测试证据（TDD 红灯→绿灯）

| 阶段 | 命令 | 结果 |
|---|---|---|
| 红灯 | `pytest tests/unit/test_supervisor_blocker_aggregation.py` | 1 collection error（导入缺失=未实现） |
| 绿灯 | 同上 | 7 passed in 0.17s |
| 回归 | supervisor_health_contract + heartbeat + blocker_aggregation + auto_resume + launcher + launcher_cli_contracts + monitoring_services + s5_monitoring_lifecycle + architecture | 178 passed in 3.25s |
| lint | `ruff check beidou_launcher/supervisor.py beidou_launcher/models.py tests/unit/test_supervisor_blocker_aggregation.py` | All checks passed |

## 测试覆盖矩阵

| REQ | 测试 |
|---|---|
| REQ-M00-005 聚合（同 check_id 去重、保留实体明细、短文本） | test_summarize_blockers_aggregates_duplicates / separates_distinct_checks / empty；test_report_to_dict_includes_blocker_summary |
| REQ-M00-006 告警转移门控 | test_degrade_alert_fires_once_on_transition_only；test_lock_transition_still_alerts_and_stays_silent_after |
| 静默背压（防御纵深不回退） | test_degraded_cycle_reasserts_fail_closed_silently |

## 行为影响声明

- 运行引擎（PID 77694）运行修复前代码；本修复下次重启后生效。
- 告警去重效果：DEGRADED 持续期间 0 重复告警（转移时 1 条聚合告警）；backstop 保证 DEGRADED 期间控制面不可停留 RESUME。
- supervisor-state.json 增加 `blocker_summary` 字段，旧字段 `blockers` 不变（向后兼容）。
