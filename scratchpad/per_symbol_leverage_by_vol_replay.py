"""`leverage: by_vol` 上线前的两条回放：T-6（订单与 `auto` 逐单一致、保证金预检不缩单）与 T-11（带迟滞每天换档几次）。

分交易对杠杆报告（`docs/analysis/2026-09-26-per-symbol-leverage-first-principles.md`）§9.4 的这两条要 Mac 上的
`cycles.jsonl`，所以写成脚本在 Mac 上只读地跑：不连交易所、不碰循环、不写任何文件，只往终端打印。

    PYTHONPATH=<含 by_vol 代码的目录> .venv/bin/python <那个目录>/scratchpad/per_symbol_leverage_by_vol_replay.py \\
        --cycles .beidou/live/cycles.jsonl --days 14

记录里的周期都是在 `auto`（每个币 5x）下跑的。回放把同一串周期按 `by_vol` 再走一遍：σ 读每行的 `asset_vol`，
目标名义 = `targets` × `equity`，分档的记忆（已分档、迟滞计数）一行一行带下去，与循环相同；第一行之前每个币按
`auto` 的 5x 起步，与切换那次启动相同。档位表（交易所按名义的上限）不在记录里，回放不压上限：在 k=0.175 的
名义上它压不到 15x 以下，压到了也只会让档位更低、保证金更多，所以不压是更严的那一边。

T-6 比的只有保证金预检，因为订单在读杠杆之前就定了。每行拿记录下的订单与可用余额（`margin.budget` ÷
(1 − margin_buffer)），分别在 5x 与分档下跑 `scale_orders_to_margin`。分档下的可用余额按持仓在两种杠杆下的
保证金差额修正；持仓取该行订单的 `current_notional`，没有订单的币用上一行的目标名义近似。这是近似，不是交易所
的读数，所以按报告 T-6 只防回归，不证明安全（K-06）。

T-11 数的是换档：一个周期里发出的值与原设置不同的币，按 UTC 日加总。不含 S4 的每日全量重发（按设计每天每个币
一次，每次权重 1），也不含第一个分档周期（从 5x 一次换到各自的档位，只发生一次）。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from beidou_alpha.registry import parse_registry
from beidou_live.config import live_config
from beidou_live.engine import LiveConfig
from beidou_live.leverage import derive_leverage, plan_leverage, scale_orders_to_margin
from beidou_live.rebalancer import PlannedOrder
from beidou_shared.types import Side

ROOT = Path(__file__).resolve().parents[1]
DAY_MS = 86_400_000


def load_config(profile: Path) -> LiveConfig:
    registry = parse_registry(yaml.safe_load((ROOT / "config" / "alpha_registry.yaml").read_text(encoding="utf-8")))
    return live_config(yaml.safe_load(profile.read_text(encoding="utf-8")), ["BTCUSDT"], registry, dry_run=True)


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def _orders(row: dict[str, Any]) -> list[PlannedOrder]:
    return [
        PlannedOrder(
            symbol=str(order["symbol"]),
            side=Side(order["side"]),
            quantity=Decimal(str(order["quantity"])),
            reduce_only=bool(order.get("reduce_only")),
            client_order_id=str(order.get("client_order_id", "")),
            target_weight=float(order.get("target_weight") or 0.0),
            current_notional=float(order.get("current_notional") or 0.0),
            target_notional=float(order.get("target_notional") or 0.0),
            price=float(order.get("price") or 0.0),
            note=str(order.get("note") or ""),
        )
        for order in row.get("orders") or []
    ]


def replay(rows: list[dict[str, Any]], config: LiveConfig) -> dict[str, Any]:
    """Walk the rows under `by_vol` the way the loop would, and compare each pre-check with `auto`'s."""
    base = derive_leverage(config.guards.max_gross, config.margin_cap, config.max_leverage)
    standing: dict[str, int] = {}
    tiered: set[str] = set()
    streaks: dict[str, int] = {}
    previous: dict[str, float] = {}
    changes: Counter[str] = Counter()
    first_switch: dict[str, int] | None = None
    out: dict[str, Any] = {
        "cycles": 0,
        "with_orders": 0,
        "auto_scaled": [],
        "by_vol_scaled": [],
        "orders_differ": [],
        "raised": 0,
        "tiered_margin_max": 0.0,
        "auto_margin_max": 0.0,
        "headroom_min": {"auto": None, "by_vol": None},
    }
    for row in rows:
        # A normal row carries no `phase` (the heartbeat does); ERROR and SKIPPED rows do, and neither re-tiers.
        if row.get("phase") not in (None, "OK") or row.get("skip") or not row.get("targets"):
            continue
        equity = float(row.get("equity") or 0.0)
        if equity <= 0:
            continue
        bar = int(row["bar_open_ms"])
        targets = {symbol: float(weight) for symbol, weight in row["targets"].items()}
        managed = sorted(set(row.get("universe") or []) | set(row.get("leaving") or []) | set(targets))
        orders = _orders(row)
        positions = {symbol: previous.get(symbol, 0.0) * equity for symbol in managed}
        positions.update({order.symbol: order.current_notional for order in orders})
        for symbol in managed:
            standing.setdefault(symbol, base)  # the switch's startup sends `auto`'s value first
        before = dict(standing)
        plan = plan_leverage(
            managed=managed,
            sigma={symbol: float(value) for symbol, value in (row.get("asset_vol") or {}).items()},
            standing=standing,
            tiered=tiered,
            streaks=streaks,
            target_notional={symbol: targets.get(symbol, 0.0) * equity for symbol in managed},
            position_notional=positions,
            auto=dict.fromkeys(managed, base),
            tables={},
            base=base,
            sigma_ref=config.leverage_sigma_ref,
            tiers=config.leverage_tiers,
            cycles=config.leverage_hysteresis,
        )
        moved = {symbol: value for symbol, value in plan.wanted.items() if before.get(symbol) != value}
        if first_switch is None:
            first_switch = moved
        else:
            changes[_day(bar)] += len(moved)
        standing.update(plan.wanted)
        out["cycles"] += 1
        out["raised"] += bool(plan.raised)
        out["tiered_margin_max"] = max(out["tiered_margin_max"], plan.margin["tiered"] / equity)
        out["auto_margin_max"] = max(out["auto_margin_max"], plan.margin["auto"] / equity)
        margin = row.get("margin") or {}
        if orders and "budget" in margin:
            out["with_orders"] += 1
            available = float(margin["budget"]) / (1.0 - config.margin_buffer)
            # The snapshot the loop reads was taken before this cycle's sends, so the standing book's margin
            # is at the settings in force before them: `before`, against 5x in the recorded run.
            shift = sum(abs(value) * (1.0 / base - 1.0 / before[symbol]) for symbol, value in positions.items())
            common = {"buffer": config.margin_buffer, "default_leverage": base}
            kept_auto, auto = scale_orders_to_margin(orders, available, dict.fromkeys(managed, base), {}, **common)
            kept_vol, vol = scale_orders_to_margin(orders, available + shift, dict(standing), {}, **common)
            day = _day(bar) + datetime.fromtimestamp(bar / 1000, tz=UTC).strftime("T%H")
            if auto["scaled"] or any("MARGIN_SCALED" in order.note for order in orders):
                out["auto_scaled"].append(day)
            if vol["scaled"]:
                out["by_vol_scaled"].append(day)
            if kept_vol != kept_auto:
                out["orders_differ"].append(day)
            for key, reading in (("auto", auto), ("by_vol", vol)):
                room = (reading["budget"] - reading["needed_margin"]) / equity
                low = out["headroom_min"][key]
                out["headroom_min"][key] = room if low is None else min(low, room)
        tiered = set(managed) & (set(plan.ideal) | tiered)
        streaks = dict(plan.streaks)
        previous = targets
    out["first_switch"] = first_switch or {}
    out["changes_per_day"] = dict(sorted(changes.items()))
    out["final"] = {symbol: standing[symbol] for symbol in sorted(standing)}
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cycles", type=Path, default=Path(".beidou/live/cycles.jsonl"))
    parser.add_argument("--profile", type=Path, default=ROOT / "config" / "live.demo.yaml")
    parser.add_argument("--days", type=float, default=14.0, help="only the last N days of rows")
    parser.add_argument("--sigma-ref", type=float, default=None, help="override the profile's (a what-if, not T-6)")
    args = parser.parse_args()

    config = load_config(args.profile)
    if args.sigma_ref is not None:
        config = replace(config, leverage_sigma_ref=args.sigma_ref)
    rows = [json.loads(line) for line in args.cycles.read_text(encoding="utf-8").splitlines() if line.strip()]
    newest = max(int(row.get("bar_open_ms") or 0) for row in rows)
    rows = [row for row in rows if int(row.get("bar_open_ms") or 0) > newest - args.days * DAY_MS]
    result = replay(rows, config)

    per_day = list(result["changes_per_day"].values())
    t6 = not result["by_vol_scaled"] and not result["orders_differ"]
    t11 = max(per_day, default=0) <= 5
    print(f"rows {result['cycles']} (last {args.days:g} days), with orders {result['with_orders']}")
    print(f"sigma_ref {config.leverage_sigma_ref}  tiers {list(config.leverage_tiers)}  hysteresis {config.leverage_hysteresis}")
    print(f"first switch: {len(result['first_switch'])} symbols {result['first_switch']}")
    print(f"tiers at the end: {result['final']}")
    print(
        f"target-book margin / equity, max: tiered {result['tiered_margin_max']:.2%}, auto {result['auto_margin_max']:.2%};"
        f" cycles where the invariant raised a tier: {result['raised']}"
    )
    print(f"pre-check headroom / equity, min: {result['headroom_min']}")
    print(f"auto scaled (recorded run): {result['auto_scaled'] or 'never'}")
    print(f"T-6  {'PASS' if t6 else 'FAIL'}: by_vol scaled {result['by_vol_scaled'] or 'never'}, orders differ {result['orders_differ'] or 'never'}")
    print(
        f"T-11 {'PASS' if t11 else 'FAIL'}: tier changes per UTC day {result['changes_per_day']} "
        f"(max {max(per_day, default=0)}, limit 5; S4's daily re-send adds one per symbol per day by design)"
    )


if __name__ == "__main__":
    main()
