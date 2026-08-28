"""PKG-02 CLI/config contracts for the bounded Testnet verifier."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from apps.testnet_verify.cli import main
from apps.testnet_verify.config import VerifierConfig
from apps.testnet_verify.runtime import VerificationSummary


def test_config_from_env_uses_dataclass_defaults(monkeypatch) -> None:
    for name in (
        "BEIDOU_TESTNET_REST_URL",
        "BEIDOU_TESTNET_API_KEY",
        "BEIDOU_TESTNET_API_SECRET",
        "BEIDOU_TESTNET_ACCOUNT_ID",
        "BEIDOU_TESTNET_MAX_NOTIONAL",
        "BEIDOU_TESTNET_MAX_LEVERAGE",
        "BEIDOU_TESTNET_MAX_INSTRUMENTS",
        "BEIDOU_TESTNET_INTERVAL",
        "BEIDOU_TESTNET_KLINE_LIMIT",
        "BEIDOU_TESTNET_TRACE_PATH",
        "BEIDOU_TESTNET_POOL_STATE_PATH",
        "BEIDOU_TESTNET_EVIDENCE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)

    config = VerifierConfig.from_env()

    assert config.rest_url == "https://demo-fapi.binance.com"
    assert config.max_notional == 25.0
    assert config.max_leverage == 3.0
    assert config.max_instruments == 5
    assert config.interval == "1m"


def test_cli_help_is_side_effect_free(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["--help"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "--confirm-testnet" in result.output
    assert "--max-notional" in result.output
    assert "--max-leverage" in result.output
    assert "--close-after-verify" in result.output
    assert not (tmp_path / ".beidou").exists()
    assert not (tmp_path / "evidence").exists()


def test_cli_passes_bounded_flags_to_runtime(monkeypatch, tmp_path: Path) -> None:
    captured: list[VerifierConfig] = []

    async def fake_run(config: VerifierConfig) -> VerificationSummary:
        captured.append(config)
        return VerificationSummary(
            run_id="run-1",
            status="EPISODE_COMPLETED",
            startup={},
            episodes=[],
            trace_ids=[],
        )

    monkeypatch.setattr("apps.testnet_verify.cli.run", fake_run)
    result = CliRunner().invoke(
        main,
        [
            "--confirm-testnet",
            "--max-notional",
            "12.5",
            "--max-leverage",
            "2",
            "--once",
            "--close-after-verify",
            "--trace-path",
            str(tmp_path / "trace.jsonl"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert len(captured) == 1
    assert captured[0].confirm_testnet is True
    assert captured[0].max_notional == 12.5
    assert captured[0].max_leverage == 2.0
    assert captured[0].once is True
    assert captured[0].close_after_verify is True
    assert captured[0].trace_path == tmp_path / "trace.jsonl"
    assert "api_secret" not in result.output


def test_cli_marks_not_verifiable_with_nonzero_exit(monkeypatch) -> None:
    async def fake_run(_config: VerifierConfig) -> VerificationSummary:
        return VerificationSummary(
            run_id="run-unknown",
            status="NOT_VERIFIABLE",
            startup={},
            episodes=[],
            trace_ids=[],
        )

    monkeypatch.setattr("apps.testnet_verify.cli.run", fake_run)
    result = CliRunner().invoke(main, ["--once"], catch_exceptions=False)

    assert result.exit_code == 2
    assert '"status": "NOT_VERIFIABLE"' in result.output
