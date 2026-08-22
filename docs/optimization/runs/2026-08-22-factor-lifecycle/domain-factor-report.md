# 因子生命周期研究与桥接报告

日期：2026-08-22

范围：本地历史数据、`factor_miner` 正式研究、`EvidenceBridge` 离线复验；未执行交易所写操作。

## 结论

研究→证据→桥接链路可以正常运行。报告编写时，未加载新批次的运行实例仍为旧 commit `f0277adc66585431c9667700318be9fa30e0177d`，因此运行态显示 1 个 `ACTIVE`、1 个 `CHALLENGER`，核心 8 个因子为 `IDEA`；这不是新批次重启后的状态结论。

## 正式研究批次

- 数据源：本地 KlineStore，`BNBUSDT/BTCUSDT/ETHUSDT/SOLUSDT`，1h，约两年；四个数据集的运行时计算 manifest hash 均与对应 `.manifest.json` 一致。
- 运行入口：`python -m apps.factor_miner run --from-store --jobs 4`。
- 产物目录：`evidence/factors/research_runs/2026-08-22-local-4symbols-1h/`。
- 产物：233 份；`PASS=108`、`FAIL=92`、`NOT_VERIFIABLE=33`。
- 含完整晋级链的产物终态：`PAPER_TRADING=84`、`CHALLENGER=7`、`ACTIVE=17`；125 份没有完整链，保持拒绝。
- 对整个证据目录执行严格桥接复验：应用 52 个因子，生命周期为 `ACTIVE=15`、`CHALLENGER=6`、`PAPER_TRADING=31`；核心 8 个仍为 `IDEA`。

旧证据未覆盖或重签：旧的 171 份 `direction_flipped` 未纳入哈希的文件仍以 `artifact_hash_mismatch` 拒绝；旧文件及本批次失败文件均保留。

## 根因与修复

1. 核心 8 个 ID 没有对应的正式 EvidenceBundle，现有有效证据是 `tmpl_*` 挖掘因子，不能安全映射为核心实现；因此核心因子停在 `IDEA` 是门禁的预期结果，不能通过改状态解决。
2. macOS/Python 3.14 的 `spawn` 在 `python -m apps.factor_miner` 下无法反序列化 `__main__._run_symbol_worker`，导致多品种正式研究失败。已将 worker 移到可导入的 `apps.factor_miner.worker`，并补充回归测试。

## 验证

- `tests/unit/test_factor_miner_cli.py`：10 passed。
- EvidenceBridge、promotion chain、动态 registry：19 passed。
- 因子研究与挖矿边界：45 passed。
- Ruff、`git diff --check`、独立写能力 registry oracle：PASS。
- 四品种真实 CLI 并行研究：完成；BTC 本批无候选通过门禁，未生成可晋级证据。

## 未完成边界

报告编写时新代码和新证据尚未绑定到运行实例；下一步是提交/G5 重新绑定/受控重启，并复核因子数量、Alpha DAG、对账和下单流程。
