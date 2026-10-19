"""mainnet 的启动器（切换方案 M5，2026-10-19）：缺什么就以 78 退出，什么都齐了才 `exec` 那一行。

跑的是启动器本身，复制进一个临时仓库：`.venv/bin/beidou` 换成一个只回显参数的桩，复制件第二行把家目录改成临时目录，
所以测试读不到操作者真实的 `~/.zshrc`（那里有真实的导出），也不会在真实的 Application Support 下建目录；而且永远不会起
一个真循环——哪天 `governance/MAINNET_ENABLED` 真的入库，这里照样只跑桩。家目录在复制件里改，而不是经 `env=` 传进去：
`test_tests_never_touch_the_real_app_support.py` 不许测试代码出现那个变量名，这样改是离真实目录更远，不是绕开它。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "deploy" / "run_live_mainnet.sh"
NEEDED = ("BEIDOU_MAINNET_API_KEY", "BEIDOU_MAINNET_API_SECRET", "BEIDOU_MAINNET_MAX_EQUITY_USDT")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "deploy").mkdir(parents=True)
    shebang, rest = LAUNCHER.read_text(encoding="utf-8").split("\n", 1)
    copy = root / "deploy" / LAUNCHER.name
    copy.write_text(f'{shebang}\nexport HOME="{tmp_path / "home"}"\n{rest}', encoding="utf-8")
    stub = root / ".venv" / "bin" / "beidou"
    stub.parent.mkdir(parents=True)
    stub.write_text('#!/bin/bash\necho "ARGS $*"\n', encoding="utf-8")
    stub.chmod(0o755)
    (root / "governance").mkdir()
    (tmp_path / "home").mkdir()
    return root


def _run(repo: Path, **env: str) -> subprocess.CompletedProcess[str]:
    clean = {key: value for key, value in os.environ.items() if not key.startswith("BEIDOU_")}
    return subprocess.run(
        ["bash", str(repo / "deploy" / LAUNCHER.name), "--cycles", "1"],
        env={**clean, **env},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("missing", NEEDED)
def test_each_missing_variable_is_a_configuration_exit(repo: Path, missing: str) -> None:
    (repo / "governance" / "MAINNET_ENABLED").write_text("x\n", encoding="utf-8")
    result = _run(repo, **{name: "set" for name in NEEDED if name != missing})
    assert result.returncode == 78
    assert f"{missing} is not in the environment" in result.stderr and "ARGS" not in result.stdout


def test_the_demo_variables_do_not_start_it(repo: Path) -> None:
    (repo / "governance" / "MAINNET_ENABLED").write_text("x\n", encoding="utf-8")
    result = _run(repo, BEIDOU_BINANCE_API_KEY="demo", BEIDOU_BINANCE_API_SECRET="demo")
    assert result.returncode == 78 and "ARGS" not in result.stdout


def test_without_the_sign_off_it_stops_before_the_loop(repo: Path) -> None:
    result = _run(repo, **dict.fromkeys(NEEDED, "set"))
    assert result.returncode == 78
    assert "MAINNET_ENABLED is absent" in result.stderr and "ARGS" not in result.stdout


def test_with_everything_in_place_it_runs_the_mainnet_profile_armed(repo: Path) -> None:
    (repo / "governance" / "MAINNET_ENABLED").write_text("x\n", encoding="utf-8")
    result = _run(repo, **dict.fromkeys(NEEDED, "set"))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "ARGS live run --profile config/live.mainnet.yaml --immediate --armed --mainnet --cycles 1"
    )


def test_the_zshrc_exports_are_read_when_there_is_no_env_file(repo: Path) -> None:
    (repo / "governance" / "MAINNET_ENABLED").write_text("x\n", encoding="utf-8")
    zshrc = "".join(f"export {name}=from-zshrc\n" for name in NEEDED)
    (repo.parent / "home" / ".zshrc").write_text(f"alias ll='ls -l'\n{zshrc}", encoding="utf-8")
    assert _run(repo).returncode == 0


def test_it_carries_no_bridge_and_the_demo_launcher_knows_nothing_of_it() -> None:
    text = LAUNCHER.read_text(encoding="utf-8")
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("allow-unvalidated" in line or "BRIDGE" in line for line in code)
    demo = (ROOT / "deploy" / "run_live.sh").read_text(encoding="utf-8")
    assert "mainnet" not in demo.lower()
