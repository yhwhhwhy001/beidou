"""The other half of the 2026-09-27 fix: something refreshes the archive M-011 compares against.

`data metrics` ran once, by hand, on 2026-09-09 for the 205 symbols of that day's pit universe, and
`deploy/run_data.sh` held it out of the nightly job: its note counted one reader, the research panel,
and a research-only feed does not belong in the loop's own job.  It missed the second reader -
`report daily` compares the archive with the loop's snapshot every hour - so the archive stayed at
2026-09-07 and M-011 re-read one frozen window for nineteen days (see
`tests/live/test_an_agreement_is_only_as_recent_as_its_newest_bucket.py`).

With no arguments the command now means the nightly job: every symbol the snapshot store holds - the
set M-011 compares, whatever the loop has managed - from each symbol's own watermark up to yesterday.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from click.testing import CliRunner

from beidou_cli import data_cmd, main
from beidou_data.store import MetricsStore

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "run_data.sh"


class _NoNetwork:
    def __enter__(self) -> _NoNetwork:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What `data metrics` asked the ingest for, with the network and the ingest itself stubbed out."""
    seen: dict[str, Any] = {}

    def sync(client: Any, store: MetricsStore, symbols: list[str], **kwargs: Any) -> dict[str, int]:
        seen.update(symbols=list(symbols), **kwargs)
        return {}

    monkeypatch.setattr(data_cmd, "MetricsArchiveClient", _NoNetwork)
    monkeypatch.setattr(data_cmd, "sync_metrics", sync)
    return seen


def test_the_nightly_data_job_runs_the_archive_ingest_m011_compares_against() -> None:
    """The lines that RUN something, read the way the spot test reads them - a comment is not a job."""
    runs = [line for line in SCRIPT.read_text(encoding="utf-8").splitlines() if line.startswith('"$BEIDOU" data ')]
    metrics = [line for line in runs if line.split()[2] == "metrics"]

    assert metrics, "nothing schedules the archive ingest, so the archive M-011 reads stops where it was last run"
    assert "--symbols" not in metrics[0], "the set must follow the snapshot store, not a list written down once"


def test_with_no_arguments_it_brings_every_snapshotted_symbol_up_to_yesterday(
    tmp_path: Path, asked: dict[str, Any]
) -> None:
    for symbol in ("LSKUSDT", "BTCUSDT"):
        MetricsStore(tmp_path, kind="metrics_snapshot").append(
            symbol, pd.DataFrame({"open_time": [0], "symbol": symbol, "sum_open_interest": 1.0})
        )

    before = datetime.now(UTC).strftime("%Y-%m-%d")
    result = CliRunner().invoke(main, ["data", "metrics", "--root", str(tmp_path)])
    after = datetime.now(UTC).strftime("%Y-%m-%d")

    assert result.exit_code == 0, result.output
    assert asked["symbols"] == ["BTCUSDT", "LSKUSDT"]
    assert asked["end"] in {before, after}, "exclusive, so the newest day asked for is yesterday"
    first = datetime.strptime(asked["end"], "%Y-%m-%d") - timedelta(days=30)
    assert asked["start"] == first.strftime("%Y-%m-%d"), "a symbol the archive has never held starts 30 days back"


def test_nothing_to_ingest_is_refused_rather_than_reported_as_done(tmp_path: Path, asked: dict[str, Any]) -> None:
    """An empty snapshot store on a machine the loop never ran on: a silent success would read as fresh."""
    result = CliRunner().invoke(main, ["data", "metrics", "--root", str(tmp_path)])

    assert result.exit_code != 0
    assert "snapshot" in result.output
    assert not asked, "the ingest was never asked"


def test_the_research_ingest_still_takes_its_own_symbols_and_window(tmp_path: Path, asked: dict[str, Any]) -> None:
    """The 2026-09-09 pit ingest passes all three; defaults must not leak into an explicit run."""
    result = CliRunner().invoke(
        main,
        [
            "data",
            "metrics",
            "--root",
            str(tmp_path),
            "--symbols",
            "nearusdt",
            "--from",
            "2021-12-01",
            "--to",
            "2021-12-03",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (asked["symbols"], asked["start"], asked["end"]) == (["NEARUSDT"], "2021-12-01", "2021-12-03")
