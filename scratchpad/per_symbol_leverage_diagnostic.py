"""只读诊断：分交易对杠杆的第一性原理分析（2026-09-26）。

在 Mac 的主 checkout 根目录运行（只读）。脚本不必 checkout 分支，取出来放到仓库外即可：

    cd ~/beidou && git fetch origin claude/adaptive-leverage-analysis-zvraep
    git show FETCH_HEAD:scratchpad/per_symbol_leverage_diagnostic.py > /tmp/per_symbol_leverage_diagnostic.py
    .venv/bin/python /tmp/per_symbol_leverage_diagnostic.py --out /tmp/lev_diag

读：.beidou/live/state.json、.beidou/live/cycles.jsonl、.beidou/data（研究面板）、config/*.yaml。
写：只写 --out 目录。不读凭据、不连交易所、不改仓库、不碰实盘循环、不写 trials ledger
（本脚本不调用任何 `research` 命令，只调用读取面板与生成信号的库函数）。

输出里不含任何绝对金额：持仓一律是权益的比例，保证金是权益的比例。

四个部分：
  A  实盘快照：每个持仓的目标权重（= 实际杠杆，名义/权益）、该币年化波动率、交易所杠杆设置、
     保证金占用；近 N 天保证金占用、强平距离、max_weight 截断的读数。
  B1 分波动率组的「单位风险收益」：每根 bar 按波动率把持仓名字分成三组，
     每个名字按 1/σ 持有（单位风险），比较三组的 Sharpe。回答「逆波动率是不是已经最优」。
  B2 分波动率组的标准化尾部：r / σ 的尾部分位数。回答「按 σ 定仓是否低估了高波动币的尾部」。
  B3 max_weight 在不同 k 下的截断频率（主账本，截断前权重对 k 线性，只算一次再缩放）。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

HOURS_PER_YEAR = 8760.0
LEVERAGE_TIERS = (1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20)


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------
def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _q(values: list[float] | np.ndarray, qs: tuple[float, ...] = (0.5, 0.95, 1.0)) -> dict[str, float] | None:
    arr = np.asarray([v for v in values if v is not None and math.isfinite(v)], dtype=float)
    if arr.size == 0:
        return None
    return {f"q{int(q * 100) if q * 100 == int(q * 100) else q * 100}": float(np.quantile(arr, q)) for q in qs} | {
        "n": int(arr.size),
        "mean": float(arr.mean()),
    }


def nearest_tier(value: float) -> int:
    if not math.isfinite(value) or value <= 0:
        return 1
    return min(LEVERAGE_TIERS, key=lambda tier: abs(math.log(tier) - math.log(value)))


def ols_slope(x: np.ndarray, y: np.ndarray) -> dict[str, float] | None:
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    if x.size < 3 or float(np.var(x)) == 0.0:
        return None
    slope, intercept = np.polyfit(x, y, 1)
    fitted = slope * x + intercept
    ss_res = float(((y - fitted) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    return {"slope": float(slope), "r2": 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan"), "n": int(x.size)}


def read_tail_jsonl(path: Path, max_rows: int) -> list[dict[str, Any]]:
    lines: deque[str] = deque(maxlen=max_rows)
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                lines.append(line)
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# ----------------------------------------------------------------------------------------------
# A  live snapshot
# ----------------------------------------------------------------------------------------------
def live_part(state_dir: Path, days: int, max_weight: float) -> dict[str, Any]:
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    leverage = {str(k): int(v) for k, v in (state.get("leverage_set") or {}).items() if _f(v) is not None}
    rows = read_tail_jsonl(state_dir / "cycles.jsonl", max_rows=24 * days * 4)
    usable = [
        row
        for row in rows
        if row.get("phase") != "ERROR"
        and isinstance(row.get("targets"), dict)
        and isinstance(row.get("asset_vol"), dict)
        and row.get("asset_vol")
    ]
    out: dict[str, Any] = {
        "state": {
            "restarted_at": state.get("restarted_at"),
            "restarts": state.get("restarts"),
            "leverage_symbols": len(leverage),
            "leverage_values": dict(Counter(leverage.values())),
        },
        "rows_read": len(rows),
        "rows_usable": len(usable),
    }
    if not usable:
        return out
    last_ms = max(int(_f(row.get("bar_open_ms")) or 0) for row in usable)
    window = [row for row in usable if int(_f(row.get("bar_open_ms")) or 0) >= last_ms - days * 86_400_000]
    latest = window[-1]

    # --- latest row: one line per held symbol -------------------------------------------------
    targets = {str(s): _f(w) for s, w in latest["targets"].items()}
    vols = {str(s): _f(v) for s, v in latest["asset_vol"].items()}
    held = {s: w for s, w in targets.items() if w is not None and abs(w) > 1e-6 and vols.get(s)}
    table = []
    for symbol, weight in sorted(held.items(), key=lambda kv: vols[kv[0]] or 0.0):
        sigma = float(vols[symbol] or 0.0)
        venue = leverage.get(symbol)
        table.append(
            {
                "symbol": symbol,
                "weight": round(weight, 5),
                "sigma_annual": round(sigma, 4),
                "sigma_daily": round(sigma / math.sqrt(365.0), 5),
                "venue_leverage": venue,
                "im_share": round(abs(weight) / venue, 5) if venue else None,
                "standalone_risk": round(abs(weight) * sigma, 5),
                # daily std of the ROE% the venue UI shows for this position, at today's setting
                "roe_daily_std_now": round(sigma / math.sqrt(365.0) * venue, 4) if venue else None,
            }
        )
    abs_w = np.array([abs(row["weight"]) for row in table])
    sig = np.array([row["sigma_annual"] for row in table])
    summary: dict[str, Any] = {"bar": latest.get("bar"), "n_held": len(table)}
    if len(table):
        mean_abs = float(abs_w.mean())
        median_sigma = float(np.median(sig))
        for row in table:
            equal_margin = 5.0 * abs(row["weight"]) / mean_abs
            by_sigma = 5.0 * median_sigma / row["sigma_annual"]
            row["lev_equal_margin"] = round(equal_margin, 2)
            row["lev_equal_margin_tier"] = nearest_tier(equal_margin)
            row["lev_by_sigma"] = round(by_sigma, 2)
            row["lev_by_sigma_tier"] = nearest_tier(by_sigma)
            row["roe_daily_std_by_sigma_tier"] = round(row["sigma_daily"] * row["lev_by_sigma_tier"], 4)
        summary |= {
            "gross": float(abs_w.sum()),
            "net": float(sum(row["weight"] for row in table)),
            "notional_max_over_min": float(abs_w.max() / abs_w.min()),
            "sigma_max_over_min": float(sig.max() / sig.min()),
            "standalone_risk_max_over_min": float((abs_w * sig).max() / (abs_w * sig).min()),
            "loglog_weight_on_sigma": ols_slope(np.log(sig), np.log(abs_w)),
            "im_total_now": float(sum(row["im_share"] or 0.0 for row in table)),
            "im_total_equal_margin_tiers": float(sum(abs(r["weight"]) / r["lev_equal_margin_tier"] for r in table)),
            "im_total_by_sigma_tiers": float(sum(abs(r["weight"]) / r["lev_by_sigma_tier"] for r in table)),
            "long_names": int(sum(1 for r in table if r["weight"] > 0)),
            "short_names": int(sum(1 for r in table if r["weight"] < 0)),
        }
    out["latest"] = {"summary": summary, "positions": table}
    collateral = latest.get("collateral")
    if isinstance(collateral, dict):
        out["latest"]["collateral_share"] = _f(collateral.get("share"))
    book_vol = latest.get("book_vol")
    if isinstance(book_vol, dict):
        out["latest"]["book_vol"] = {k: _f(book_vol.get(k)) for k in ("target", "ex_ante", "clipped_risk_share")}
    out["latest"]["construction"] = latest.get("construction")

    # --- the window ---------------------------------------------------------------------------
    margin_usage, gross, clipped, ex_ante, liq_min = [], [], [], [], []
    liq_measured, liq_unreachable = [], []
    scaled = 0
    capped_rows = 0
    capped_symbols: Counter[str] = Counter()
    per_symbol: dict[str, dict[str, float]] = {}
    pooled_x, pooled_y, pooled_x_uncapped, pooled_y_uncapped = [], [], [], []
    for row in window:
        mu = _f(row.get("margin_usage"))
        if mu is not None:
            margin_usage.append(mu)
        tw = {str(s): _f(w) for s, w in (row.get("targets") or {}).items()}
        av = {str(s): _f(v) for s, v in (row.get("asset_vol") or {}).items()}
        held_row = {s: w for s, w in tw.items() if w is not None and abs(w) > 1e-6}
        gross.append(sum(abs(w) for w in held_row.values()))
        if isinstance(row.get("margin"), dict) and row["margin"].get("scaled") is True:
            scaled += 1
        bv = row.get("book_vol")
        if isinstance(bv, dict):
            if _f(bv.get("clipped_risk_share")) is not None:
                clipped.append(_f(bv.get("clipped_risk_share")))
            if _f(bv.get("ex_ante")) is not None:
                ex_ante.append(_f(bv.get("ex_ante")))
        lv = row.get("min_liq_distance")
        if isinstance(lv, dict):
            if _f(lv.get("min_distance")) is not None:
                liq_min.append(_f(lv.get("min_distance")))
            if _f(lv.get("measured")) is not None:
                liq_measured.append(_f(lv.get("measured")))
            if _f(lv.get("unreachable")) is not None:
                liq_unreachable.append(_f(lv.get("unreachable")))
        row_capped = False
        for symbol, weight in held_row.items():
            is_capped = abs(weight) >= max_weight - 1e-6
            if is_capped:
                row_capped = True
                capped_symbols[symbol] += 1
            sigma = av.get(symbol)
            if sigma:
                slot = per_symbol.setdefault(symbol, {"n": 0, "sum_abs_w": 0.0, "sum_sigma": 0.0})
                slot["n"] += 1
                slot["sum_abs_w"] += abs(weight)
                slot["sum_sigma"] += sigma
                pooled_x.append(math.log(sigma))
                pooled_y.append(math.log(abs(weight)))
                if not is_capped:
                    pooled_x_uncapped.append(math.log(sigma))
                    pooled_y_uncapped.append(math.log(abs(weight)))
        capped_rows += int(row_capped)
    symbols_table = sorted(
        (
            {
                "symbol": s,
                "cycles_held": v["n"],
                "mean_abs_weight": round(v["sum_abs_w"] / v["n"], 5),
                "mean_sigma": round(v["sum_sigma"] / v["n"], 4),
            }
            for s, v in per_symbol.items()
        ),
        key=lambda r: r["mean_sigma"],
    )
    out["window"] = {
        "days": days,
        "rows": len(window),
        "first_bar": window[0].get("bar"),
        "last_bar": latest.get("bar"),
        "margin_usage": _q(margin_usage),
        "gross": _q(gross),
        "margin_scaled_rows": scaled,
        "clipped_risk_share": _q(clipped),
        "book_vol_ex_ante": _q(ex_ante),
        "liq_min_distance_daily_sigma": _q(liq_min, (0.0, 0.5)),
        "liq_measured_positions": _q(liq_measured, (0.5,)),
        "liq_unreachable_positions": _q(liq_unreachable, (0.5,)),
        "rows_with_a_name_at_max_weight": capped_rows,
        "names_at_max_weight": dict(capped_symbols.most_common(10)),
        "pooled_loglog_weight_on_sigma": ols_slope(np.array(pooled_x), np.array(pooled_y)),
        "pooled_loglog_uncapped": ols_slope(np.array(pooled_x_uncapped), np.array(pooled_y_uncapped)),
        "per_symbol": symbols_table,
    }
    return out


# ----------------------------------------------------------------------------------------------
# B  research panel (pure functions first, so they can be tested on synthetic frames)
# ----------------------------------------------------------------------------------------------
def vol_buckets(sigma: pd.DataFrame, mask: pd.DataFrame, n_buckets: int = 3) -> pd.DataFrame:
    """Cross-sectional sigma tercile (1 = calmest) among the names ``mask`` admits, per bar."""
    ranked = sigma.where(mask).rank(axis=1, pct=True)
    return np.ceil(ranked * n_buckets).clip(1, n_buckets)


def _sharpe(series: pd.Series) -> float:
    values = series.to_numpy(dtype=float)
    values = values[np.isfinite(values)]
    if values.size < 2 or values.std(ddof=1) == 0:
        return float("nan")
    return float(values.mean() / values.std(ddof=1) * math.sqrt(HOURS_PER_YEAR))


def block_bootstrap_diff(a: pd.Series, b: pd.Series, *, block: int, draws: int, seed: int) -> dict[str, float]:
    """Paired weekly-block bootstrap of Sharpe(a) - Sharpe(b)."""
    x = a.to_numpy(dtype=float)
    y = b.to_numpy(dtype=float)
    n = min(x.size, y.size)
    x, y = np.nan_to_num(x[:n]), np.nan_to_num(y[:n])
    n_blocks = max(1, n // block)
    starts_max = max(1, n - block)
    rng = np.random.default_rng(seed)
    diffs = np.empty(draws)
    for i in range(draws):
        starts = rng.integers(0, starts_max, size=n_blocks)
        idx = (starts[:, None] + np.arange(block)[None, :]).ravel()
        xs, ys = x[idx], y[idx]
        sx = xs.mean() / xs.std(ddof=1) if xs.std(ddof=1) > 0 else 0.0
        sy = ys.mean() / ys.std(ddof=1) if ys.std(ddof=1) > 0 else 0.0
        diffs[i] = (sx - sy) * math.sqrt(HOURS_PER_YEAR)
    return {
        "point": _sharpe(pd.Series(x)) - _sharpe(pd.Series(y)),
        "q05": float(np.quantile(diffs, 0.05)),
        "q50": float(np.quantile(diffs, 0.50)),
        "q95": float(np.quantile(diffs, 0.95)),
        "p_le_0": float((diffs <= 0).mean()),
    }


def edge_by_bucket(
    signal: pd.DataFrame,
    sigma: pd.DataFrame,
    close: pd.DataFrame,
    *,
    n_buckets: int = 3,
    cost_bps: float = 7.0,
    block: int = 168,
    draws: int = 2000,
    seed: int = 20260926,
) -> dict[str, Any]:
    """Unit-risk P&L of the signal, split by the name's sigma tercile at decision time.

    ``u = s / sigma`` is a position whose forecast annualised vol is 1 (stage 1 without the book
    scalar), so the three buckets are compared per unit of risk: if their Sharpes agree, inverse-vol
    sizing (equal risk per name) is already the growth-optimal split; if the calm bucket earns more
    per unit risk, the first-principles answer is to tilt MORE notional toward it than 1/sigma does.
    Decision at the close of bar t earns close(t)->close(t+1).  Costs: |du| x cost_bps, charged to
    the bucket the name sat in when the position was held (closing trades to the previous bucket).
    """
    r_next = close.pct_change(fill_method=None).shift(-1)
    s = signal.reindex(index=close.index, columns=close.columns)
    held = s.where(s != 0)
    valid = held.notna() & sigma.notna() & (sigma > 0) & r_next.notna()
    bucket = vol_buckets(sigma, valid, n_buckets)
    u = (held / sigma).where(valid)
    u_all = (s / sigma).where(s.notna() & sigma.notna() & (sigma > 0)).fillna(0.0)
    turnover = u_all.diff().abs()
    turnover.iloc[0] = u_all.iloc[0].abs()
    cost_bucket = bucket.where(bucket.notna(), bucket.shift(1))
    gross = u * r_next
    cost = turnover * cost_bps / 1e4
    out: dict[str, Any] = {"cost_bps": cost_bps, "buckets": {}}
    series: dict[int, dict[str, pd.Series]] = {}
    for b in range(1, n_buckets + 1):
        in_b = bucket == b
        count = in_b.sum(axis=1)
        gross_b = gross.where(in_b).sum(axis=1)
        cost_b = cost.where(cost_bucket == b).sum(axis=1)
        denom = count.where(count > 0)
        # a closing trade lands on a bar where the name is no longer in the bucket, so its cost is
        # spread over the bucket's previous head-count rather than dropped on an empty bar
        denom_cost = denom.fillna(count.shift(1).where(count.shift(1) > 0))
        port_gross = (gross_b / denom).fillna(0.0)
        port_net = port_gross - (cost_b / denom_cost).fillna(0.0)
        pooled = gross.where(in_b).stack().dropna()
        pooled_net_sum = float(gross_b.sum() - cost_b.sum())
        n_obs = int(in_b.sum().sum())
        per_year = {}
        for year, chunk in port_gross.groupby(port_gross.index.year):
            per_year[str(year)] = round(_sharpe(chunk), 3)
        series[b] = {"gross": port_gross, "net": port_net}
        out["buckets"][str(b)] = {
            "median_sigma": float(sigma.where(in_b).stack().dropna().median()) if n_obs else None,
            "names_per_bar": float(count[count > 0].mean()) if n_obs else None,
            "position_bars": n_obs,
            "portfolio_sharpe_gross": _sharpe(port_gross),
            "portfolio_sharpe_net": _sharpe(port_net),
            # per position, per unit of risk: mean/std of u*r over all (name, bar) observations
            "position_ir_gross": float(pooled.mean() / pooled.std(ddof=1) * math.sqrt(HOURS_PER_YEAR))
            if pooled.size > 1 and float(pooled.std(ddof=1)) > 0
            else None,
            "position_ir_net_mean_over_gross_std": float(
                (pooled_net_sum / n_obs) / pooled.std(ddof=1) * math.sqrt(HOURS_PER_YEAR)
            )
            if pooled.size > 1 and n_obs and float(pooled.std(ddof=1)) > 0
            else None,
            # realised annualised vol of a unit-risk position: 1.0 means sigma forecast its risk exactly
            "realised_over_forecast_vol": float(math.sqrt(float((pooled**2).mean()) * HOURS_PER_YEAR))
            if pooled.size
            else None,
            "long_share": float((held.where(in_b) > 0).sum().sum() / n_obs) if n_obs else None,
            "sharpe_by_year_gross": per_year,
        }
    if n_buckets >= 2:
        low, high = series[1], series[n_buckets]
        out["low_minus_high_gross"] = block_bootstrap_diff(low["gross"], high["gross"], block=block, draws=draws, seed=seed)
        out["low_minus_high_net"] = block_bootstrap_diff(low["net"], high["net"], block=block, draws=draws, seed=seed)
    return out


def tails_by_bucket(sigma: pd.DataFrame, close: pd.DataFrame, mask: pd.DataFrame, n_buckets: int = 3) -> dict[str, Any]:
    """Standardised next-bar and next-day moves, r / sigma, split by sigma tercile at decision time."""
    r_next = close.pct_change(fill_method=None).shift(-1)
    valid = mask & sigma.notna() & (sigma > 0) & r_next.notna()
    bucket = vol_buckets(sigma, valid, n_buckets)
    z_hour = r_next / (sigma / math.sqrt(HOURS_PER_YEAR))
    r_day = close.pct_change(24, fill_method=None).shift(-24)
    midnight = np.broadcast_to(np.asarray(close.index.hour == 0)[:, None], valid.shape)
    valid_day = valid & r_day.notna() & midnight
    z_day = r_day / (sigma * math.sqrt(24.0 / HOURS_PER_YEAR))
    out: dict[str, Any] = {}
    for b in range(1, n_buckets + 1):
        zh = z_hour.where(valid & (bucket == b)).stack().dropna().to_numpy(dtype=float)
        zd = z_day.where(valid_day & (bucket == b)).stack().dropna().to_numpy(dtype=float)
        rd = r_day.where(valid_day & (bucket == b)).stack().dropna().to_numpy(dtype=float)
        entry: dict[str, Any] = {"hour_n": int(zh.size), "day_n": int(zd.size)}
        if zh.size:
            entry["hour_z"] = {
                "q0001": float(np.quantile(zh, 0.0001)),
                "q001": float(np.quantile(zh, 0.001)),
                "q999": float(np.quantile(zh, 0.999)),
                "q9999": float(np.quantile(zh, 0.9999)),
                "p_abs_gt5": float((np.abs(zh) > 5).mean()),
                "p_abs_gt8": float((np.abs(zh) > 8).mean()),
                "std": float(zh.std()),
                "excess_kurtosis": float(pd.Series(zh).kurt()),
                "min": float(zh.min()),
                "max": float(zh.max()),
            }
        if zd.size:
            entry["day_z"] = {
                "q001": float(np.quantile(zd, 0.001)),
                "q01": float(np.quantile(zd, 0.01)),
                "q99": float(np.quantile(zd, 0.99)),
                "q999": float(np.quantile(zd, 0.999)),
                "p_abs_gt5": float((np.abs(zd) > 5).mean()),
                "std": float(zd.std()),
                "min": float(zd.min()),
                "max": float(zd.max()),
            }
            entry["day_return"] = {
                "q001": float(np.quantile(rd, 0.001)),
                "q01": float(np.quantile(rd, 0.01)),
                "q99": float(np.quantile(rd, 0.99)),
                "q999": float(np.quantile(rd, 0.999)),
                "min": float(rd.min()),
                "max": float(rd.max()),
            }
            entry["median_sigma"] = float(sigma.where(valid_day & (bucket == b)).stack().dropna().median())
        out[str(b)] = entry
    return out


def cap_binding(pre_cap: pd.DataFrame, k_ref: float, ks: list[float], max_weight: float) -> dict[str, Any]:
    """How often the uniform notional cap binds, at several book scales.

    Stages 1-2 are linear in ``vol_target`` (stage 2's scalar is vol_target / portfolio_vol(stage1),
    and stage 1 is itself proportional to vol_target, so the scalar does not depend on it), so the
    pre-cap weights at k are ``pre_cap * k / k_ref`` exactly - one covariance pass serves every k.
    """
    active = pre_cap.abs().sum(axis=1) > 0
    out: dict[str, Any] = {}
    for k in ks:
        w = pre_cap.loc[active] * (k / k_ref)
        over = w.abs() > max_weight
        rows = over.any(axis=1)
        requested = w.abs().sum(axis=1)
        kept = w.clip(-max_weight, max_weight).abs().sum(axis=1)
        clipped = (1.0 - kept / requested.where(requested > 0)).fillna(0.0)
        recent = w.index >= (w.index.max() - pd.Timedelta(days=365))
        capped = w.clip(-max_weight, max_weight).abs()
        held = capped > 1e-9
        ratio = (capped.where(held).max(axis=1) / capped.where(held).min(axis=1)).replace([np.inf], np.nan)
        out[str(k)] = {
            "bars": int(rows.size),
            "share_bars_capped": float(rows.mean()),
            "share_bars_capped_last_365d": float(rows[recent].mean()) if recent.any() else None,
            "clipped_share_mean": float(clipped.mean()),
            "clipped_share_q95": float(clipped.quantile(0.95)),
            "names_capped_most": dict(Counter(over.sum(axis=0)[over.sum(axis=0) > 0].to_dict()).most_common(8)),
            "gross_median": float(w.clip(-max_weight, max_weight).abs().sum(axis=1).median()),
            "notional_max_over_min_median": float(ratio.median()),
        }
    return out


def research_part(args: argparse.Namespace) -> dict[str, Any]:
    from beidou_alpha.portfolio import asset_vol, vol_targeted
    from beidou_cli.research_panel import _load, _membership, _resolve_symbols
    from beidou_live.composition import build_model, load_registry
    from beidou_shared.config import load_yaml

    started = time.time()
    profile = load_yaml(args.profile)
    registry = load_registry(args.registry)
    model = build_model(registry, profile)
    symbols = _resolve_symbols(args.root, "", "1h", "pit")
    panel = _load(args.root, symbols, "1h", args.start, args.end, True)
    membership = _membership(args.root, "pit", panel, 0)
    per_strategy = model.strategy_targets(panel, membership)
    sigma = asset_vol(panel.close, model.portfolio, panel.bars_per_year)
    eligible = model.eligible(panel, membership)
    out: dict[str, Any] = {
        "panel": {
            "symbols": len(panel.symbols),
            "bars": len(panel.index),
            "start": str(panel.index[0]),
            "end": str(panel.index[-1]),
            "vol_target_in_profile": model.portfolio.vol_target,
            "max_weight": model.portfolio.max_weight,
        },
        "edge": {},
    }
    print(f"[research] panel loaded in {time.time() - started:.0f}s", flush=True)

    def section(name: str, compute: Any) -> Any:
        try:
            value = compute()
        except Exception as exc:  # one section failing must not lose the others
            import traceback

            value = {"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-2000:]}
        print(f"[research] {name} done at {time.time() - started:.0f}s", flush=True)
        return value

    for strategy_id, frame in per_strategy.items():
        out["edge"][strategy_id] = section(
            f"edge {strategy_id}", lambda frame=frame: edge_by_bucket(frame, sigma, panel.close, draws=args.draws)
        )
    mask = eligible.reindex(index=sigma.index, columns=sigma.columns).fillna(False).astype(bool)
    out["tails"] = section("tails", lambda: tails_by_bucket(sigma, panel.close, mask))

    def binding() -> dict[str, Any]:
        book_conviction = model.book_targets(per_strategy)
        pre_cap = vol_targeted(book_conviction[model.book_names[0]], panel.close, panel.bars_per_year, model.portfolio)
        return cap_binding(pre_cap, model.portfolio.vol_target, [0.175, 0.30, 0.45, 0.60], model.portfolio.max_weight)

    out["cap_binding_main_book"] = section("cap binding", binding)

    def live_tails() -> dict[str, Any]:
        positions = getattr(args, "live_positions", None) or []
        close = panel.close
        last = close.index.max()
        year = close.loc[close.index >= last - pd.Timedelta(days=365)]
        day = year.pct_change(24, fill_method=None)
        rows = []
        for position in positions:
            symbol, weight = position["symbol"], float(position["weight"])
            if symbol not in day.columns:
                rows.append({"symbol": symbol, "missing": True})
                continue
            moves = day[symbol].dropna()
            if moves.empty:
                rows.append({"symbol": symbol, "missing": True})
                continue
            adverse = moves if weight < 0 else -moves  # a loss for this side is a positive number
            worst = float(adverse.max())
            q99 = float(adverse.quantile(0.99))
            rows.append(
                {
                    "symbol": symbol,
                    "weight": weight,
                    "sigma_annual": position.get("sigma_annual"),
                    "worst_24h_adverse_move": round(worst, 4),
                    "q99_24h_adverse_move": round(q99, 4),
                    "equity_loss_if_worst_repeats": round(abs(weight) * worst, 5),
                    "equity_loss_at_q99": round(abs(weight) * q99, 5),
                    "worst_in_daily_sigma": round(worst / (float(position.get("sigma_annual") or 0) / math.sqrt(365.0)), 2)
                    if position.get("sigma_annual")
                    else None,
                }
            )
        return {"window_start": str(year.index.min()), "window_end": str(last), "positions": rows}

    out["live_position_tails"] = section("live position tails", live_tails)
    out["seconds"] = round(time.time() - started, 1)
    return out


# ----------------------------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--state-dir", default=".beidou/live")
    parser.add_argument("--root", default=".beidou/data")
    parser.add_argument("--profile", default="config/live.demo.yaml")
    parser.add_argument("--registry", default="config/alpha_registry.yaml")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--draws", type=int, default=2000)
    parser.add_argument("--skip-live", action="store_true")
    parser.add_argument("--skip-research", action="store_true")
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"generated_at": pd.Timestamp.now(tz="UTC").isoformat()}
    try:
        import subprocess

        result["git_head"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
        ).stdout.strip()
    except Exception:  # provenance is best effort
        result["git_head"] = None
    result["versions"] = {"python": sys.version.split()[0], "pandas": pd.__version__, "numpy": np.__version__}
    if not args.skip_live:
        from beidou_shared.config import load_yaml

        max_weight = float((load_yaml(args.profile).get("portfolio") or {}).get("max_weight", 0.15))
        try:
            result["live"] = live_part(Path(args.state_dir), args.days, max_weight)
            args.live_positions = (result["live"].get("latest") or {}).get("positions") or []
        except Exception as exc:  # report, keep going
            result["live"] = {"error": f"{type(exc).__name__}: {exc}"}
        (out_dir / "live.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        print("[live] written", flush=True)
    if not args.skip_research:
        try:
            result["research"] = research_part(args)
        except Exception as exc:
            import traceback

            result["research"] = {"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-3000:]}
    (out_dir / "lev_diag.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    print(json.dumps(result, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
