# 北斗 (Beidou) V5 — Alpha-First

加密永续合约量化交易系统（Binance USDⓈ-M，demo/testnet 阶段）。

V5 是对 V2/V4 治理时代代码库的绞杀式重建：`git tag v2-governance-final` 保留了旧系统的全部历史。
新系统只有七个包和一条架构规则（依赖方向），90% 的精力投入 alpha（策略与因子）。

```
beidou_alpha     特征 → 信号 → 集成 → 组合 → 回测/验证   （纯函数，只依赖 numpy/pandas）
beidou_data      官方公共数据下载/校验/存储、universe、实盘闭合 K 线
beidou_exchange  Binance USDⓈ-M REST 适配器 + host allowlist + kill-switch
beidou_governance 证据 → 判定 → 晋级：预登记、试验账本、家族门、事务化写 registry
beidou_live      bar 驱动实盘循环：目标权重 → 幂等再平衡 → 对账 → 归因/日报
beidou_shared    值类型与 YAML/env 配置加载（Side、InstrumentRules、Position、OrderRequest）
beidou_cli       beidou data | research | governance | live | report
```

上面这段 2026-09-16 更正过：原文写「五个包」并只列了其中五个，而 `pyproject.toml` 自
`beidou_governance` 落地起就打包七个。少列的两个不是小配角——`beidou_governance` 是整条
晋级线，`beidou_shared` 是另外六个包的公共类型层。

- 架构：`docs/ARCHITECTURE.md`
- 运行：`docs/RUNBOOK.md`
- 重构方案与证据账本：`~/.claude/plans/nifty-gliding-petal.md`（deep-analysis L 级）

```bash
python3.12 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]" && pytest -q
```

Proprietary — 保留所有权利。

例外：`.claude/skills/backtest-guard/` 是两个第三方 MIT 项目的合并版（回测工程审查 +
策略逻辑对抗审查），按 MIT 保留原始许可证于该目录内，不适用上面这行。它是审查这个
仓库时用的那把尺子——`docs/analysis/2026-09-05-backtest-guard-external-audit.md` 是它的产出。
