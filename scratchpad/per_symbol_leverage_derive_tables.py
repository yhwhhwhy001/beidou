"""从 `per_symbol_leverage_diagnostic.py` 的输出推出 2026-09-26 分交易对杠杆分析的派生表。

报告附录 A、E-022、E-025 的每个数都由本脚本算出，不含心算格。只读输入文件，只打印到标准输出。

用法：.venv/bin/python scratchpad/per_symbol_leverage_derive_tables.py /tmp/lev_diag/lev_diag.json
"""
import json
import math
import sys

import numpy as np

K_NOW, K_NEXT = 0.60, 0.175  # 实盘现在的 vol_target；#163 合入后的 vol_target
with open(sys.argv[1], encoding="utf-8") as handle:
    d = json.load(handle)
pos = d["live"]["latest"]["positions"]

# stage 1-2 对 vol_target 线性（portfolio.py 模块说明与 cap_binding 的推导），所以 k=0.175 下
# 未截断名字的单名风险 = 现在的单名风险 x 0.175/0.60；现在被 0.15 截断的 BTC/BNB 在 0.175 下不再触顶。
r_now = max(p["standalone_risk"] for p in pos)
r_next = r_now * K_NEXT / K_NOW
print(f"单名风险（年化，占权益）：k=0.60 {r_now:.5f}；k=0.175 {r_next:.5f}")
rows = []
for p in pos:
    w_next = min(r_next / p["sigma_annual"], 0.15)
    tier = p["lev_by_sigma_tier"]
    rows.append(
        {
            "symbol": p["symbol"].replace("USDT", ""),
            "sigma": p["sigma_annual"],
            "w_now": p["weight"],
            "w_next": w_next,
            "im_now_5x_next": w_next / 5,
            "tier": tier,
            "im_tier_next": w_next / tier,
            "roe_now": p["roe_daily_std_now"],
            "roe_tier": p["roe_daily_std_by_sigma_tier"],
        }
    )
g_next = sum(r["w_next"] for r in rows)
print(f"k=0.175 推算：gross {g_next:.3f}；5x 下初始保证金 {g_next / 5:.4f}；分档下 {sum(r['im_tier_next'] for r in rows):.4f}")
print()
print("| 交易对 | σ（年化） | 真实杠杆 现在 k=0.60 | 真实杠杆 k=0.175（推算） | 交易所杠杆 现在 → 分档 | 保证金/权益 k=0.175：5x → 分档 | 界面 ROE% 日波动：5x → 分档 |")
print("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
for r in rows:
    print(
        f"| {r['symbol']} | {r['sigma']:.2f} | {r['w_now']:.3f} | {r['w_next']:.4f} | 5x → {r['tier']}x | "
        f"{r['im_now_5x_next']:.2%} → {r['im_tier_next']:.2%} | {r['roe_now']:.0%} → {r['roe_tier']:.0%} |"
    )
lo = min(r["im_tier_next"] for r in rows)
hi = max(r["im_tier_next"] for r in rows)
print(f"\n分档后每个持仓的保证金在 {lo:.2%}–{hi:.2%} 之间；5x 下在 {min(r['im_now_5x_next'] for r in rows):.2%}–{max(r['im_now_5x_next'] for r in rows):.2%}")
print(f"分档后界面 ROE% 日波动在 {min(r['roe_tier'] for r in rows):.0%}–{max(r['roe_tier'] for r in rows):.0%}；5x 下 {min(r['roe_now'] for r in rows):.0%}–{max(r['roe_now'] for r in rows):.0%}")

# E-022：若分组 Sharpe 差是真的，最优只做多风险配比能把全书 Sharpe 提高多少（组间相关 rho 未测，列几档）
print("\nE-022（分组 Sharpe 取自 research.edge.tsmom.buckets.*.portfolio_sharpe_*）")
for label, key in (("毛", "portfolio_sharpe_gross"), ("净", "portfolio_sharpe_net")):
    S = np.array([d["research"]["edge"]["tsmom"]["buckets"][b][key] for b in "123"])
    for rho in (0.0, 0.3, 0.5, 0.7, 0.8):
        C = np.full((3, 3), rho)
        np.fill_diagonal(C, 1.0)
        w_eq = np.ones(3) / 3
        s_eq = w_eq @ S / math.sqrt(w_eq @ C @ w_eq)
        best, best_w = 0.0, None
        for a in np.linspace(0, 1, 101):
            for b in np.linspace(0, 1 - a, round((1 - a) * 100) + 1):
                w = np.array([a, b, 1 - a - b])
                v = w @ C @ w
                if v > 0 and w @ S / math.sqrt(v) > best:
                    best, best_w = w @ S / math.sqrt(v), w
        print(f"  {label} rho={rho}: 等风险 {s_eq:.3f} → 最优只做多 {best:.3f}（{best / s_eq - 1:+.1%}），风险份额 低/中/高 = {np.round(best_w, 2).tolist()}")

# E-016 的合计，按 k 线性缩放到 0.175（上界：假设 17 个名字各自最差日同时发生）
tails = d["research"]["live_position_tails"]["positions"]
worst = sum(p["equity_loss_if_worst_repeats"] for p in tails)
print(f"\nE-016：17 个持仓各自最差 24h 同时发生的合计 k=0.60 {worst:.3f}；按 k 线性缩放到 0.175 约 {worst * K_NEXT / K_NOW:.3f}")
