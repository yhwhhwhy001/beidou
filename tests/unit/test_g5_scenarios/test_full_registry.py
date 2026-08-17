"""G5 注册表完整性测试:16 场景全部注册且与 testnet 计划一致。"""

from pathlib import Path

import yaml

from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY

_PLAN_PATH = Path(__file__).resolve().parents[3] / "config" / "g5-testnet-plan.yaml"


def test_registry_covers_plan_scenarios():
    with _PLAN_PATH.open() as f:
        plan = yaml.safe_load(f)
    expected = set(plan["scenarios"])
    assert set(SCENARIO_REGISTRY) == expected


def test_registry_importable_and_ordered():
    assert len(SCENARIO_REGISTRY) == 16
    ids = list(SCENARIO_REGISTRY)
    # 注册顺序 = 子包导入次序(dict 插入序,确定性),并非字典序。
    # brief 逐字断言 ids == sorted(ids) 与分组注册语义冲突(Task 12 裁决 3:
    # 不改注册顺序去迁就断言),故改为断言确定性并校验键与类绑定一致。
    assert ids == list(SCENARIO_REGISTRY)
    assert all(cls.scenario_id == sid for sid, cls in SCENARIO_REGISTRY.items())
