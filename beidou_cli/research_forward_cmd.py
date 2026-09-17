"""``beidou research forward``：候选前向板（操作者 2026-09-17 裁定 Q-C = 建）。

**为什么是两条命令而不是一条带模式的。** `add` 花钱，`status` 不花钱。把它们做成同一条命令的两个
开关，迟早有人读一次板就花掉一笔——而板的门读的就是板上有几笔，所以误计一笔会让**所有**在板候选
的判定年限一起变长。分成两条，是让「花钱」这件事没有办法顺手发生。

**读板为什么免费。** 一个候选上板时就把假设钉死了：参数、universe、构造、声称的 Sharpe、起始 bar，
全部写进 `forward_board.jsonl` 并计一笔。此后每天重算的是**同一个**假设在多出来的数据上的读数，
不是一次新的选择。这正是前向检验与「再搜一次」的分别，也是板唯一便宜的地方。

**它不产生裁定。** 每份读数都带 `verdict` 与 `years_to_decide`，而按方案的定价，板上 30 个候选要
约 3.8 年才谈得上判。所以 `status` 的正常输出是一整版 `OBSERVING`，`decidable: 0`——那是诚实的读数，
不是功能没做完。

规则住在 `beidou_alpha/validation/forward_board.py`，这里只负责取数、计费、落盘。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from beidou_alpha.validation.forward_board import (
    FORWARD_BOARD_STRATEGY,
    BoardEntry,
    already_on_board,
    board_param_key,
    board_report,
    census,
    claimed_sharpe_from,
    forward_reading,
    read_board,
)
from beidou_alpha.validation.ledger import TrialRecord, resolve_ledger_path
from beidou_alpha.validation.pipeline import score_book
from beidou_cli import research
from beidou_cli.research_book_eval import _book_guards, _exit_params
from beidou_cli.research_ledger_io import (
    _construction_digest,
    _record_trial,
    _symbol_set_hash,
)
from beidou_cli.research_options import _common_options
from beidou_cli.research_panel import _entry, _load, _membership, _model, _resolve_symbols
from beidou_cli.research_report import _stamp, _write
from beidou_live.composition import cost_model
from beidou_shared.config import load_yaml

#: 板文件。与 ledger 同一个目录，因为它们是同一件事的两半：板是名册，ledger 是账单。
DEFAULT_BOARD = "reports/research/forward_board.jsonl"


@research.group("forward")
def research_forward() -> None:
    """候选前向板：上板即计费，读板免费，判定要年。"""


def _board_path(board: str) -> Path:
    return Path(board)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@research_forward.command("add")
@_common_options
@click.option(
    "--evidence",
    required=True,
    help=(
        "让这个候选够格上板的那份报告（JSON）。声称的 Sharpe 从它里面读，不接受手输——"
        "手输的那个数正是会被往低里写的：写低一点，判定年限就短一点，板位就能早点「到期」。"
        "样本外优先于全样本（`oos.annualized_sharpe` > `summary.annualized_sharpe`）。"
    ),
)
@click.option(
    "--charge",
    type=int,
    default=None,
    help=(
        "本次上板要花多少笔，必须与实际相符。上板即计费到 `forward_board` 桶，而板的门读的就是"
        "这个桶——每上一个板位，**所有**在板候选的判定年限一起变长。不写这个数就拒跑。"
    ),
)
@click.option("--board", default=DEFAULT_BOARD, show_default=True, help="板文件（append-only）")
@click.option("--note", default="", help="给下一个读板的人的一句话")
def forward_add(
    evidence: str,
    charge: int | None,
    board: str,
    note: str,
    strategy: str,
    params: str,
    root: str,
    symbols: str,
    interval: str,
    start: str | None,
    end: str | None,
    profile: str,
    registry_path: str,
    costs_path: str,
    execution: str,
    funding: bool,
    out: str,
    min_history: int | None,
    universe_mode: str,
    min_tenure: int,
    grids: str,
) -> None:
    """把一个候选放上前向板：钉死假设，计一笔，此后只观察。"""
    board_file = _board_path(board)
    existing = read_board(board_file.read_text(encoding="utf-8").splitlines() if board_file.exists() else [])

    evidence_path = Path(evidence)
    if not evidence_path.exists():
        raise click.ClickException(f"证据报告不存在：{evidence_path}")
    report = json.loads(evidence_path.read_text(encoding="utf-8"))
    claimed, where = claimed_sharpe_from(report)
    if claimed is None:
        raise click.ClickException(
            f"{evidence_path} 里读不到年化 Sharpe。板不拿 0.0 当默认——那会让判定年限读起来像"
            "「这个候选没希望」，而真相是「没读到那个数」。"
        )

    entry_spec = _entry(strategy, registry_path, params, grids)
    profile_payload = load_yaml(profile)
    portfolio = _model(entry_spec, profile_payload, interval, min_history).portfolio.__dict__
    cost = cost_model(load_yaml(costs_path), use_funding=funding)
    digest = _construction_digest(portfolio, cost, execution)

    candidate = BoardEntry(
        candidate=strategy,
        param_key=board_param_key(strategy, dict(entry_spec.params)),
        params=dict(entry_spec.params),
        universe=universe_mode,
        construction_digest=digest,
        entered_at=datetime.now(UTC).isoformat(timespec="seconds"),
        run_id=f"board-{_stamp()}",
        claimed_sharpe=claimed,
        evidence=str(evidence_path),
        evidence_sha256=_sha256(evidence_path),
        note=note,
    )

    if already_on_board(existing, candidate):
        click.echo(f"已在板上（{candidate.param_key}），不重复计费。板上现有 {census(existing)} 个板位。")
        return

    wanted = 1
    if charge != wanted:
        n_after = census([*existing, candidate])
        raise click.ClickException(
            f"这次上板要计 {wanted} 笔到 `{FORWARD_BOARD_STRATEGY}` 桶，请显式写 `--charge {wanted}`。\n"
            f"板上现有 {census(existing)} 个板位，加上这个是 {n_after} 个——"
            f"而板的门按 N 算，所以这一笔让**已经在板上的每一个候选**都要多等一段。\n"
            f"证据：{evidence_path}（{where} = {claimed:.4f}）"
        )

    ledger_path = resolve_ledger_path(out=out)
    chosen = _resolve_symbols(root, symbols, interval, universe_mode)
    trial = TrialRecord(
        strategy=FORWARD_BOARD_STRATEGY,
        param_key=f"{strategy}|{candidate.param_key}|{universe_mode}",
        sharpe_annual=None,  # 上板时没有前向读数，写 None 而不是把声称值抄进账单
        bars_per_year=0.0,
        recorded_at=candidate.entered_at,
        range_start=candidate.entered_at,
        range_end=candidate.entered_at,
        symbols=len(chosen),
        run_id=candidate.run_id,
        construction_digest=digest,
        symbol_set_hash=_symbol_set_hash(chosen),
    )
    charged = _record_trial(ledger_path, trial)

    board_file.parent.mkdir(parents=True, exist_ok=True)
    with board_file.open("a", encoding="utf-8") as handle:
        handle.write(candidate.to_json() + "\n")

    click.echo(f"上板：{strategy} {candidate.param_key} universe={universe_mode}")
    click.echo(f"  声称 Sharpe {claimed:.4f}（读自 {evidence_path} 的 {where}）")
    click.echo(
        f"  计费 {'1 笔' if charged else '0 笔（同一笔已在账上）'} → {ledger_path}（桶 {FORWARD_BOARD_STRATEGY}）"
    )
    click.echo(f"  板上现有 {census([*existing, candidate])} 个板位")


@research_forward.command("status")
@_common_options
@click.option("--board", default=DEFAULT_BOARD, show_default=True, help="板文件")
@click.option("--guards/--no-guards", default=True, show_default=True, help="按实盘的 book guards 定价")
@click.option("--exits/--no-exits", "exits", default=True, show_default=True, help="按实盘的退出层定价")
def forward_status(
    board: str,
    guards: bool,
    exits: bool,
    strategy: str,
    params: str,
    root: str,
    symbols: str,
    interval: str,
    start: str | None,
    end: str | None,
    profile: str,
    registry_path: str,
    costs_path: str,
    execution: str,
    funding: bool,
    out: str,
    min_history: int | None,
    universe_mode: str,
    min_tenure: int,
    grids: str,
) -> None:
    """重算板上每个候选的前向读数。**不计费**：钉死的假设在多出来的数据上重读，不是一次新的选择。"""
    board_file = _board_path(board)
    if not board_file.exists():
        click.echo(f"板是空的（{board_file} 不存在）。`research forward add` 放第一个候选上去。")
        return
    entries = read_board(board_file.read_text(encoding="utf-8").splitlines())
    if not entries:
        click.echo("板上没有可读的条目。")
        return

    n = census(entries)
    profile_payload = load_yaml(profile)
    cost = cost_model(load_yaml(costs_path), use_funding=funding)
    readings: list[dict[str, Any]] = []
    for item in entries:
        chosen = _resolve_symbols(root, symbols, interval, item.universe)
        panel = _load(root, chosen, interval, start, end, funding)
        membership = _membership(root, item.universe, panel, min_tenure)
        spec = _entry(item.candidate, registry_path, json.dumps(item.params), grids)
        model = _model(spec, profile_payload, interval, min_history)
        weights, _combined, _per = model.evaluate(panel, membership)
        result, _priced = score_book(
            panel,
            weights,
            cost,
            execution=execution,
            guards=_book_guards(profile_payload, guards),
            exits=_exit_params(profile_payload, exits, interval),
        )
        readings.append(
            forward_reading(
                result.portfolio_net,
                item,
                bars_per_year=panel.bars_per_year,
                n_on_board=n,
                params_now=dict(spec.params),
            )
        )

    report = board_report(readings)
    report["board"] = str(board_file)
    # 板读数单独成文，不进任何 validate / book 报告。Scope Firewall 原话：
    # 「前向板读数不进任何历史选择」——一份混在一起的报告，迟早会有人拿板上的排序去挑候选。
    lines = [
        f"板上 {report['n_on_board']} 个板位；到得了判定年数的 {report['decidable']} 个；"
        f"过门的 {report['passing']} 个；作废的 {report['tampered']} 个。"
    ]
    for reading in readings:
        if reading.get("verdict") == "TAMPERED":
            lines.append(f"  {reading['candidate']} {reading['param_key']}  作废：{reading['reason']}")
            continue
        # 一个**今天刚上板**的候选前向窗口是空的，所以这三个都可能是 None，而那是正常状态不是异常：
        # 板位在上板那一刻就成立，读数要等下一根 bar。直接格式化 None 会让日任务在上板当天崩掉。
        sharpe_text = "—" if reading["sharpe_annual"] is None else f"{reading['sharpe_annual']:.3f}"
        gate_text = "—" if reading["threshold_annual"] is None else f"{reading['threshold_annual']:.3f}"
        years_text = "永远不够" if reading["years_to_decide"] is None else f"{reading['years_to_decide']:.2f}"
        lines.append(
            f"  {reading['candidate']} {reading['param_key']} {reading['universe']}  "
            f"前向 {reading['years_forward']:.2f} 年  S={sharpe_text}  "
            f"门={gate_text}  要看 {years_text} 年  {reading['verdict']}"
        )
    for line in lines:
        click.echo(line)

    path, digest = _write(out, f"forward-board-{_stamp()}", report, "\n".join(["# 候选前向板", "", *lines]))
    click.echo(f"report: {path} sha256={digest}")
