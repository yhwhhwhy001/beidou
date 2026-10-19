"""mainnet 循环没人看着就不启动（切换方案 M7，操作者 2026-10-19 卡片「作为硬门」）。

两样都要有：两个告警通道（一条 URL 是单点静默，DL-L3），以及宿主外的 dead-man（每个通道都从这台机器发出，
机器一停就什么都发不出去：10-03 到 10-08 demo 循环停了 5.5 天，没有一条告警）。mainnet 的 dead-man 是自己的
check：两个循环 ping 同一个 check，任何一个活着它就是绿的。
"""

from __future__ import annotations

import inspect

import pytest

from beidou_cli.live_cmd import live_run
from beidou_live.deadman import LOOP_URL_ENV, MAINNET_URL_ENV, loop_url, mainnet_watch_problems

WATCHED = ("https://hooks.example/one", "https://hooks.example/two")


def test_two_channels_and_a_dead_man_may_start() -> None:
    assert mainnet_watch_problems(WATCHED, "https://deadman.example/check") == []


@pytest.mark.parametrize(
    ("urls", "deadman", "missing"),
    [
        (WATCHED[:1], "https://deadman.example/check", ["two alert channels"]),
        ((), "https://deadman.example/check", ["two alert channels"]),
        (("", WATCHED[1]), "https://deadman.example/check", ["two alert channels"]),
        (WATCHED, "", [MAINNET_URL_ENV]),
        ((), "", ["two alert channels", MAINNET_URL_ENV]),
    ],
)
def test_each_missing_watch_is_named(urls: tuple[str, ...], deadman: str, missing: list[str]) -> None:
    problems = mainnet_watch_problems(urls, deadman)
    assert len(problems) == len(missing)
    assert all(word in problem for word, problem in zip(missing, problems, strict=True))
    assert not any("example" in problem for problem in problems), "names only: the URLs are tokens"


def test_each_loop_pings_its_own_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOOP_URL_ENV, "https://deadman.example/demo")
    monkeypatch.setenv(MAINNET_URL_ENV, "https://deadman.example/mainnet")
    assert MAINNET_URL_ENV != LOOP_URL_ENV
    assert loop_url() == "https://deadman.example/demo"
    assert loop_url(MAINNET_URL_ENV) == "https://deadman.example/mainnet"


def test_live_run_asks_before_a_mainnet_start() -> None:
    """The refusal itself is two lines in `live_run`; what needs the source is that it is asked at all."""
    source = inspect.getsource(live_run.callback)
    assert "mainnet_watch_problems(alerts.urls, deadman_url)" in source
    assert "loop_url(MAINNET_URL_ENV if account_gate(payload) is not None else LOOP_URL_ENV)" in source
