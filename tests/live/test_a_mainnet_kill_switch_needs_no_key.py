"""mainnet 的账户级 kill switch 按 venue 主机寻址，不按 API key（MAINNET_READINESS §5；切换方案 M4，2026-10-19）。

按 key 寻址有两个后果：换 key 就换了开关的位置，旧 key 下落着的开关新进程读不到；`--release` 在 shell 里没有凭据时
找不到它，只好拒绝。主机两样都不需要。L-B 只有一个 mainnet 子账户，所以主机就是账户的名字。demo 的路径一字不变。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from beidou_cli.live_cmd import kill_switch_targets, live_kill_switch
from beidou_live.config import account_kill_switches
from beidou_live.lock import account_kill_switch_path, venue_kill_switch_path

MAINNET = {"venue": {"rest_url": "https://fapi.binance.com", "api_key_env": "BEIDOU_MAINNET_TEST_KEY"}}


def test_a_mainnet_switch_does_not_move_when_the_key_does(
    monkeypatch: pytest.MonkeyPatch, isolated_app_support: Path
) -> None:
    monkeypatch.delenv("BEIDOU_MAINNET_TEST_KEY", raising=False)
    without = account_kill_switches(MAINNET)
    monkeypatch.setenv("BEIDOU_MAINNET_TEST_KEY", "old-key")
    old = account_kill_switches(MAINNET)
    monkeypatch.setenv("BEIDOU_MAINNET_TEST_KEY", "rotated-key")
    assert without == old == account_kill_switches(MAINNET) == (venue_kill_switch_path("fapi.binance.com"),)
    assert without[0] == isolated_app_support / "fapi.binance.com.KILL_SWITCH"


def test_demo_keeps_the_key_derived_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    demo = {"venue": {"rest_url": "https://demo-fapi.binance.com", "api_key_env": "BEIDOU_DEMO_TEST_KEY"}}
    monkeypatch.setenv("BEIDOU_DEMO_TEST_KEY", "demo-key")
    assert account_kill_switches(demo) == (account_kill_switch_path("demo-key"),)
    monkeypatch.delenv("BEIDOU_DEMO_TEST_KEY")
    assert account_kill_switches(demo) == ()


def test_a_mainnet_switch_engages_and_releases_without_the_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BEIDOU_MAINNET_TEST_KEY", raising=False)
    profile = tmp_path / "live.mainnet.yaml"
    profile.write_text(
        "venue:\n  rest_url: https://fapi.binance.com\n  api_key_env: BEIDOU_MAINNET_TEST_KEY\n"
        f"guards:\n  kill_switch_path: {tmp_path / 'KILL_SWITCH'}\n",
        encoding="utf-8",
    )
    targets = kill_switch_targets(MAINNET | {"guards": {"kill_switch_path": str(tmp_path / "KILL_SWITCH")}})
    runner = CliRunner()
    engaged = runner.invoke(live_kill_switch, ["--profile", str(profile), "--engage"])
    assert engaged.exit_code == 0, engaged.output
    assert all(path.exists() for path in targets) and len(targets) == 2
    released = runner.invoke(live_kill_switch, ["--profile", str(profile), "--release"])
    assert released.exit_code == 0, released.output
    assert not any(path.exists() for path in targets)
