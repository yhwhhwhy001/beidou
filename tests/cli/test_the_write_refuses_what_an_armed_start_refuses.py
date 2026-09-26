"""The registry write refuses exactly what an armed `live run` refuses to start on.

`promote.py` asks the gate in process, before any restart, so that a bad write never reaches a running
loop - which is only true if that gate is the one startup asks.  Since D-041 it was not: `live run`
refuses on the evidence half OR the blocking half of the dataset check, and `governance_cmd._gate`
asked the evidence half alone.  Measured 2026-09-27 on the machine that runs the loop: after the 09-24
membership rebuild (refreshes 2056 -> 2063) the shipped registry failed the dataset half, and the
write's gate did not ask it.

So each case is built once and put to both, against a literal answer.  The four cases are the four
readings the startup gate has today; a refusal added to `live run` needs a case here too.  The armed
start stops on the first line after its gate - `resolve_universe` raises - so nothing that reads
credentials or opens a venue is reached, whatever the environment holds.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml
from click.testing import CliRunner

import beidou_cli.live_cmd as live_cmd
from beidou_cli import main
from beidou_data.manifest import build_manifest
from beidou_data.store import KlineStore
from tests.live.fakes import UNREACHABLE

ROOT = Path(__file__).resolve().parents[2]
#: A tsmom report whose evidence half passes against the shipped profile.  Only its `dataset` block is
#: replaced, by the manifest of the fixture's own data root, so the dataset half starts out agreeing.
REPORT = ROOT / "reports" / "research" / "tsmom-validation-20260913T182325Z.json"


class _PastTheGate(Exception):
    """Raised where an armed start goes on to build its universe, its venue and its account lock."""


def _membership(root: Path, refreshes: int) -> None:
    index = pd.date_range("2024-01-01", periods=refreshes, freq="D", tz="UTC")
    pd.DataFrame(True, index=index, columns=["BTCUSDT", "ETHUSDT"]).to_parquet(root / "membership.parquet")


def _case(tmp_path: Path, declared: str) -> tuple[Path, Path, Path]:
    """A profile, a registry citing one report, and the data root that report was produced on."""
    root = tmp_path / "data"
    root.mkdir()
    _membership(root, 30)
    KlineStore(root).append("BTCUSDT", "1h", pd.DataFrame({"open_time": [0, 3_600_000], "close": [1.0, 2.0]}))
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    report["dataset"] = build_manifest(root).to_dict()
    cited = tmp_path / "report.json"
    cited.write_text(json.dumps(report), encoding="utf-8")

    shipped = yaml.safe_load((ROOT / "config" / "alpha_registry.yaml").read_text(encoding="utf-8"))
    tsmom = next(entry for entry in shipped.pop("strategies") if entry["id"] == "tsmom")
    digest = hashlib.sha256(cited.read_bytes()).hexdigest()
    tsmom["evidence"] = {"report": str(cited), "sha256": digest, "verdict": declared}
    shipped.pop("books", None)  # the flow sleeve's book; the one strategy left is on the main book
    registry = tmp_path / "alpha_registry.yaml"
    registry.write_text(yaml.safe_dump({**shipped, "strategies": [tsmom]}), encoding="utf-8")

    profile = yaml.safe_load((ROOT / "config" / "live.demo.yaml").read_text(encoding="utf-8"))
    profile["registry"] = str(registry)
    profile["market_data"]["rest_url"] = UNREACHABLE
    profile["paths"] = {"state_dir": str(tmp_path / "live"), "reports_dir": str(tmp_path / "reports")}
    path = tmp_path / "live.yaml"
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    return path, registry, root


def _armed_start_refuses(profile: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> bool:
    def past_the_gate(*_args: Any, **_kwargs: Any) -> Any:
        raise _PastTheGate

    monkeypatch.setattr(live_cmd, "resolve_universe", past_the_gate)
    monkeypatch.setattr(live_cmd, "build_venue", past_the_gate)  # never reached; here in case the order moves
    result = CliRunner().invoke(main, ["live", "run", "--profile", str(profile), "--armed", "--data-root", str(root)])
    if isinstance(result.exception, _PastTheGate):
        return False
    assert "lack validation evidence" in result.output, result.output
    return True


def _governance(command: str, profile: Path, proposed: Path, root: Path, tmp_path: Path) -> tuple[int, dict[str, Any]]:
    """`plan` or `apply` of ``proposed`` over a copy that differs by one comment, in a checkout of its own."""
    current = tmp_path / "current.yaml"
    current.write_text(proposed.read_text(encoding="utf-8") + "# the file the proposal replaces\n", encoding="utf-8")
    result = CliRunner().invoke(
        main,
        [
            *("governance", command, "--proposed", str(proposed), "--registry", str(current)),
            *("--profile", str(profile), "--data-root", str(root), "--root", str(tmp_path)),
            *("--state-dir", str(tmp_path / "live"), "--shadow-dir", str(tmp_path / "live-shadow")),
        ],
    )
    assert result.exit_code in (0, 1), result.output
    return result.exit_code, json.loads(result.stdout)


def _sync_appended_bars(root: Path) -> None:
    KlineStore(root).append("ETHUSDT", "1h", pd.DataFrame({"open_time": [0], "close": [1.0]}))


def _membership_rebuilt(root: Path) -> None:
    _membership(root, 180)


@pytest.mark.parametrize(
    ("declared", "move", "refused"),
    [
        ("WEAK_PASS", lambda root: None, False),
        ("WEAK_PASS", _sync_appended_bars, False),  # advisory: printed, never refused
        ("WEAK_PASS", _membership_rebuilt, True),  # the half the write did not ask until 2026-09-27
        ("FAIL", lambda root: None, True),
    ],
    ids=["nothing-moved", "the-sync-appended-bars", "membership-rebuilt", "evidence-verdict-fail"],
)
def test_the_write_and_an_armed_start_give_one_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declared: str, move: Callable[[Path], None], refused: bool
) -> None:
    profile, registry, root = _case(tmp_path, declared)
    move(root)

    assert _armed_start_refuses(profile, root, monkeypatch) is refused
    code, transaction = _governance("plan", profile, registry, root, tmp_path)
    assert bool(transaction["reasons"]) is refused, transaction
    assert code == int(refused)


def test_apply_takes_back_a_write_whose_data_moved_under_its_evidence(tmp_path: Path) -> None:
    """`apply` asks the same gate as `plan`, so a rebuilt membership table rolls the write back."""
    profile, registry, root = _case(tmp_path, "WEAK_PASS")
    _membership_rebuilt(root)
    (tmp_path / "governance").mkdir()
    (tmp_path / "governance" / "ENABLED").write_text("enabled by the test\n", encoding="utf-8")

    code, transaction = _governance("apply", profile, registry, root, tmp_path)

    assert (code, transaction["action"]) == (1, "ROLLBACK")
    assert [reason.split(": ", 2)[:2] for reason in transaction["reasons"]] == [
        ["tsmom", "dataset changed since this result was produced"]
    ]
    assert (tmp_path / "current.yaml").read_text(encoding="utf-8").endswith("# the file the proposal replaces\n")
