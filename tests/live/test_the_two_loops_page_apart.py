"""demo 与 mainnet 两个循环共用告警通道和去重文件，所以每条 mainnet 告警要说自己是谁（切换方案 M6，2026-10-19）。

不加区分会出两件事：mainnet 的告警读起来和 demo 的一样；两个循环在同一个窗口里发同一个 key（`second-instance`、
`check-status`、`equity-cap`……），后发的那条被当成重复吞掉。`.beidou/data` 下的共享记录也只能有一个写者，就是 demo。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from beidou_cli.live_cmd import alert_label, writes_the_data_root
from beidou_live.alerts import WebhookAlerts
from beidou_live.config import load_profile
from tests.live.test_the_hourly_check_pages_sooner_and_on_both_channels import CHECK, DATA, _function, _run, _serve

ROOT = Path(__file__).resolve().parents[2]
DEMO = load_profile(ROOT / "config" / "live.demo.yaml")
MAINNET = load_profile(ROOT / "config" / "live.mainnet.yaml")


def _channel(state: Path, sent: list[str], label: str) -> WebhookAlerts:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content.decode("utf-8"))
        return httpx.Response(200, json={"code": 0})

    return WebhookAlerts(
        "https://hooks.example/beidou", state_path=state, label=label, transport=httpx.MockTransport(handler)
    )


def test_only_the_mainnet_profile_gets_a_label() -> None:
    assert alert_label(MAINNET) == "实盘"
    assert alert_label(DEMO) == ""


async def test_the_same_key_from_both_loops_is_two_pages(tmp_path: Path) -> None:
    sent: list[str] = []
    state = tmp_path / "alert-dedup.json"
    assert await _channel(state, sent, "").send("北斗：已有一个实例在交易这个账户", key="second-instance")
    assert await _channel(state, sent, "实盘").send("北斗：已有一个实例在交易这个账户", key="second-instance")
    assert len(sent) == 2
    assert "【实盘】北斗：" in sent[1] and "【实盘】" not in sent[0], "demo's text is unchanged"
    # and each loop still dedups against itself
    assert not await _channel(state, sent, "实盘").send("北斗：已有一个实例在交易这个账户", key="second-instance")


async def test_a_labelled_clear_and_recently_sent_read_the_labelled_key(tmp_path: Path) -> None:
    sent: list[str] = []
    mainnet = _channel(tmp_path / "state.json", sent, "实盘")
    assert await mainnet.send("北斗日报 2026-10-19：x")
    assert mainnet.recently_sent("北斗日报 2026-10-19：x")
    assert not _channel(tmp_path / "state.json", sent, "").recently_sent("北斗日报 2026-10-19：x")
    assert await mainnet.send("权益超过上限", key="equity-cap")
    mainnet.clear("equity-cap")
    assert await mainnet.send("权益超过上限", key="equity-cap"), "the condition cleared, so the next one fires"


def test_only_demo_writes_the_data_root() -> None:
    assert writes_the_data_root(DEMO, trades=True) is True
    assert writes_the_data_root(MAINNET, trades=True) is False, "mainnet trades its account and only reads .beidou/data"
    assert writes_the_data_root(DEMO, trades=False) is False


@pytest.fixture
def webhook() -> Iterator[tuple[str, list[str]]]:
    yield from _serve()


def test_the_hourly_check_pages_mainnet_under_its_own_name(tmp_path: Path, webhook: tuple[str, list[str]]) -> None:
    url, received = webhook
    result, _ = _run(
        tmp_path,
        [_function(CHECK, "notify"), "NOTIFY_LABEL=实盘", 'notify "mainnet-status" "心跳已过期"'],
        BEIDOU_ALERTS_WEBHOOK_URL=url,
    )
    assert result.returncode == 0, result.stderr
    assert received == ["【实盘】北斗巡检失败（mainnet-status）：心跳已过期"]


def test_the_hourly_check_reads_mainnet_only_once_its_plist_is_installed() -> None:
    lines = [line.strip() for line in CHECK.read_text(encoding="utf-8").splitlines()]
    gate = lines.index('if [ -f "$HOME/Library/LaunchAgents/com.beidou.live-mainnet.plist" ]; then')
    block = lines[gate : lines.index("fi", gate)]
    assert block[1:6] == [
        "PROFILE=config/live.mainnet.yaml",
        "PREFIX=mainnet-",
        "NOTIFY_LABEL=实盘",
        "loop_checks",
        "check_report",
    ]
    assert block[6:] == ["PROFILE=", "PREFIX=", "NOTIFY_LABEL="], "the checks after it read demo again"


def test_the_nightly_job_keeps_mainnet_copies_apart() -> None:
    text = DATA.read_text(encoding="utf-8")
    assert '"$BEIDOU" live backup --profile config/live.mainnet.yaml --to "$SUPPORT/backup-mainnet"' in text
