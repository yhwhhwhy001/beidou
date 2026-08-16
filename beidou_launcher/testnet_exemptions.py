"""testnet 特赦登记表（M00-F05，P0-20 治理）。

集中登记引擎内所有 testnet-only 安全门放宽项。约束:

- 引擎代码对应处必须带 ``# TESTNET-EXEMPT: <id>`` 标记（架构测试断言
  登记与标记双向一致，漂移即失败）；
- 每项必须给出理由、风险接受记录与负责复审的模块；
- 收紧/移除由责任模块（M10/M12/M13/M19）逐项裁决，本表只做可见性；
- 任何特赦都不得改变 live/canary/paper 的严格语义。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TestnetExemption:
    exemption_id: str
    title: str
    rationale: str
    reassessment_module: str
    risk_note: str

    @property
    def marker(self) -> str:
        return f"TESTNET-EXEMPT: {self.exemption_id}"


TESTNET_EXEMPTIONS: tuple[TestnetExemption, ...] = (
    TestnetExemption(
        exemption_id="EXEMPT-01",
        title="对账 event 侧漂移降 WARN（system/exchange 两方仍严格）",
        rationale="demo 用户流事件乱序/丢失使投影器与 fill 记账两条重建路径周期性分歧"
        "（final75 实测符号反转）；system/exchange 差异仍阻断。",
        reassessment_module="M13",
        risk_note="若 system/exchange 无差异但 event 侧失真，仓位重建仍可能失真；M13 评估投影器修正而非长期豁免。",
    ),
    TestnetExemption(
        exemption_id="EXEMPT-02",
        title="保护覆盖豁免无本地所有权的外部持仓",
        rationale="testnet 是共享 demo 账户，外部活动持仓不属于引擎，不得产生覆盖 gap；"
        "live/canary 中'交易所有持仓但本地无记录'仍是丢仓，必须 fail-closed。",
        reassessment_module="M12",
        risk_note="引擎自己的持仓所有权判定依赖 position_generation 投影准确性；M12 复核所有权证据链。",
    ),
    TestnetExemption(
        exemption_id="EXEMPT-03",
        title="user stream 事件新鲜度放宽至 300s",
        rationale="demo 低频环境凌晨可 10+ 分钟无事件，30s 即 STALE → 三方对账恒失败；"
        "事件停流保护语义不变（CONNECTED + listenKey 有效才放宽）。",
        reassessment_module="M13",
        risk_note="停流检测灵敏度从 60s 降至 300s；M13 复核与 _reconciliation_max_age_seconds 对齐。",
    ),
    TestnetExemption(
        exemption_id="EXEMPT-04",
        title="liquidationPrice 缺失时用杠杆推导保守近似",
        rationale="demo 账户快照不提供 liquidationPrice 字段（实测 None），R7 恒 UNKNOWN"
        " 使持仓后无法下任何新单；推导为 entry×(1∓1/lev) 标准名义近似。",
        reassessment_module="M10",
        risk_note="推导值可能高估安全边际；live/canary 保持缺失即 UNKNOWN。M10 复核推导公式。",
    ),
    TestnetExemption(
        exemption_id="EXEMPT-05",
        title="对账事实新鲜度阈值 300s（默认 30s）",
        rationale="replay baseline 授权后投影时间戳冻结，demo 低频事件下 30s 恒 STALE →"
        " DEGRADED；与 readiness event_age 阈值（CONNECTED 300s）一致。",
        reassessment_module="M13",
        risk_note="对账事实陈旧窗口放大 10 倍；live/canary/paper 保持 30s。",
    ),
    TestnetExemption(
        exemption_id="EXEMPT-06",
        title="快照 exchange_health 用核心活性判定",
        rationale="完整 liveness 的 user stream/对账/控制面瞬时状态在 demo 抖动下反复"
        " DEGRADED → R0 恒拒（final59 实测 auxskip 增长）；这些维度的安全由各自门禁负责。",
        reassessment_module="M10",
        risk_note="exchange_health 不再反映 user stream/对账瞬时状态；M10 复核 R0 快照门。",
    ),
    TestnetExemption(
        exemption_id="EXEMPT-07",
        title="引擎侧 durable_ok 后自动恢复 RESUME",
        rationale="durable 事实验证通过 + 所有权确认后恢复 RESUME（引擎内）；"
        "与 supervisor 的 _maybe_testnet_auto_reauthorize 属两个层级。",
        reassessment_module="M19",
        risk_note="自动恢复不经过 TruthSnapshot 授权链（P0-12）；M19 统一挂接授权链。",
    ),
    TestnetExemption(
        exemption_id="EXEMPT-08",
        title="零写模式跳过对账的 liveness 标准",
        rationale="零写模式按设计跳过对账，_last_reconciliation_result 恒 None；"
        "以 RESUME + realtime 活跃为健康标准，与 supervisor 对账豁免同语义。",
        reassessment_module="M13",
        risk_note="paper/shadow/research 环境无对账健康维度；可写环境保持严格。",
    ),
)
