"""G5 注册表完整性测试:16 场景全部注册且与 testnet 计划一致。"""

from pathlib import Path

import yaml

from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY

_PLAN_PATH = Path(__file__).resolve().parents[3] / "config" / "g5-testnet-plan.yaml"

# 注册序 = 各场景模块在 import DAG 上的 DFS 访问序(dict 保持插入序,确定性);
# 该顺序即 runner.run_selected 的实际执行序。reconciliation_mismatch 在
# restart 组之后执行,避免它的短暂监督器事件污染重启场景前置；partial_fill
# 仍固定在最后以保留其终态清理语义,故 pin 期望序而非 sorted()。
EXPECTED_ORDER = [
    "ack_loss",
    "cancel_fill_race",
    "timeout_unknown_recovery",
    "double_worker_fencing",
    "create_query_cancel",
    "clock_skew",
    "credential_failure",
    "duplicate_request",
    "rate_limit",
    "stable_client_order_id",
    "native_protection",
    "process_restart",
    "database_restart",
    "user_stream_reconnect",
    "reconciliation_mismatch",
    # Ruling-20: partial_fill 移到最后 —— PASS 后引擎 ghost 订单缺陷
    # (CANCELED-with-executedQty 不落 order_state)会污染后续场景前置。
    "partial_fill",
]


def test_registry_covers_plan_scenarios():
    with _PLAN_PATH.open() as f:
        plan = yaml.safe_load(f)
    expected = set(plan["scenarios"])
    assert set(SCENARIO_REGISTRY) == expected


def test_registry_importable_and_ordered():
    assert len(SCENARIO_REGISTRY) == 16
    ids = list(SCENARIO_REGISTRY)
    # brief 逐字断言 ids == sorted(ids) 与注册语义冲突(Task 12 裁决 3:
    # 不改注册顺序去迁就断言),改 pin 期望注册序(见 EXPECTED_ORDER)。
    assert ids == EXPECTED_ORDER
    assert all(cls.scenario_id == sid for sid, cls in SCENARIO_REGISTRY.items())
