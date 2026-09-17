"""候选前向板：把「可观察」的候选数从 2 提到数十，代价是每上一个板都让所有候选更难过门。

**它买的是什么，以及明确不是什么。** 2026-09-17 的 alpha 模块深度分析（C-AM08，`docs/analysis/
2026-09-17-alpha-module-deep-analysis.md`）把这件事的价钱算过：板提高的是**可观察**的候选数，不是
**可裁定**的。在 `Policy.max_concurrent_probes = 2` 之下，任何时刻只有两个候选在拿真实盘位；板让
几十个候选同时被前向记录，venue 成本为零。但判定要年：单候选按 S=1.5 判到 z=2 要约 1.8 年，板上 30
个候选按选择门要 z≈2.93，即约 3.8 年（`years_to_decide` 就是这两个数的来源）。所以这个模块的每一份
读数都带 `verdict: OBSERVING` 与 `years_to_decide`，**在到点之前它不产生任何裁定**。

**四条不变式，每条都对应一个已写下的失败模式。**

1. **前向就是前向。** 读数只用 `entered_at` 当根 bar 及其之后的收益（`forward_slice`）。一个候选上板
   之前的表现，无论多好，都不进它的板读数——那正是它上板之前已经被用过的那部分样本。
2. **上板即计费，计到独立桶。** 每个板条目在 `FORWARD_BOARD_STRATEGY` 桶里写一行。`ledger_scope`
   对任何真实策略都不返回这个桶，所以板的计费**不进任何 family gate 的分母**；反过来，板自己的门
   `board_threshold` 读的就是这个桶的行数。RISK-AM03：板若不计费就是一条免费窥视通道。
3. **板上的候选不许被换参数。** 条目记 `param_key`（参数的规范摘要）。重算时参数对不上就是
   `TAMPERED`，那一条作废而不是给出读数（FM-AM4：板上候选被换参数 → 撤板）。
4. **板读数不进任何历史选择。** 这个模块不导出任何能喂给 `validate` / `book` 的东西，它的报告也
   单独成文。Scope Firewall 原话：「前向板读数不进任何历史选择」。

**门为什么不直接用 `max_sharpe_quantile`。** 那是 D-028 的选择门，`n_trials <= 1` 时按约定返回
0.0——没有选择就没有选择门，这在 `validate` 里是对的，因为显著性由别处的 OOS 门与 DSR 负责。板不同：
板上的读数**本身就是那个检验**，所以哪怕只有一个候选也要过普通的单边 95%。`board_threshold` 因此取
「选择门」与「单边临界值」的较大者。**不改 `max_sharpe_quantile`**：它是 `validate` 在用的门，动它
是一次 R10 规则变更，会移动每一条历史裁决的阈值。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.report import canonical_json
from beidou_alpha.validation.metrics import normal_ppf
from beidou_alpha.validation.multiple_testing import max_sharpe_quantile, sampling_variance

#: 板自己的 ledger 桶。`ledger_scope` 对任何真实策略都不会返回它，所以板的计费不进 family gate。
#: 与 `MINED_SEARCH_STRATEGY` / `PAIR_SEARCH_STRATEGY` 同一形状：桶属于**那次观察**，不属于被观察的策略。
FORWARD_BOARD_STRATEGY = "forward_board"

#: 板读数的三种状态。没有第四种——尤其没有「通过」以外的任何软性说法。
OBSERVING = "OBSERVING"  # 还没到可判的年数，或还没过门
PASSES = "PASSES_BOARD_GATE"  # 过了按今天板大小算的门
TAMPERED = "TAMPERED"  # 参数与上板时对不上，这一条作废


@dataclass(frozen=True)
class BoardEntry:
    """一个候选上板时被钉住的东西。append-only：改一个条目等于换一个候选。"""

    candidate: str  # 候选来自哪个族（记录用，**不是**它的 ledger 桶）
    param_key: str  # 上板时参数的规范摘要，重算时要对上
    params: dict[str, Any]
    universe: str
    construction_digest: str
    entered_at: str  # UTC ISO。前向窗口从这里开始，一根 bar 都不往前借
    run_id: str
    #: 上板时这个候选**声称**的年化 Sharpe（来自让它够格上板的那份 validate 报告）。
    #: 判定年限由它算，**不由后来观察到的 Sharpe 算**——否则一个早期走运的候选会自己缩短
    #: 自己的年限，那正是这块东西该防的循环。它在上板那一刻钉死，此后不再变。
    claimed_sharpe: float = 0.0
    #: 那个 `claimed_sharpe` 是从哪份报告读出来的，以及那份报告的 sha256。
    #: 不让它手输，是因为手输的「声称 Sharpe」正是会被往低里写的那个数——写低一点，
    #: `years_to_decide` 就短一点，板位就能早点「到期」。从报告读并钉住摘要，这条路就堵上了。
    evidence: str = ""
    evidence_sha256: str = ""
    note: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> BoardEntry | None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return cls(
                candidate=str(payload["candidate"]),
                param_key=str(payload["param_key"]),
                params=dict(payload["params"]),
                universe=str(payload["universe"]),
                construction_digest=str(payload["construction_digest"]),
                entered_at=str(payload["entered_at"]),
                run_id=str(payload["run_id"]),
                claimed_sharpe=float(payload.get("claimed_sharpe", 0.0)),
                evidence=str(payload.get("evidence", "")),
                evidence_sha256=str(payload.get("evidence_sha256", "")),
                note=str(payload.get("note", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def identity(self) -> tuple[str, str, str, str]:
        """同一候选、同一参数、同一 universe、同一构造 = 同一个板位，第二次上板不另计费。"""
        return (self.candidate, self.param_key, self.universe, self.construction_digest)


def _normalised(value: Any) -> Any:
    """整值浮点归一为整数，其余原样。

    这一步只为一件事：参数经 YAML/JSON 往返一次，`168` 可能变成 `168.0`。那不是换了候选，而
    `tampering` 误报会**作废一个真板位**——一个已经前向观察了两年、再也补不回来的板位。所以这里
    归一，而且只归一这一种：别的类型差异（字符串 "168" 对数字 168）确实是不同的输入，不该被抹平。

    `bool` 明确排除：`True` 是 `int` 的子类，归一会让 `enabled: true` 与 `enabled: 1` 撞成一个键，
    而那两个在 YAML 里是不同的写法、也该是不同的候选。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Mapping):
        return {str(k): _normalised(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalised(v) for v in value]
    return value


def board_param_key(candidate: str, params: Mapping[str, Any]) -> str:
    """参数的规范摘要。用 `canonical_json` 而不是 `str(dict)`：键序与浮点写法都不该改变身份。"""
    return hashlib.sha256(f"{candidate}|{canonical_json(_normalised(dict(params)))}".encode()).hexdigest()[:16]


#: 一份报告里「年化 Sharpe」可能待的地方，按优先级。`validate` 与 `backtest` 的形状不同，
#: 而板要能从两者任一上板，所以这个顺序本身是契约的一部分：**样本外优先于全样本**。
CLAIMED_SHARPE_PATHS: tuple[tuple[str, ...], ...] = (
    ("oos", "annualized_sharpe"),
    ("walk_forward", "oos", "annualized_sharpe"),
    ("summary", "annualized_sharpe"),
)


def claimed_sharpe_from(report: Mapping[str, Any]) -> tuple[float | None, str]:
    """从一份报告里读上板时该钉住的 Sharpe。返回 (值, 它在报告里的路径)。

    找不到就返回 `(None, "")` 而不是 0.0：0.0 会让 `years_to_decide` 返回 `None`，读起来像
    「这个候选没希望」，而真相是「没读到那个数」。两件事混在一起，板会安静地记下一个错的年限。
    """
    for path in CLAIMED_SHARPE_PATHS:
        node: Any = report
        for key in path:
            if not isinstance(node, Mapping) or key not in node:
                node = None
                break
            node = node[key]
        if isinstance(node, (int, float)) and not isinstance(node, bool) and math.isfinite(float(node)):
            return float(node), ".".join(path)
    return None, ""


def read_board(lines: Iterable[str]) -> list[BoardEntry]:
    """读板。坏行跳过而不是抛错——板是 append-only 的记录，一行坏掉不该让整块读不出来。"""
    out: list[BoardEntry] = []
    for line in lines:
        if not line.strip():
            continue
        entry = BoardEntry.from_json(line)
        if entry is not None:
            out.append(entry)
    return out


def census(entries: Sequence[BoardEntry]) -> int:
    """板上有几个不同的板位。门的 N 就是它——**板越大，每个候选越难过门**。

    这与 D-028「搜得越多越退休自己的 incumbent」是同一条性质，换到前向上：多看一个候选，
    是在抬高所有候选的门，包括已经在板上看了两年的那个。写在这里是为了让加板位这件事有价钱。
    """
    return len({entry.identity() for entry in entries})


def already_on_board(entries: Sequence[BoardEntry], candidate: BoardEntry) -> bool:
    return candidate.identity() in {entry.identity() for entry in entries}


def tampering(entry: BoardEntry, params_now: Mapping[str, Any]) -> str:
    """参数对不上就是作废，不是「大致还是那个候选」。返回空串表示没问题。"""
    key_now = board_param_key(entry.candidate, params_now)
    if key_now == entry.param_key:
        return ""
    return (
        f"板上 `{entry.candidate}` 的参数摘要是 {entry.param_key}，今天重算得到 {key_now}。"
        "板上的候选不许被换参数——换了就是另一个候选，要另开一个板位并另行计费。"
    )


def forward_slice(net: pd.Series, entered_at: str) -> pd.Series:
    """只留 `entered_at` 当根 bar 及其之后的收益。

    这是这块东西的全部意义所在，所以它是一个有名字的函数而不是一行切片：一个候选上板之前的表现
    已经被它上板这件事本身用掉了，把那段收益算进板读数，板就成了另一种样本内。
    """
    if net.empty:
        return net
    start = pd.Timestamp(entered_at)
    if start.tzinfo is None:
        start = start.tz_localize(UTC)
    index = net.index
    if isinstance(index, pd.DatetimeIndex) and index.tz is None:
        index = index.tz_localize(UTC)
        net = pd.Series(net.to_numpy(), index=index, name=net.name)
    return net[net.index >= start]


def board_threshold(n_on_board: int, *, n_obs: int, alpha: float = 0.05) -> float:
    """今天板上这么多候选时，一个候选要清的**每期** Sharpe。

    取两者较大：D-028 的选择门（`max_sharpe_quantile`，N<=1 时按约定为 0），以及普通的单边
    `1 - alpha` 临界值。后者是地板：板上只有一个候选时没有选择偏差，但「看了就算数」仍要过显著性，
    而在 `validate` 里那件事由别的门负责，在板上没有别人负责。
    """
    if n_obs <= 1 or not 0.0 < alpha < 1.0:
        return math.inf
    variance = sampling_variance(0.0, n_obs)
    floor = normal_ppf(1.0 - alpha) * math.sqrt(variance)
    return max(max_sharpe_quantile(max(n_on_board, 1), variance, alpha=alpha), floor)


def years_to_decide(claimed_sharpe: float, *, n_on_board: int, alpha: float = 0.05) -> float | None:
    """按今天板上的候选数，一个**声称**这么高年化 Sharpe 的候选要观察多少年才够清门。

    吃的是上板时钉住的 `BoardEntry.claimed_sharpe`，**不是**后来观察到的 Sharpe。用观察值是循环的：
    早期走运的候选会把年限缩短到已经观察到的长度，于是「够久了」其实只是「今天恰好好看」。

    推导只有一步：零假设下年化 Sharpe 的噪声 ≈ 1/√T（T 以年计），所以门 ≈ z(N)/√T，
    解 S = z(N)/√T 得 **T = (z(N)/S)²**。方案里那两个数就是它：单候选 z=2 → 1.8 年；
    30 个候选 z≈2.93 → 3.8 年。

    `None` 表示永远不够——Sharpe 非正时没有 T 能让它过门。

    **与方案里那个数的一处差别，写明而不是让它自己消化掉。** 分析文档写「单候选判到 z=2 约 1.8 年」，
    那个 z=2 是举例用的（约等于单边 97.7%）。本模块按 `alpha` 走：α=0.05 的单边临界值是 1.645，
    S=1.5 时得 **1.2 年**。30 个候选那个数（z≈2.93 → 3.8 年）两边一致，因为它由选择门决定而不由地板决定。
    """
    if claimed_sharpe <= 0.0 or not 0.0 < alpha < 1.0:
        return None
    z = max(max_sharpe_quantile(max(n_on_board, 1), 1.0, alpha=alpha), normal_ppf(1.0 - alpha))
    return float((z / claimed_sharpe) ** 2)


def forward_reading(
    net: pd.Series,
    entry: BoardEntry,
    *,
    bars_per_year: float,
    n_on_board: int,
    alpha: float = 0.05,
    params_now: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """一个板位今天的读数。**永远带上 `verdict` 与 `years_to_decide`，永远不只给一个 Sharpe。**

    只给 Sharpe 的板会被当成排行榜读，而排行榜正是这块东西唯一真正的危险：它让人以为看见了
    可以据以行动的东西，而实际上要等年。
    """
    if params_now is not None:
        reason = tampering(entry, params_now)
        if reason:
            return {
                "candidate": entry.candidate,
                "param_key": entry.param_key,
                "universe": entry.universe,
                "entered_at": entry.entered_at,
                "verdict": TAMPERED,
                "reason": reason,
            }

    forward = forward_slice(net.dropna(), entry.entered_at)
    n_obs = int(forward.shape[0])
    values = forward.to_numpy(dtype=float)
    mean = float(np.mean(values)) if n_obs else 0.0
    sd = float(np.std(values, ddof=1)) if n_obs > 1 else 0.0
    sharpe_period = mean / sd if sd > 0 else None
    sharpe_annual = None if sharpe_period is None else sharpe_period * math.sqrt(bars_per_year)
    threshold = board_threshold(n_on_board, n_obs=n_obs, alpha=alpha) if n_obs > 1 else math.inf
    # 年限用**上板时钉住的** `claimed_sharpe`，不用刚算出来的 `sharpe_annual`。后者会让一个
    # 早期走运的候选把自己的年限缩短到已经观察到的长度，于是「够久了」变成「今天恰好好看」。
    # N 用今天的板大小，所以年限会随板变长——那是加板位的价钱，该由每个在板的候选一起付。
    years = years_to_decide(entry.claimed_sharpe, n_on_board=n_on_board, alpha=alpha)
    observed_years = n_obs / bars_per_year if bars_per_year > 0 else 0.0

    passes = sharpe_period is not None and math.isfinite(threshold) and sharpe_period > threshold
    return {
        "candidate": entry.candidate,
        "param_key": entry.param_key,
        "universe": entry.universe,
        "entered_at": entry.entered_at,
        "bars_forward": n_obs,
        "years_forward": round(observed_years, 4),
        "sharpe_annual": sharpe_annual,
        "threshold_period": threshold if math.isfinite(threshold) else None,
        "threshold_annual": (threshold * math.sqrt(bars_per_year)) if math.isfinite(threshold) else None,
        "n_on_board": n_on_board,
        "claimed_sharpe": entry.claimed_sharpe,
        "years_to_decide": years,
        # 到点了没有，与过门没过门是两件事，分开记：一个候选可以今天就在门上面，而它要到
        # `years_to_decide` 才有足够的观察让那件事不是噪声。
        "long_enough": years is not None and observed_years >= years,
        "verdict": PASSES if (passes and years is not None and observed_years >= years) else OBSERVING,
    }


def board_report(readings: Sequence[Mapping[str, Any]], *, generated_at: str | None = None) -> dict[str, Any]:
    """整块板的读数。`decidable` 是「今天有几条到了可判的年数」，通常是 0，而那是诚实的。"""
    stamp = generated_at or datetime.now(UTC).isoformat(timespec="seconds")
    return {
        "generated_at": stamp,
        "n_on_board": len({(r.get("candidate"), r.get("param_key"), r.get("universe")) for r in readings}),
        "tampered": sum(1 for r in readings if r.get("verdict") == TAMPERED),
        "decidable": sum(1 for r in readings if r.get("long_enough")),
        "passing": sum(1 for r in readings if r.get("verdict") == PASSES),
        "readings": list(readings),
    }
