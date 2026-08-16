"""launchd plist 治理测试（M00-F07，P0-02）。

覆盖: 模板/实装漂移检测、wrapper 终态退出码映射（LOCKED/FAILED 不重启）、
模板本身的受控重启语义（KeepAlive 不得为 True）。
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

from beidou_launcher.preflight import _launchd_plist_drift

ROOT = Path(__file__).resolve().parent.parent.parent


def _write_plist(path: Path, payload: dict) -> None:
    with open(path, "wb") as f:
        plistlib.dump(payload, f)


def _installed_payload(*, keep_alive: object = True, throttle: int = 10, with_eval: bool = True) -> dict:
    args = [
        "/bin/zsh",
        "-c",
        "eval \"$(grep '^export BEIDOU_' ~/.zshrc)\" && /opt/homebrew/bin/beidou start",
    ]
    if not with_eval:
        args = ["/opt/homebrew/bin/beidou", "start", "--mode", "testnet"]
    return {
        "Label": "com.beidou.autopilot",
        "ProgramArguments": args,
        "KeepAlive": keep_alive,
        "ThrottleInterval": throttle,
    }


def test_drift_detects_keepalive_true_and_shell_eval(tmp_path: Path) -> None:
    installed = tmp_path / "com.beidou.autopilot.plist"
    _write_plist(installed, _installed_payload(keep_alive=True, throttle=10, with_eval=True))
    drift, evidence = _launchd_plist_drift(ROOT, installed_path=installed)
    assert any("KeepAlive=true" in item for item in drift)
    assert any("eval" in item for item in drift)
    assert any("ThrottleInterval" in item for item in drift)
    assert evidence["installed"] is True


def test_no_installed_plist_is_not_drift(tmp_path: Path) -> None:
    drift, evidence = _launchd_plist_drift(ROOT, installed_path=tmp_path / "nonexistent.plist")
    assert drift == []
    assert evidence["installed"] is False


def test_compliant_installed_plist_has_no_drift(tmp_path: Path) -> None:
    installed = tmp_path / "com.beidou.autopilot.plist"
    _write_plist(
        installed,
        {
            "Label": "com.beidou.autopilot",
            "ProgramArguments": [
                str(ROOT / "deploy" / "beidou_launchd_wrapper.sh"),
                "/opt/homebrew/bin/beidou",
                "start",
            ],
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 30,
        },
    )
    drift, _evidence = _launchd_plist_drift(ROOT, installed_path=installed)
    assert drift == []


def _run_wrapper_with_exit(code: int) -> int:
    # 测试运行固定路径的仓库 wrapper,参数为常量退出码 —— 无不可信输入。
    return subprocess.run(  # noqa: S603
        [str(ROOT / "deploy" / "beidou_launchd_wrapper.sh"), "/bin/sh", "-c", f"exit {code}"],
        check=False,
        capture_output=True,
    ).returncode


def test_wrapper_maps_only_locked_to_zero() -> None:
    """仅 LOCKED(5) 映射为 0 —— 终态不重启（M00-F07-R2 与 supervisor 真实返回码对齐）。"""
    assert _run_wrapper_with_exit(5) == 0


def test_wrapper_preserves_other_exit_codes() -> None:
    """启动失败(4)/引擎失败(6)/崩溃(其他非零)透传 —— launchd SuccessfulExit=false 据此重启。"""
    assert _run_wrapper_with_exit(4) == 4
    assert _run_wrapper_with_exit(6) == 6
    assert _run_wrapper_with_exit(3) == 3
    assert _run_wrapper_with_exit(0) == 0


def test_template_plist_uses_governed_restart_semantics() -> None:
    """模板为安全默认(safety_only):完全禁用自动启动/重启。

    合并语义(codex/full-system-optimization): 受控重启 wrapper 由
    testnet 实装 plist(用户 LaunchAgents)承担,模板本身 KeepAlive
    False + RunAtLoad False —— 比 KeepAlive={SuccessfulExit:false}
    更强的防重启循环语义。
    """
    with open(ROOT / "deploy" / "com.beidou.autopilot.plist", "rb") as f:
        template = plistlib.load(f)
    keep_alive = template.get("KeepAlive")
    assert keep_alive is False, "模板必须完全禁用自动重启(safety_only 默认)"
    assert template.get("RunAtLoad") is False, "模板不得开机自动拉起"
    assert template.get("EnvironmentVariables", {}).get("BEIDOU_ENV") == "safety_only"
    args = template["ProgramArguments"]
    assert "eval" not in " ".join(str(a) for a in args), "模板不得 shell eval"
    wrapper = ROOT / "deploy" / "beidou_launchd_wrapper.sh"
    assert wrapper.is_file() and wrapper.stat().st_mode & 0o111, "wrapper 缺失或不可执行(实装使用)"
