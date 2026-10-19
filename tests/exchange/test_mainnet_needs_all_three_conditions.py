"""mainnet 只在三件事同时成立时放行（docs/MAINNET_READINESS.md §4.2–4.3，2026-10-19）。

三件事来自三个地方：仓库里的签字文件、这一次的命令行、profile 自己的声明。缺任何一件，guard 在构造时就拒绝，
所以连签名的读请求也到不了 mainnet。8 种组合只有全真放行，这里逐个核。

同一处还核两个口：

- `BinanceRestClient` 不带 guard 时，主机按 demo/testnet 白名单查，到不了 mainnet；带 guard 时，URL 的主机必须是
  guard 批过的那个。此前不带 guard 就完全不查主机（§4.1）。
- `/fapi/v1/algoOrder` 在 `ORDER_PATHS` 里。Binance 把 STOP_MARKET 等条件单挪到了这个接口；`is_risk_reducing`
  对不在集合里的路径一律答「降风险」，所以不列进去，kill switch 在时加风险的条件单照样放行。
"""

from __future__ import annotations

import itertools
from pathlib import Path

import click
import pytest

from beidou_cli.live_cmd import venue_release
from beidou_exchange.binance_usdm.rest_client import BinanceRestClient
from beidou_exchange.guard import (
    ALLOWED_HOSTS,
    SIGNOFF_PATH,
    GuardError,
    MainnetRelease,
    WriteGuard,
    is_risk_reducing,
    mainnet_refusal,
    normalize_host,
)

MAINNET = "https://fapi.binance.com"
DEMO = "https://demo-fapi.binance.com"
PROFILE = "config/live.mainnet.yaml"


def _signoff(root: Path, names: str = PROFILE) -> Path:
    path = root / "governance" / "MAINNET_ENABLED"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"profile: {names}\n", encoding="utf-8")
    return path


def _release(root: Path, *, signed: bool, flag: bool, named: bool) -> MainnetRelease:
    signoff = _signoff(root) if signed else root / "governance" / "MAINNET_ENABLED"
    return MainnetRelease(
        profile_path=PROFILE if named else "config/live.demo.yaml",
        profile_label="mainnet" if named else "demo",
        flag=flag,
        signoff_path=signoff,
    )


@pytest.mark.parametrize(("signed", "flag", "named"), list(itertools.product([True, False], repeat=3)))
def test_only_all_three_together_reach_mainnet(tmp_path: Path, signed: bool, flag: bool, named: bool) -> None:
    release = _release(tmp_path, signed=signed, flag=flag, named=named)
    if signed and flag and named:
        assert normalize_host(MAINNET, mainnet=release) == "fapi.binance.com"
        assert WriteGuard(MAINNET, mainnet=release).host == "fapi.binance.com"
    else:
        with pytest.raises(GuardError, match="mainnet and is refused"):
            normalize_host(MAINNET, mainnet=release)
        with pytest.raises(GuardError):
            WriteGuard(MAINNET, mainnet=release)


def test_the_file_name_and_the_profile_key_are_each_required(tmp_path: Path) -> None:
    signoff = _signoff(tmp_path, names="config/live.demo.yaml")
    # a copy of the demo profile renamed but still saying `profile: demo` - the most natural mistake (§4.3)
    copied = MainnetRelease("config/live.demo.yaml", "mainnet", True, signoff)
    assert "does not name mainnet" in (mainnet_refusal(copied) or "")
    relabelled = MainnetRelease(PROFILE, "demo", True, _signoff(tmp_path))
    assert "`profile: demo`" in (mainnet_refusal(relabelled) or "")


def test_a_sign_off_naming_another_profile_releases_nothing(tmp_path: Path) -> None:
    signoff = _signoff(tmp_path, names="config/live.mainnet-other.yaml")
    release = MainnetRelease(PROFILE, "mainnet", True, signoff)
    assert "the sign-off names" in (mainnet_refusal(release) or "")
    # the same profile spelled as an absolute path is the same profile
    same = MainnetRelease(str((tmp_path / PROFILE).resolve()), "mainnet", True, _signoff(tmp_path))
    assert mainnet_refusal(same) is None


def test_an_unreadable_or_empty_sign_off_is_no_sign_off(tmp_path: Path) -> None:
    path = tmp_path / "governance" / "MAINNET_ENABLED"
    path.parent.mkdir(parents=True)
    for text in ("", "profile:\n", ": : :\n", "- a list\n", "profile: # nothing\n"):
        path.write_text(text, encoding="utf-8")
        assert "no operator sign-off" in (mainnet_refusal(MainnetRelease(PROFILE, "mainnet", True, path)) or "")


def test_no_release_and_no_sign_off_in_the_repository() -> None:
    assert mainnet_refusal(None) == "no mainnet release was presented"
    with pytest.raises(GuardError):
        normalize_host(MAINNET)
    # Shipping this mechanism ships no release: the sign-off is a separate, operator-merged PR (HC-3).
    assert not SIGNOFF_PATH.exists()


def test_the_demo_allow_list_is_word_for_word_unchanged() -> None:
    assert frozenset({"demo-fapi.binance.com", "testnet.binancefuture.com"}) == ALLOWED_HOSTS
    # a release never widens anything but the mainnet host itself
    for other in ("https://api.binance.com", "https://fapi.binance.com.evil.example"):
        with pytest.raises(GuardError, match="not an allowed"):
            normalize_host(other, mainnet=MainnetRelease(PROFILE, "mainnet", True, SIGNOFF_PATH))


def test_a_client_without_a_guard_cannot_reach_mainnet() -> None:
    with pytest.raises(GuardError):
        BinanceRestClient(MAINNET, "k", "s")
    BinanceRestClient(DEMO, "k", "s")  # the shape every existing caller and test uses keeps working


def test_a_demo_guard_cannot_be_paired_with_a_mainnet_url() -> None:
    with pytest.raises(GuardError, match="not the host its guard approved"):
        BinanceRestClient(MAINNET, "k", "s", guard=WriteGuard(DEMO))


def test_conditional_orders_are_order_paths_under_the_kill_switch(tmp_path: Path) -> None:
    switch = tmp_path / "KILL_SWITCH"
    switch.write_text("x")
    guard = WriteGuard(DEMO, switch)
    params = {"algoType": "CONDITIONAL", "symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "BUY"}
    assert not is_risk_reducing("POST", "/fapi/v1/algoOrder", params)
    with pytest.raises(GuardError):
        guard.authorize("POST", "/fapi/v1/algoOrder", params)
    guard.authorize("POST", "/fapi/v1/algoOrder", {**params, "closePosition": "true"})
    guard.authorize("POST", "/fapi/v1/algoOrder", {**params, "reduceOnly": "true"})


def test_mainnet_flag_on_a_demo_profile_is_refused_out_loud() -> None:
    demo = {"profile": "demo", "venue": {"rest_url": DEMO}}
    with pytest.raises(click.ClickException, match="is not mainnet"):
        venue_release("config/live.demo.yaml", demo, flag=True)
    release = venue_release("config/live.demo.yaml", demo, flag=False)
    assert release == MainnetRelease("config/live.demo.yaml", "demo", False)
    mainnet = {"profile": "mainnet", "venue": {"rest_url": MAINNET}}
    assert venue_release(PROFILE, mainnet, flag=True).flag
