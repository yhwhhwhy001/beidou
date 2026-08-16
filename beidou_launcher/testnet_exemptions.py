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
    TestnetExemption(
        exemption_id="EXEMPT-09",
        title="对账余额相对容差 testnet 1% vs 其他 0.01%",
        rationale="共享 demo 账户外部活动漂移 ~0.14 USDT/分钟，0.01% 容差数分钟即失效"
        "（PKG20）；canary/live/paper/research 保持 0.0001 严格默认。",
        reassessment_module="M13",
        risk_note="100 倍容差放大 —— 余额差异检测灵敏度显著降低；M13 评估共享账户隔离方案。",
    ),
    TestnetExemption(
        exemption_id="EXEMPT-10",
        title="Algo inventory 查询失败/UNKNOWN 视为空列表",
        rationale="demo 端点瞬时失败即锁控全局下单过重（I4 审查）；保护单缺失重试有"
        " inventory 语义校验兜底；live/canary 返回 None → NO_NEW_RISK。",
        reassessment_module="M12",
        risk_note="'查询失败'与'确无单'不可区分 —— 幽灵保护单可能漏检；M12 复核兜底校验强度。",
    ),
    TestnetExemption(
        exemption_id="EXEMPT-11",
        title="user stream 事件降级为信息性（ALGO_UPDATE/MARGIN_CALL/未知事件）",
        rationale="demo 15:52 实测 FILLED 达成后 ALGO_UPDATE 触发 fault → NO_NEW_RISK；"
        "testnet 按信息性事件处理保持流健康；live/canary 保持 fault（自有算法单状态变化必须复核）。",
        reassessment_module="M13",
        risk_note="MARGIN_CALL 追缴事件在 testnet 降级为信息 —— 真追缴风险信号被掩盖；"
        "live/canary 语义不变。M13 复核。",
    ),
    TestnetExemption(
        exemption_id="EXEMPT-12",
        title="user stream 就绪不要求事件新鲜（CONNECTED 状态）",
        rationale="demo 低频环境事件停流保护由 transport 状态承担（listenKey 失效 → fault）；"
        "live/canary 保持严格 event_age 语义。",
        reassessment_module="M13",
        risk_note="与 EXEMPT-03 同族（300s 阈值），此处是完全豁免事件龄；M13 合并评估。",
    ),
    TestnetExemption(
        exemption_id="EXEMPT-13",
        title="两方对拍一致即自动授权 user stream replay baseline",
        rationale="sequencer 未授权 → event_stream INCOMPLETE 属鸡生蛋预期，不构成授权障碍；"
        "两方冲突时 fail-closed 不授权（PKG02 已移除仅仓位不匹配旁路）。",
        reassessment_module="M13",
        risk_note="replay baseline 授权后投影时间戳冻结（EXEMPT-05 的根源）；M13 评估投影器修正。",
    ),
    TestnetExemption(
        exemption_id="EXEMPT-14",
        title="本地权益估计替代共享余额（sizing 输入）",
        rationale="共享 demo 账户 REST 余额含外部资金，直接使用会放大/掩盖自有 drawdown、"
        "污染仓位 sizing；本地估计=启动基线+自有持仓 unrealized；非 testnet 直接返回共享余额。",
        reassessment_module="M10",
        risk_note="sizing 输入的权益口径与交易所不同 —— 组合层资金边界由本地估计定义；M10 复核。",
    ),
    TestnetExemption(
        exemption_id="EXEMPT-15",
        title="提款权限（R9）testnet 豁免：不告警、风险视角按 False 结算",
        rationale="testnet 测试资金由交易所默认开启提款权限；非 testnet 保持 CRITICAL 阻断"
        "（PKG02 R9 统一检查）。",
        reassessment_module="M10",
        risk_note="提款权限在 testnet 完全退出风控告警面 —— live/canary 语义不变；M10 复核 R9。",
    ),
    TestnetExemption(
        exemption_id="EXEMPT-16",
        title="无主 Algo 单清理仅在 testnet 执行",
        rationale="共享 demo 账户存在历史遗留无主条件单，testnet 下清理；live/canary 无此路径。",
        reassessment_module="M12",
        risk_note="清理依赖所有权判定证据链；M12 复核 unowned 判定与幽灵单取消的幂等性。",
    ),
    TestnetExemption(
        exemption_id="EXEMPT-17",
        title="风控视角：外部持仓按无持仓处理",
        rationale="共享账户外部持仓不参与风控判定（风控针对自有敞口），本地无所有权证明时"
        " position_qty=0、liq 价置 None；与 EXEMPT-02（覆盖判定）同根。",
        reassessment_module="M10",
        risk_note="风控敞口口径依赖所有权证据；live/canary 保持严格（无此豁免）。M10 复核。",
    ),
    TestnetExemption(
        exemption_id="EXEMPT-18",
        title="启动期交易所不可用长周期重试（最多 10 分钟）",
        rationale="demo 地域限制（-2015）间歇性出现，快速 FATAL 会让每次抖动杀死进程；"
        "testnet 长周期退避重试期间健康端点存活；live/canary 保持快速 FATAL。",
        reassessment_module="M22",
        risk_note="进程存活但功能不可用窗口最长 10 分钟；M22 复核与 launchd 重启语义的协同。",
    ),
)
