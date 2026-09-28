"""shell 脚本里，`$NAME` 后面不许紧跟非 ASCII 字符，一律写 `${NAME}`（2026-09-29）。

macOS 自带的 /bin/bash 是 3.2.57，本仓库的 hook 与 launcher 都由它跑（shebang 全是 `#!/bin/bash`）。
UTF-8 locale 下，它会把 `$NAME` 后面那个多字节字符的首字节读进变量名。`echo "（退出码 $rc）"` 里的
`）` 是 EF BC 89，名字就读成 `rc\\xEF`。开了 `set -u` 的脚本当场报 `rc�: unbound variable` 退出；
没开时展开成空串，剩下两个字节成了乱码。写成 `${rc}` 就没事，C locale 下也没事。

这些脚本全开 `set -u`，提示文字又大量用中文标点，所以这条错会反复长出来。09-29 先在 pre-push 里
撞上一次，同日又扫出 deploy 里两处：`run_forward_board.sh` 的 `（$BOARD）`，`run_governance_gate.sh`
的 `$rc，`。launchd 的环境里没有 LANG，定时任务不会因此崩。在 UTF-8 终端里手动跑才崩，而手动跑
往往就在出事之后。

检查只读文本，不依赖哪个 bash，所以 CI 上也跑。CI 的 Linux bash 会不会复现，没核过。
单引号与带引号的 heredoc 里不展开，这条照样要求。`${NAME}` 在那里无害，规则不看上下文，也就没有例外可争。

已知的边界：以 `#` 开头的行整行跳过。多行双引号串或不带引号的 heredoc 里以 `#` 开头的行，也一起跳过了。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: `$NAME` 紧跟一个非 ASCII 字符。`$1`、`$?` 这类参数名只有一个字符，bash 不往后读，不在此列。
UNBRACED = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]")
SHEBANG = re.compile(rb"^#!.*\b(?:ba)?sh\b")


def _is_shell(path: Path) -> bool:
    if path.suffix == ".sh":
        return True
    with path.open("rb") as handle:
        first_line = handle.read(128).split(b"\n", 1)[0]
    return bool(SHEBANG.match(first_line))


def _scripts() -> list[Path]:
    """git 与 launchd 交给 bash 的脚本：hook 没有后缀，launcher 都在 deploy/ 下。"""
    candidates = [*ROOT.glob(".githooks/*"), *ROOT.glob("deploy/*")]
    return sorted(path for path in candidates if path.is_file() and _is_shell(path))


SCRIPTS = _scripts()


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """不以 `#` 开头的行。注释常引用出过错的写法，而注释不执行。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [(number, line) for number, line in enumerate(lines, 1) if not line.lstrip().startswith("#")]


def test_the_text_check_catches_the_forms_that_failed() -> None:
    for failed in ('echo "（退出码 $rc）"', 'echo "板是空的（$BOARD）——"', 'detail="退出码 $rc，且没有任何输出"'):
        assert UNBRACED.search(failed), failed
    for fine in ('echo "（退出码 ${rc}）"', 'echo "（$1）（$?）（$#）"', 'echo "$rc ，"', 'echo "[$(stamp)] 板是空的"'):
        assert not UNBRACED.search(fine), fine


def test_the_scan_covers_the_hooks_and_the_launchers() -> None:
    """扫一个空 glob 的守卫永远是绿的，所以点名它要覆盖的文件。"""
    assert {path.relative_to(ROOT).as_posix() for path in SCRIPTS} >= {
        ".githooks/pre-commit",
        ".githooks/pre-push",
        "deploy/run_check.sh",
        "deploy/run_forward_board.sh",
        "deploy/run_governance_gate.sh",
        "deploy/run_live.sh",
    }


def test_every_tracked_shell_script_is_in_the_scan() -> None:
    """脚本放进别的目录，上面的 glob 就看不见它。git 知道全部被跟踪的文件，拿它对一遍。"""
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=False)
    if listed.returncode != 0:
        pytest.skip("不在 git checkout 里，列不出被跟踪的文件")
    tracked = (ROOT / name for name in listed.stdout.decode("utf-8").split("\0") if name)
    missing = sorted(
        path.relative_to(ROOT).as_posix()
        for path in tracked
        if path.is_file() and path not in SCRIPTS and _is_shell(path)
    )
    assert not missing, f"{missing} 是 shell 脚本，但不在 SCRIPTS 的 glob 里：把它的目录加进 _scripts()"


def test_no_shell_script_runs_a_variable_into_a_non_ascii_character() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{number}: {line.strip()}"
        for path in SCRIPTS
        for number, line in _code_lines(path)
        if UNBRACED.search(line)
    ]
    assert not offenders, (
        "macOS 的 /bin/bash 3.2 在 UTF-8 locale 下会把紧跟的多字节字符读进变量名，"
        "`set -u` 时当场 unbound variable。改写成 ${NAME}：\n" + "\n".join(offenders)
    )
