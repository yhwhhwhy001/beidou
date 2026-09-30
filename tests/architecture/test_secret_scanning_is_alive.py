"""密钥扫描这道门必须是活的，而不只是配置文件存在。

2026-09-19 发现本仓库**从 2026-08-05 创建起就是公开的**，而 `CLAUDE.md` 里一直写着
「本仓库是 Free plan 的 private repo」。认知差持续了 45 天。当天做了全历史扫描，结论
是干净的：1,637 个 commit、72 MB、所有凭据形态零命中，凭据设计本身也是对的（从
`env.sh` / `~/.zshrc` 读，代码与 plist 里都没有值）。

于是这个文件的职责不是「找密钥」——CI 的 Secrets 门每次跑都在找。它守的是**扫描本身
还有没有用**，因为这道门有五种坏法，而且都不会自己喊出来：

  1. **配置崩了，扫描根本没跑。**  建这道门的当天就撞上了：`.gitleaks.toml` 第一版用
     `(?=.*[A-Z])(?=.*[a-z])` 表达「大小写混排」，而 gitleaks 的正则是 RE2 语法，**不支持
     lookahead**。它不是报一个配置错误，是带栈回溯地 panic。在 CI 里这会红，能看见。
     本机两个 hook 此前把它报成「有疑似密钥」，panic 的原话被吞掉（2026-09-29 改，见文件末尾那组）。
     配置文件不在或写坏则是 FTL、退 1，与有命中同码。`install-hooks.sh` 的自检此前把这两种都当成
     「抓到了」、打 ✓；本文件的 `_scan` 把 FTL 读成零命中（2026-09-29 改，见文件末尾）。

  2. **allowlist 被放宽到什么都抓不住。**  这道门上线时压掉了 117 处误报（全部核实过：
     100 处是 trial ledger 的 `param_key`、9 处是 sha256 文件摘要、2 处是 SSH 公钥指纹、
     其余是测试里写死的假值）。压误报是必要的——117 个红的检查等于没有检查，人看三次
     就开始无视它。但每压一条，抓真密钥的能力就少一点。下面 `test_a_forged_credential_is_caught`
     就是这件事的刹车：allowlist 再怎么加，一个伪造的 Binance 形态必须还能被抓出来。

  3. **扫描跑了，却看不见 merge 提交。**  2026-09-28 实测：gitleaks 的 git 模式跑的是
     `git log -p`，而 `git log -p` 默认不给 merge 提交出 diff。解冲突时写进 merge 提交的
     假凭据，pre-push 与 CI 的 Secrets 门都报 `no leaks found`。上面那次「1,637 个 commit
     零命中」也不含 merge 的 diff。文件末尾那组测试守这一条：造一个只活在 merge 提交里的
     假凭据，拿 hook 本身、ci.yml 与 SECURITY.md 里现写的命令去扫。命令不在测试里另抄一份。
     抄一份的话，门上的写法改坏了，测试照绿，真门照哑。

  4. **git log 失败了，gitleaks 照样返回 0。**  gitleaks 8.30.1 只记一行 `ERR [git] fatal`，
     输出 `no leaks found`，退出码 0。pre-push 此前又把 stderr 丢进 /dev/null。这在运行时
     真撞得上：`git push --force` 盖过一个没 fetch 的远端 tip 时，remote_sha 本地没有，
     range 无效，推送不扫就放行。2026-09-29 在临时仓库里复现过，伪造凭据随强推进了远端。
     守这一条的有两处：`_run_pre_push` 的第三种推送，与文件末尾的 fail-closed 测试。

  5. **有一类凭据，没有一条规则认得它。**  2026-09-30 发现：告警 webhook 的 URL 是凭据
     （Lark/飞书自定义机器人的 token 就是 URL 的最后一段），默认规则与 `.gitleaks.toml` 都认不出。
     这里的零命中读作「没有这条规则」，不是「干净」，从输出上分不开。文件末尾那组测试守这一条：
     随机拼一条假 webhook，pre-commit 与 CI 都得把它记在 `beidou-lark-webhook-url` 名下。

测试用的「密钥」全部是当场随机生成的假值，不落在仓库里，也从未对应任何账户。
"""

from __future__ import annotations

import json
import os
import random
import re
import shlex
import shutil
import string
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".gitleaks.toml"

pytestmark = pytest.mark.skipif(
    shutil.which("gitleaks") is None,
    reason="没装 gitleaks（本机：brew install gitleaks；CI 上由 Secrets 那一步放进 PATH）",
)


def _forged_binance_credential() -> str:
    """造一个 Binance API key/secret 的形态：64 位，大小写字母与数字混排。

    随机生成而不是写死一个常量：写死的那个会被将来某次 allowlist 调整按值放行掉
    （这正是本仓库放行 `sk-1234567890abcdef` 的方式），于是测试继续绿，而真密钥
    不再被拦。每次跑都换一个值，就没有「按值放行」这条捷径。
    """
    lower = [random.choice(string.ascii_lowercase) for _ in range(28)]
    upper = [random.choice(string.ascii_uppercase) for _ in range(26)]
    digits = [random.choice(string.digits) for _ in range(10)]
    chars = lower + upper + digits
    random.shuffle(chars)
    forged = "".join(chars)
    assert len(forged) == 64
    return forged


# `_scan` 让 gitleaks 有命中时用这个码退出（`--exit-code`）。gitleaks 自己出错时退 1、2 或 126，
# 取一个离它们都远的值，与两个 hook 和 install-hooks.sh 的 `leaks_rc` 相同。
LEAKS_RC = 42


def _scan(contents: dict[str, str], config: Path | None = None) -> list[dict]:
    """把给定内容写进一个临时目录，用仓库的配置扫它，返回命中列表。

    给了 `config` 就用它代替仓库的配置。只有测 `_scan` 自己的那条用得上，见文件末尾。
    """
    config = CONFIG if config is None else config
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in contents.items():
            (Path(tmp) / name).write_text(text, encoding="utf-8")
        report = Path(tmp) / "_report.json"
        proc = subprocess.run(
            [
                "gitleaks",
                "detect",
                "--source",
                tmp,
                "--config",
                str(config),
                "--no-git",
                "--no-banner",
                "--exit-code",
                str(LEAKS_RC),
                "--report-format",
                "json",
                "--report-path",
                str(report),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        # 0 = 干净，LEAKS_RC = 有命中。别的值都是 gitleaks 自己没跑完，必须炸出来，不能当成「没命中」。
        # 配置不在或写坏是 FTL、退 1，正是 gitleaks 有命中时的默认退出码，所以要传 `--exit-code` 分开它们。
        # 规则里有 RE2 不支持的正则是 panic、退 2。2026-09-29 之前这里认 0 与 1，配置写坏也过得去，见文件末尾。
        assert proc.returncode in (0, LEAKS_RC), (
            f"gitleaks 没能正常跑完（returncode={proc.returncode}）。\n"
            "这通常意味着 .gitleaks.toml 加载失败：文件不在或写坏了是 FTL、退 1；\n"
            "规则里有 RE2 不支持的正则（例如 lookahead）是 panic、退 2。\n"
            f"stderr:\n{proc.stderr[:2000]}"
        )
        # 退出码是 0 或 LEAKS_RC 时，gitleaks 已经写了报告，干净时是 `[]`。报告不在只能是出了别的错，
        # 不能读成「没命中」。此前这里返回 []，配置写坏时的零命中就是从这条路出来的。
        assert report.exists(), f"gitleaks 退出码是 {proc.returncode}，却没写报告。\nstderr:\n{proc.stderr[:2000]}"
        return json.loads(report.read_text(encoding="utf-8") or "[]")


def test_the_config_loads_at_all() -> None:
    """配置能被 gitleaks 加载。抓的是 RE2 那个坑：panic 不是报错，容易被当成通过。"""
    assert CONFIG.exists(), f"{CONFIG} 不在了——CI 的 Secrets 门会连着一起哑掉"
    findings = _scan({"ordinary.py": "x = 1\n"})
    assert findings == [], f"一个只写了 x = 1 的文件不该有命中：{findings}"


def test_a_forged_credential_is_caught() -> None:
    """伪造的 Binance 形态必须被抓到——这是 allowlist 的刹车。

    这条红了，说明 allowlist 已经宽到扫描失去意义。修法不是改这个测试，
    是回头看最近一次往 `.gitleaks.toml` 加了什么。
    """
    forged = _forged_binance_credential()
    findings = _scan({"leak.py": f'BEIDOU_BINANCE_API_KEY = "{forged}"\n'})
    assert findings, (
        "伪造的 Binance 凭据形态没有被拦下。\n.gitleaks.toml 的 allowlist 被放得太宽，密钥扫描已经失去意义。"
    )


def test_a_credential_with_no_assignment_context_is_caught() -> None:
    """裸串也要抓到，不能只在 `KEY = "..."` 这种上下文里才响。

    这正是 `beidou-binance-credential-shape` 那条自定义规则存在的理由：内置的
    `generic-api-key` 靠赋值上下文触发，而泄漏最常见的形态是一行裸串——粘进
    注释、贴进报告、抄进 RESEARCH_LOG 的一段日志。
    """
    forged = _forged_binance_credential()
    findings = _scan({"note.md": f"循环昨天报错，日志里那行是：\n{forged}\n应该是权限问题。\n"})
    assert findings, (
        "没有赋值上下文的裸凭据没有被拦下。\n"
        "内置规则靠 `key =` 触发，自定义规则 `beidou-binance-credential-shape` "
        "就是为这个形态加的——检查它是不是被删掉或改窄了。"
    )


@pytest.mark.parametrize(
    ("name", "text"),
    [
        # 本仓库满地都是的哈希形态。任何一条误报，基线就不是零，
        # 而不是零基线的检查三天之内就会被忽略。
        ("digest.json", '{"api.py": "4c4148b58b39095d1a004b3e9af4c3715b7448765850d7140572b6805f9bb735"}\n'),
        ("trials.jsonl", '{"param_key": "207ad3fb54979ace", "trial": 1}\n'),
        ("objectid.txt", "9ca85c6900000000000000000000000000000000\n"),
    ],
)
def test_repository_hash_shapes_do_not_false_positive(name: str, text: str) -> None:
    """摘要、指纹、object id 不该报。它们是单一字符集的 hex，不可能是 Binance 凭据。"""
    findings = _scan({name: text})
    assert findings == [], f"{name} 的哈希形态被误报成密钥：{findings}\n基线必须是零，否则这道门会被当成噪声忽略掉。"


@pytest.mark.parametrize("name", ["pre-commit", "pre-push"])
def test_the_local_hooks_are_present_and_executable(name: str) -> None:
    """本地两层拦截都在仓库里，而且是可执行的。

    不可执行时 git 会**静默跳过** hook——不报错，不提示，看起来和「扫描通过」
    一模一样。

    这两层比一般项目重要：见 `test_the_docs_record_that_github_cannot_catch_binance_keys`，
    服务端那层接不住 Binance 的密钥，所以本机这两个是它进入公开仓库前仅有的拦截。
    """
    hook = ROOT / ".githooks" / name
    assert hook.exists(), f"`.githooks/{name}` 不在了，本地少了一层"
    assert hook.stat().st_mode & 0o111, (
        f"`.githooks/{name}` 没有可执行位。git 会静默跳过它，"
        f"看起来与「扫描通过」无法区分。修：chmod +x .githooks/{name}"
    )


def test_the_docs_record_that_github_cannot_catch_binance_keys() -> None:
    """这个限制必须留在文档里，因为它反直觉且代价很高。

    2026-09-19 核实：Binance **不在** GitHub secret scanning 的 partner pattern
    列表里，而 custom patterns 要求仓库属于组织并启用付费的 Secret Protection。
    个人账户下的公开仓库两条都不满足。

    为什么给一句文档写测试：这条限制不写出来，读者的默认假设恰好是相反的
    （「开了 push protection 就有人兜底了」），而那个假设会直接导出
    「`--no-verify` 一下没关系」。它在任何一次文档重写里都可能被当成啰嗦删掉。
    """
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "partner pattern" in security, (
        "SECURITY.md 不再说明 GitHub 的 push protection 认不出 Binance 密钥。\n"
        "这条删不得：少了它，读者会以为服务端有人兜底，而实际上 Binance 密钥\n"
        "进入公开仓库之前只有本机那两个 hook 拦得住。"
    )


def test_ci_scans_full_history_not_a_shallow_checkout() -> None:
    """CI 的 Secrets 门要有完整历史才有意义。

    `actions/checkout` 默认只取一个 commit。在浅检出上跑历史扫描会安静地通过，
    而密钥进了历史之后，把文件删掉并不会让它从公开仓库里消失。
    """
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "gitleaks" in ci, "CI 里没有 Secrets 门了"
    assert "fetch-depth: 0" in ci, (
        "CI 的 checkout 没有 `fetch-depth: 0`。浅检出上的历史扫描会空跑通过，"
        "这是最难发现的一种失败：它长得和平安一模一样。"
    )


# ---------------------------------------------------------------------------
# 第三种坏法：merge 提交的 diff 不在扫描里
# ---------------------------------------------------------------------------

HOOK = ROOT / ".githooks" / "pre-push"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SECURITY = ROOT / "SECURITY.md"
Z40 = "0" * 40

# 两种 git 配置下都要抓到。第二种是修这道门时撞上的：`-m` 只说「给 merge 出 diff」，
# 格式听 `log.diffMerges` 的。它设成 `dense-combined`（或 `combined`）时，`git log -p -m`
# 出的是合并格式的 diff，gitleaks 解析不了，照样报 `no leaks found`。所以门上写
# `--diff-merges=separate`：每个 parent 各出一份普通 diff，不看这个配置。
GIT_CONFIGS: dict[str, str | None] = {
    "default-config": None,
    "log.diffMerges=dense-combined": "dense-combined",
}
PUSHES = ("existing-branch", "new-branch", "unfetched-remote-tip")

# 远端报来、本地没有的 tip。它在不在本地，git 都原样交给 hook。任取一个本地不存在的 sha 就是这个场景。
UNFETCHED_TIP = "deadbeef" * 5


def _git_env(diff_merges: str | None = None) -> dict[str, str]:
    """临时仓库里一切 git 调用的环境，包括 hook 与 gitleaks 自己拉起的 `git log`。

    不读 `~/.gitconfig` 与系统配置，也不继承外层的 `GIT_*`。在 hook 里跑 pytest 时，
    `GIT_DIR` 这类变量指向外层仓库，不清掉的话命令会落到那里去。
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "scan-test",
        "GIT_AUTHOR_EMAIL": "scan-test@example.invalid",
        "GIT_COMMITTER_NAME": "scan-test",
        "GIT_COMMITTER_EMAIL": "scan-test@example.invalid",
    }
    if diff_merges is not None:
        env |= {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "log.diffMerges", "GIT_CONFIG_VALUE_0": diff_merges}
    return env


def _git(repo: Path, *args: str, expect: int = 0) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], env=_git_env(), capture_output=True, text=True, timeout=60)
    assert proc.returncode == expect, f"git {' '.join(args)} 返回 {proc.returncode}，预期 {expect}：\n{proc.stderr}"
    return proc.stdout.strip()


@dataclass(frozen=True)
class _MergeRepo:
    """一个临时仓库，里面有一个解冲突时写进指定值的 merge 提交。"""

    path: Path
    resolution: str  # 解冲突时写进 merge 提交的值
    remote_tip: str  # 推送前远端 main 的位置，即 merge 之前 main 的 tip
    merge: str  # 那个 merge 提交
    head: str  # merge 之后的提交，把那一行又改掉了


def _build_merge_repo(path: Path, resolution: str) -> _MergeRepo:
    """base → side 与 main 改同一行 → merge 冲突 → 解冲突写进 `resolution` → 再提交一次改掉它。

    最后那次提交是有意的，它让值**只**活在 merge 提交的 diff 里。工作树里已经没有它，
    `--no-git` 的目录扫描抓不到。gitleaks 只扫新增行，那次删除也不会被扫到。
    能抓到它的，只剩真去看 merge diff 的扫描。

    建完把 HEAD 放回 base，也是有意的。全历史扫描要扫全部 ref，不只是 HEAD 走得到的那些。
    命令传了 `--log-opts` 却忘了写回 `--all` 时，git 只扫 HEAD，这里就看不见那个 merge。
    """
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    settings = path / "settings.py"

    def commit(value: str, message: str) -> str:
        settings.write_text(f'note = "{value}"\n', encoding="utf-8")
        _git(path, "add", "settings.py")
        _git(path, "commit", "-q", "-m", message)
        return _git(path, "rev-parse", "HEAD")

    base = commit("base", "base")
    _git(path, "checkout", "-q", "-b", "side")
    commit("side", "side")
    _git(path, "checkout", "-q", "main")
    remote_tip = commit("main", "main")
    _git(path, "merge", "-q", "side", expect=1)  # 两边改了同一行：冲突
    merge = commit(resolution, "merge side")
    assert len(_git(path, "rev-list", "--parents", "-n", "1", merge).split()) == 3, "解完冲突的提交应当有两个 parent"
    head = commit("cleanup", "cleanup")
    _git(path, "checkout", "-q", "--detach", base)
    # 远端「已经有」的 main。hook 对新分支扫 `local --not --remotes`，没有它就成了扫整个历史。
    _git(path, "update-ref", "refs/remotes/origin/main", remote_tip)
    # hook 与 ci.yml 都按仓库根的相对位置找配置。不提交它，扫 git 历史时它不在视野里。
    shutil.copy(CONFIG, path / ".gitleaks.toml")
    return _MergeRepo(path=path, resolution=resolution, remote_tip=remote_tip, merge=merge, head=head)


@pytest.fixture(scope="module")
def leaky_repo() -> Iterator[_MergeRepo]:
    with tempfile.TemporaryDirectory() as tmp:
        yield _build_merge_repo(Path(tmp) / "leaky", _forged_binance_credential())


@pytest.fixture(scope="module")
def clean_repo() -> Iterator[_MergeRepo]:
    with tempfile.TemporaryDirectory() as tmp:
        yield _build_merge_repo(Path(tmp) / "clean", "resolved")


def _run_pre_push(repo: _MergeRepo, push: str, diff_merges: str | None = None) -> subprocess.CompletedProcess[str]:
    """照 git 的方式调用仓库里那个 hook：参数是远端名与 URL，stdin 每行一个要推送的 ref。

    三种推送走 hook 里三个不同的分支：已有分支扫 `remote..local`，新分支扫
    `local --not --remotes`，远端 tip 本地没有时退回新分支的扫法。range 在各处分别拼。
    哪天有人把 `--diff-merges` 挪进其中一处，其余推送就又看不见 merge。

    第三种是 2026-09-29 补的。`git push --force` 盖过一个没 fetch 的远端 tip 时，git 把
    远端报来的 sha 原样交给 hook，本地没有这个提交。不加 `--force` 也一样交过来：git 把这
    一行标成 fetch first，hook 跑完才拒。此前 hook 照样拼 `remote..local`，git log 因 range
    无效而 fatal，gitleaks 照样返回 0，推送不扫就放行。
    """
    if push == "existing-branch":
        line = f"refs/heads/main {repo.head} refs/heads/main {repo.remote_tip}\n"
    elif push == "new-branch":
        line = f"refs/heads/topic {repo.head} refs/heads/topic {Z40}\n"
    else:
        assert push == "unfetched-remote-tip", push
        line = f"refs/heads/main {repo.head} refs/heads/main {UNFETCHED_TIP}\n"
    return subprocess.run(
        [str(HOOK), "origin", "https://example.invalid/beidou.git"],
        input=line,
        cwd=repo.path,
        env=_git_env(diff_merges),
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize("diff_merges", list(GIT_CONFIGS.values()), ids=list(GIT_CONFIGS))
@pytest.mark.parametrize("push", PUSHES)
def test_pre_push_blocks_a_credential_that_only_lives_in_a_merge_commit(
    leaky_repo: _MergeRepo, push: str, diff_merges: str | None
) -> None:
    """解冲突时写进 merge 提交的假凭据，pre-push 必须拦下。

    跑的是 `.githooks/pre-push` 本身，不是测试里抄的命令：hook 改了写法，这里跟着变。
    这个场景 pre-commit 本来也能抓——`git commit` 结束 merge 时它扫暂存区。pre-push
    要接住的是 pre-commit 没跑的那些：`--no-verify`，或者提交时 hook 还没装。
    """
    proc = _run_pre_push(leaky_repo, push, diff_merges)
    assert proc.returncode == 1, (
        f"只活在 merge 提交里的假凭据被 pre-push 放过去了（{push}，returncode={proc.returncode}）。\n"
        "gitleaks 的 git 模式跑 `git log -p`，它默认不给 merge 提交出 diff。hook 的 `--log-opts`\n"
        "要带 `--diff-merges=separate`；换成 `-m` 的话，log.diffMerges 一设成合并格式就又哑了。\n"
        "推送是 unfetched-remote-tip 时先看另一处：remote_sha 本地没有，hook 要退回 `local --not --remotes`。\n"
        f"stderr:\n{proc.stderr[-2000:]}"
    )


@pytest.mark.parametrize("push", PUSHES)
def test_pre_push_lets_the_same_merge_through_without_a_credential(clean_repo: _MergeRepo, push: str) -> None:
    """对照：同样的 merge，解冲突写的是普通值，hook 必须放行。

    没有这一条，上面那条分不清「看见了 merge 里的假凭据」和「hook 什么都拦」。后者真会
    发生：gitleaks 找不到配置时退出码也是 1，而 hook 在 git log 失败时也拦。

    unfetched-remote-tip 这一格还守着另一件事。hook 若只会 fail-closed、不核 remote_sha，
    这里的 git log 会因 range 无效而失败，干净的推送也被拦下。
    """
    proc = _run_pre_push(clean_repo, push)
    assert proc.returncode == 0, f"干净的 merge 被 pre-push 拦下了（{push}）：\n{proc.stdout}{proc.stderr[-2000:]}"
    # hook 的 stdout 只留给命中清单，见文件末尾那组。干净的推送在这里出声，就是每推一次响一次，
    # 推送的人很快就不再读 hook 的输出了。
    assert proc.stdout == "", f"干净的推送往 stdout 打了东西（{push}）：\n{proc.stdout[-2000:]}"


def _history_scans(script: str) -> list[list[str]]:
    """从一段 shell 里挑出「用 gitleaks 扫 git 历史」的命令，切成 argv。

    `--no-git`（扫目录）与 `protect`（扫暂存区）不算，它们本来就不看历史。
    """
    scans = []
    for line in script.replace("\\\n", " ").splitlines():
        if "gitleaks" not in line:
            continue
        argv = shlex.split(line, comments=True)
        runs_gitleaks = len(argv) >= 2 and Path(argv[0]).name == "gitleaks"
        if runs_gitleaks and argv[1] in {"detect", "git"} and "--no-git" not in argv:
            scans.append(argv)
    return scans


def _ci_history_scans() -> list[list[str]]:
    """ci.yml 里 Secrets 门真正执行的那条命令。"""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return [
        argv
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        for argv in _history_scans(step.get("run", ""))
    ]


def _documented_audit_scans() -> list[list[str]]:
    """SECURITY.md「手动全量审计」给出的命令。「基线是零」就是拿它量的。"""
    text = SECURITY.read_text(encoding="utf-8")
    blocks = re.findall(r"^```[a-z]*\n(.*?)^```", text, flags=re.MULTILINE | re.DOTALL)
    return [argv for block in blocks for argv in _history_scans(block)]


HISTORY_SCANS = {"ci.yml": _ci_history_scans, "SECURITY.md": _documented_audit_scans}


def _run_history_scan(
    argv: list[str], repo: _MergeRepo, diff_merges: str | None = None
) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    """在临时仓库根目录原样执行那条命令，只换掉可执行文件的路径、追加一份 JSON 报告。

    CI 里是 `/tmp/gitleaks`，这里用 PATH 上那个。命令里的 `--source .` 与
    `--config .gitleaks.toml` 是相对路径，cwd 设在临时仓库，它们就指向临时仓库。
    """
    gitleaks = shutil.which("gitleaks")
    assert gitleaks is not None
    with tempfile.TemporaryDirectory() as out:
        report = Path(out) / "report.json"
        proc = subprocess.run(
            [gitleaks, *argv[1:], "--report-format", "json", "--report-path", str(report)],
            cwd=repo.path,
            env=_git_env(diff_merges),
            capture_output=True,
            text=True,
            timeout=120,
        )
        findings = json.loads(report.read_text(encoding="utf-8") or "[]") if report.exists() else []
    return proc, findings


@pytest.mark.parametrize("diff_merges", list(GIT_CONFIGS.values()), ids=list(GIT_CONFIGS))
@pytest.mark.parametrize("source", list(HISTORY_SCANS))
def test_full_history_scans_see_a_credential_that_only_lives_in_a_merge_commit(
    leaky_repo: _MergeRepo, source: str, diff_merges: str | None
) -> None:
    """CI 的 Secrets 门与文档里的手动审计命令，都得看见只活在 merge 提交里的假凭据。

    CI 这一层要接住的，正是本机两个 hook 都没跑的情况。最典型的是在 GitHub 网页上
    解冲突：merge 提交直接生在远端，本机一个 hook 都不经过。
    """
    scans = HISTORY_SCANS[source]()
    assert scans, f"{source} 里找不到用 gitleaks 扫 git 历史的命令。是门被删了，还是换了写法？"
    for argv in scans:
        proc, findings = _run_history_scan(argv, leaky_repo, diff_merges)
        assert {(f["Commit"], f["File"]) for f in findings} == {(leaky_repo.merge, "settings.py")}, (
            f"{source} 的 `{shlex.join(argv)}` 没看见 merge 提交里的假凭据"
            f"（returncode={proc.returncode}，{len(findings)} 处命中）。\n"
            "传了 `--log-opts` 就整个替换掉 gitleaks 默认的 `--full-history --all`，要把它们写回来，\n"
            "再加 `--diff-merges=separate`。换成 `-m` 的话，log.diffMerges 一设成合并格式就又哑了。\n"
            f"stderr:\n{proc.stderr[-2000:]}"
        )
        assert proc.returncode != 0, f"{source} 的命令看见了命中却返回 0，CI 那一步不会变红"


@pytest.mark.parametrize("source", list(HISTORY_SCANS))
def test_full_history_scans_pass_the_same_merge_without_a_credential(clean_repo: _MergeRepo, source: str) -> None:
    """对照：同样的 merge 换成普通值，扫描必须干净。上面那条的命中来自假凭据，不来自别的。"""
    for argv in HISTORY_SCANS[source]():
        proc, findings = _run_history_scan(argv, clean_repo)
        assert (proc.returncode, findings) == (0, []), (
            f"干净的 merge 在 {source} 的 `{shlex.join(argv)}` 下不干净：\n{findings}\n{proc.stderr[-2000:]}"
        )


# ---------------------------------------------------------------------------
# 第四种坏法：git log 失败，gitleaks 照样返回 0
# ---------------------------------------------------------------------------

# git 解析不了的 `log.diffMerges`。只有 `git log` 读这个键，hook 里其它 git 调用不受影响。
# 所以它让「git log 失败」单独发生，也不用改 hook。
UNPARSEABLE_DIFF_MERGES = "no-such-format"


@pytest.mark.parametrize("push", PUSHES)
def test_pre_push_fails_closed_when_git_log_fails(clean_repo: _MergeRepo, push: str) -> None:
    """git log 失败时 pre-push 必须拦下，哪怕要推的东西是干净的。

    gitleaks 8.30.1 在 git log 失败时只记一行 `ERR [git] fatal: ...`，照样输出
    `no leaks found`、返回 0。「一个 commit 都没扫」和「扫过了，干净」从退出码上分不开。
    2026-09-29 撞上的是 remote_sha 本地没有，那个口子由 `_run_pre_push` 的第三种推送守着。
    这一条守的是下一个：`--log-opts` 拼坏、ref 坏了、缺对象，都该拦下而不是放行。

    用干净的仓库，是为了让拦下只剩一个原因：扫描没跑完。用哪种方式让 git log 失败都行。
    """
    proc = _run_pre_push(clean_repo, push, UNPARSEABLE_DIFF_MERGES)
    assert proc.returncode == 1, (
        f"git log 失败了，pre-push 却放行（{push}，returncode={proc.returncode}）。\n"
        "gitleaks 这时返回 0，hook 要看它的 stderr：出现 `[git]` 就当扫描失败。\n"
        f"stderr:\n{proc.stderr[-2000:]}"
    )
    # 查 gitleaks 转来的那一行本身。只查 `[git]` 不够：hook 的拦截框里也写着这几个字。
    assert "[git] fatal:" in proc.stderr, (
        f"pre-push 拦下了，却没把 git 的原话给推送的人（{push}）。只看到拦截，不知道该修什么。\n"
        f"stderr:\n{proc.stderr[-2000:]}"
    )


# ---------------------------------------------------------------------------
# pre-commit 被拦时说的话
# ---------------------------------------------------------------------------
#
# 提交被拦的那一刻，操作者正在想要不要 `--no-verify`。这时 hook 说的话会被照着做，
# 所以每一句都得是真的。下面两句在 2026-09-29 之前都不是，这组测试守的就是它们：
#
#   - 「上面列出了命中的文件与行号」。屏幕上其实什么也没列。gitleaks 不带 `--verbose` 时
#     只在 stderr 报一句 `leaks found`，而 hook 把 stderr 丢进了 /dev/null。
#   - 「GitHub 的 push protection 仍然会在 push 时拦住」。对 Binance 密钥这不成立，
#     理由见上面的 `test_the_docs_record_that_github_cannot_catch_binance_keys`。
#
# 在这之前也没有一条测试真跑过 pre-commit，只查了它在不在、能不能执行。

PRE_COMMIT = ROOT / ".githooks" / "pre-commit"


def _run_pre_commit(staged: str, fault: str | None = None) -> subprocess.CompletedProcess[str]:
    """在临时仓库里暂存一个内容为 `staged` 的文件，照 git 的方式跑仓库里那个 pre-commit。

    跑的是 hook 本身，不是测试里抄的命令，理由同 pre-push 那组。给了 `fault` 就照
    `_break_config` 弄坏配置，让 gitleaks 自己出错，见文件末尾那组。
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q", "-b", "main")
        # hook 按仓库根找配置。只拷不暂存，配置本身不在这次要扫的 diff 里。
        shutil.copy(CONFIG, repo / ".gitleaks.toml")
        if fault is not None:
            _break_config(repo, fault)
        (repo / "leak.py").write_text(staged, encoding="utf-8")
        _git(repo, "add", "leak.py")
        return subprocess.run([str(PRE_COMMIT)], cwd=repo, env=_git_env(), capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module")
def blocked_commit() -> tuple[str, subprocess.CompletedProcess[str]]:
    """暂存区里有一个假凭据时，pre-commit 的退出码与屏幕输出。"""
    forged = _forged_binance_credential()
    return forged, _run_pre_commit(f'BEIDOU_BINANCE_API_KEY = "{forged}"\n')


def test_pre_commit_blocks_a_staged_credential_and_lists_it_redacted(
    blocked_commit: tuple[str, subprocess.CompletedProcess[str]],
) -> None:
    """假凭据要拦下，命中的文件要列出来，值要脱敏。

    列出来是被拦时那段提示的前提。它的第 1 步是「先看它是不是真的密钥」，
    没有列表就无从看起，剩下的只有猜，或者 `--no-verify`。
    """
    forged, proc = blocked_commit
    shown = proc.stdout + proc.stderr
    assert proc.returncode == 1, (
        f"暂存区里的假凭据没被 pre-commit 拦下（returncode={proc.returncode}）：\n{shown[-2000:]}"
    )
    assert "leak.py" in shown, (
        "pre-commit 拦下了，屏幕上却没列出命中的文件。提示说「上面列出了命中的文件与行号」，\n"
        "照着去看的人看到的是空白。gitleaks 要带 `--verbose` 才把命中打到 stdout。\n"
        f"屏幕上实际是：\n{shown[-2000:]}"
    )
    assert forged not in shown, "pre-commit 把假凭据的原值打到了屏幕上。检查 hook 里的 `--redact` 还在不在。"


def test_the_blocked_prompt_says_push_protection_cannot_catch_binance_keys(
    blocked_commit: tuple[str, subprocess.CompletedProcess[str]],
) -> None:
    """被拦时打印的提示，要把「后面没有网」说出来。

    这是操作者想要不要 `--no-verify` 的时刻，而默认假设恰好是反的：「开了 push protection，
    后面有人兜底」。提示不说破，这个假设就会导出「绕一下没关系」。
    """
    _, proc = blocked_commit
    shown = proc.stdout + proc.stderr
    assert "partner pattern" in shown, (
        "pre-commit 被拦时的提示没说 GitHub 的 push protection 认不出 Binance 密钥。\n"
        f"操作者正要决定用不用 `--no-verify`，这句删不得。屏幕上实际是：\n{shown[-2000:]}"
    )


def test_pre_commit_lets_ordinary_content_through() -> None:
    """对照：普通内容必须放行。

    没有这一条，上面两条分不清「看见了假凭据」和「hook 什么都拦」。配置找不到时 gitleaks
    的退出码也是 1，hook 照样拦下，只是报的是「扫描没有跑完」。
    """
    proc = _run_pre_commit("x = 1\n")
    assert proc.returncode == 0, f"普通内容被 pre-commit 拦下了：\n{(proc.stdout + proc.stderr)[-2000:]}"


def _paragraphs(text: str) -> list[str]:
    """按空行切段。注释行先去掉开头的 `#`，头注与打印出来的提示按同一种段落读。"""
    paragraphs: list[str] = []
    current: list[str] = []
    for line in [*text.splitlines(), ""]:
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            current.append(stripped)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    return paragraphs


@pytest.mark.parametrize("name", ["pre-commit", "pre-push"])
def test_the_local_hooks_never_offer_push_protection_as_a_backstop(name: str) -> None:
    """hook 里每一段提到 push protection 的话，都要在同一段里说它认不出 Binance 的密钥。

    头注、缺 gitleaks 时的提示、被拦时的提示都算。pre-commit 在 2026-09-29 之前三处都把
    push protection 说成后面的兜底。只查整个文件里有没有 `partner pattern` 不够：
    说明写在头注里，打印出来的那段照样可以说错。
    """
    text = (ROOT / ".githooks" / name).read_text(encoding="utf-8")
    stale = [p for p in _paragraphs(text) if "push protection" in p and "partner pattern" not in p]
    assert not stale, (
        f"`.githooks/{name}` 里有段落提到 push protection，却没说它认不出 Binance 密钥：\n\n"
        + "\n\n".join(stale)
        + "\n\n对 Binance 密钥，push protection 不是一层网（见 SECURITY.md 第二节）。"
        "提示里把它说成兜底，读的人就会得出「`--no-verify` 一下没关系」。"
    )


# ---------------------------------------------------------------------------
# pre-push 被拦时说的话
# ---------------------------------------------------------------------------
#
# 拦截框让推送的人「`git rebase -i` 改掉那个 commit」。2026-09-29 补 `--verbose` 之前，屏幕上
# 既没有那个 commit，也没有文件。gitleaks 不带 `--verbose` 时只在 stderr 报一句 `leaks found`，
# 而 hook 截下 stderr 之后只转带 `[git]` 的行。这是上面 pre-commit 那组的同一个缺口。


@pytest.mark.parametrize("push", PUSHES)
def test_pre_push_lists_the_blocked_commit_and_file_redacted(leaky_repo: _MergeRepo, push: str) -> None:
    """被拦时要列出命中的 commit 与文件，值要脱敏。

    判据只用命中清单里才有的串：merge 提交的 sha 与文件名。「commit」「文件」这类词
    拦截框里本来就有，拿它们当判据，清单没了照样绿。
    """
    proc = _run_pre_push(leaky_repo, push)
    shown = proc.stdout + proc.stderr
    assert proc.returncode == 1, f"假凭据没被 pre-push 拦下（{push}，returncode={proc.returncode}）：\n{shown[-2000:]}"
    assert leaky_repo.merge in shown and "settings.py" in shown, (
        f"pre-push 拦下了，屏幕上却没列出命中的 commit 与文件（{push}）。拦截框让人改掉「那个 commit」，\n"
        "照着做的人不知道是哪个。gitleaks 要带 `--verbose` 才把命中打到 stdout，hook 再经 fd 3 交给终端。\n"
        f"屏幕上实际是：\n{shown[-2000:]}"
    )
    assert leaky_repo.resolution not in shown, (
        "pre-push 把假凭据的原值打到了屏幕上。检查 hook 里的 `--redact` 还在不在。"
    )


# ---------------------------------------------------------------------------
# gitleaks 自己出错时说的话
# ---------------------------------------------------------------------------
#
# 配置读不了、规则里有 RE2 不支持的正则，gitleaks 都是一行没扫就退出，退出码不是 0。
# 2026-09-29 之前两个 hook 只看退出码非 0，于是都出「有疑似密钥」的框。框里叫人先去
# 交易所把 key 作废重发。gitleaks 的原话却被吞了：pre-push 只转 `[git]` 行，pre-commit
# 的 stderr 进了 /dev/null。方向是 fail-closed，不漏密钥，但指示是错的。照着做的人会去
# 作废重发 key、重启实盘循环，真正的原因（配置坏了）却看不到。
#
# 现在两个 hook 给 gitleaks 传 `--exit-code`，有命中时退一个它出错时不用的码。别的非 0
# 都当「扫描没有跑完」，gitleaks 的 stderr 原样转给人看。

# 09-19 第一版配置想用 lookahead 表达「大小写混排」。RE2 不支持它，gitleaks 加载配置时 panic。
LOOKAHEAD_REGEX = "(?=.*[A-Z])[A-Za-z0-9]{64}"

# 往 allowlist 加一条时漏了收尾的 `]`。`.gitleaks.toml` 的日常改动就是加 allowlist（SECURITY.md
# 第三节），手滑写坏 TOML 多半是这个样子。补上 `]` 它就能加载（2026-09-29 实测），坏的只有这一处。
UNCLOSED_ALLOWLIST = "\n[[rules.allowlists]]\nregexes = ['''^[0-9a-f]{8}$'''\n"

# 让 gitleaks 自己出错的三种办法，2026-09-29 实测 8.30.1。值是只有 gitleaks 会说的话，
# 原话转没转出来，只能拿它判断。
GITLEAKS_FAULTS = {
    # 配置文件不在：FTL，退出 1。这与「有命中」的默认退出码相同，只看退出码分不开。
    "config-missing": "unable to load gitleaks config",
    # 文件在，TOML 写坏了：同一句 FTL，退出 1，也不写报告。判据与上一格相同。冒号后面那半句
    # `While parsing config: toml: ...` 是 viper 与 go-toml 的措辞，不拿来当判据，理由同下一格。
    "config-bad-toml": "unable to load gitleaks config",
    # 规则里有 lookahead：panic，退出 2。判据是那条正则本身，panic 的原话里原样带着它。
    # 不用正则引擎的报错措辞：同是 8.30.1，本机 Homebrew 版说 `invalid or unsupported Perl syntax`，
    # CI 用的官方发行版说 `bad perl operator`。两者走的是两个引擎：发行版带构建标签 `gore2regex`，
    # 用 wasilibs/go-re2；Homebrew 只跑 `go build`，用 Go 标准库 regexp。钉住 gitleaks 版本也钉不住。
    "config-lookahead": LOOKAHEAD_REGEX,
}

# 每个 hook 有两个拦截框。测试按框的标题判断 hook 走了哪一支。
PUSH_LEAK_BOX = "推送被拦下：将要进入远端的 commit 里有疑似密钥"
PUSH_UNFINISHED_BOX = "推送被拦下：密钥扫描没有跑完"
COMMIT_LEAK_BOX = "提交被拦下：暂存区里有疑似密钥"
COMMIT_UNFINISHED_BOX = "提交被拦下：密钥扫描没有跑完"


def _break_config(repo: Path, fault: str) -> None:
    """照 `GITLEAKS_FAULTS` 里的一种办法，弄坏临时仓库根上的 `.gitleaks.toml`。"""
    config = repo / ".gitleaks.toml"
    if fault == "config-missing":
        config.unlink()
    elif fault == "config-bad-toml":
        # 调用方都先拷了仓库的配置。在它末尾追加，其余部分原样是好的。
        with config.open("a", encoding="utf-8") as f:
            f.write(UNCLOSED_ALLOWLIST)
    else:
        assert fault == "config-lookahead", fault
        config.write_text(f"[[rules]]\nid = \"mixed-case\"\nregex = '''{LOOKAHEAD_REGEX}'''\n", encoding="utf-8")


@pytest.mark.parametrize("fault", list(GITLEAKS_FAULTS))
def test_pre_push_reports_a_gitleaks_error_as_an_unfinished_scan(tmp_path: Path, fault: str) -> None:
    """gitleaks 自己出错时，pre-push 照样拦下，但要说扫描没有跑完，并转出 gitleaks 的原话。

    用干净的仓库，理由同 git log 失败那条：拦下只剩一个原因。只跑一种推送。配置在扫任何
    commit 之前就加载失败，三种推送走到的是同一个出错分支。
    """
    marker = GITLEAKS_FAULTS[fault]
    # 判据要是也写在 hook 自己的文字里，删掉转发照样绿。09-29 `[git]` 那条断言就是这样空掉的。
    assert marker not in HOOK.read_text(encoding="utf-8"), f"`{marker}` 写进了 pre-push 自己的文字，它证明不了转发"
    repo = _build_merge_repo(tmp_path / "repo", "resolved")
    _break_config(repo.path, fault)
    proc = _run_pre_push(repo, "existing-branch")
    assert proc.returncode == 1, (
        f"gitleaks 出错了，pre-push 却放行（{fault}，returncode={proc.returncode}）。扫描没跑完不能当成通过。\n"
        f"stderr:\n{proc.stderr[-2000:]}"
    )
    assert PUSH_LEAK_BOX not in proc.stderr, (
        f"gitleaks 出错（{fault}），pre-push 却报「有疑似密钥」，叫人去交易所作废重发 key。\n"
        f"stderr:\n{proc.stderr[-2000:]}"
    )
    assert PUSH_UNFINISHED_BOX in proc.stderr, f"pre-push 没说扫描没有跑完（{fault}）：\n{proc.stderr[-2000:]}"
    assert marker in proc.stderr, (
        f"pre-push 没把 gitleaks 的原话转给推送的人（{fault}）。只看到拦截，不知道该修什么。\n"
        f"stderr:\n{proc.stderr[-2000:]}"
    )


@pytest.mark.parametrize("fault", list(GITLEAKS_FAULTS))
def test_pre_commit_reports_a_gitleaks_error_as_an_unfinished_scan(fault: str) -> None:
    """同上，pre-commit 这一侧。暂存的是普通内容，拦下只剩一个原因。"""
    marker = GITLEAKS_FAULTS[fault]
    assert marker not in PRE_COMMIT.read_text(encoding="utf-8"), (
        f"`{marker}` 写进了 pre-commit 自己的文字，它证明不了转发"
    )
    proc = _run_pre_commit("x = 1\n", fault)
    shown = proc.stdout + proc.stderr
    assert proc.returncode == 1, (
        f"gitleaks 出错了，pre-commit 却放行（{fault}，returncode={proc.returncode}）。扫描没跑完不能当成通过。\n"
        f"{shown[-2000:]}"
    )
    assert COMMIT_LEAK_BOX not in shown, (
        f"gitleaks 出错（{fault}），pre-commit 却报「有疑似密钥」，叫人去交易所作废重发 key。\n{shown[-2000:]}"
    )
    assert COMMIT_UNFINISHED_BOX in shown, f"pre-commit 没说扫描没有跑完（{fault}）：\n{shown[-2000:]}"
    assert marker in shown, (
        f"pre-commit 没把 gitleaks 的原话转给提交的人（{fault}）。只看到拦截，不知道该修什么。\n{shown[-2000:]}"
    )


def test_pre_push_still_reports_a_finding_as_a_finding(leaky_repo: _MergeRepo) -> None:
    """反方向：真有命中时，要出「有疑似密钥」的框，不能出「扫描没有跑完」。

    hook 靠 `--exit-code` 分开这两种情况。这个参数哪天被删了，有命中时 gitleaks 退回默认的 1，
    与出错同码。真命中于是被报成「扫描没有跑完」：推送照样拦下，人却被引去修配置。
    """
    proc = _run_pre_push(leaky_repo, "existing-branch")
    assert proc.returncode == 1, f"假凭据没被 pre-push 拦下（returncode={proc.returncode}）：\n{proc.stderr[-2000:]}"
    assert PUSH_LEAK_BOX in proc.stderr and PUSH_UNFINISHED_BOX not in proc.stderr, (
        "真有命中，pre-push 却没报「有疑似密钥」，或者报成了扫描没跑完。检查 hook 里的 `--exit-code` 还在不在。\n"
        f"stderr:\n{proc.stderr[-2000:]}"
    )


def test_pre_commit_still_reports_a_finding_as_a_finding(
    blocked_commit: tuple[str, subprocess.CompletedProcess[str]],
) -> None:
    """同上，pre-commit 这一侧。"""
    _, proc = blocked_commit
    shown = proc.stdout + proc.stderr
    assert COMMIT_LEAK_BOX in shown and COMMIT_UNFINISHED_BOX not in shown, (
        "真有命中，pre-commit 却没报「有疑似密钥」，或者报成了扫描没跑完。检查 hook 里的 `--exit-code` 还在不在。\n"
        f"{shown[-2000:]}"
    )


# ---------------------------------------------------------------------------
# install-hooks.sh 的自检，与这个文件自己的 `_scan`
# ---------------------------------------------------------------------------
#
# 两处都拿 gitleaks 的退出码判断「抓到了」，犯的是两个 hook 此前的同一个错。配置不在或写坏时，
# gitleaks 是 FTL、退 1，与有命中时的默认退出码相同。2026-09-29 实测：
#
#   - 自检只看退出码非 0。配置不在、规则里有 lookahead（panic，退 2），它都打「✓ 扫描是活的」、
#     退 0，gitleaks 的原话进了 /dev/null。SECURITY.md 说「看到 ✓ 才算装好」，接不住的恰恰是这两种。
#   - `_scan` 认 0 与 1，报告不在就返回 []。配置写坏时，`test_the_config_loads_at_all` 与哈希误报
#     那几条照样通过：尺子坏了，量出来是零。
#
# 修法与两个 hook 相同：传 `--exit-code`，有命中时退 42，别的非 0 都当没跑完。

INSTALL_HOOKS = ROOT / "deploy" / "install-hooks.sh"


def _run_install_hooks(tmp_path: Path, fault: str | None = None) -> subprocess.CompletedProcess[str]:
    """照新 clone 的做法跑 `bash deploy/install-hooks.sh`，但跑在临时仓库的副本里。

    只能在副本里跑：脚本会执行 `git config core.hooksPath .githooks`。本机的真仓库配的是绝对路径，
    在它或任何 worktree 里跑一次，所有会话的 hook 指向都会变。脚本按自己的位置找仓库根，所以把它与
    `.githooks/`、配置一起拷进副本。环境用 `_git_env()`：在 hook 里跑 pytest 时，外层的 `GIT_DIR`
    会把那条 `git config` 带回真仓库。给了 `fault` 就照 `_break_config` 弄坏副本的配置。
    """
    clone = tmp_path / "clone"
    (clone / "deploy").mkdir(parents=True)
    _git(clone, "init", "-q", "-b", "main")
    shutil.copy(INSTALL_HOOKS, clone / "deploy" / INSTALL_HOOKS.name)
    shutil.copytree(ROOT / ".githooks", clone / ".githooks")
    shutil.copy(CONFIG, clone / ".gitleaks.toml")
    if fault is not None:
        _break_config(clone, fault)
    proc = subprocess.run(
        ["bash", str(clone / "deploy" / INSTALL_HOOKS.name)],
        cwd=clone,
        env=_git_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    # 那条 `git config` 要落在副本里。落到别处时副本里没有这个键，`git config` 退 1。
    wrote = subprocess.run(
        ["git", "-C", str(clone), "config", "core.hooksPath"],
        env=_git_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert wrote.returncode == 0, f"core.hooksPath 没写进副本。脚本的输出：\n{(proc.stdout + proc.stderr)[-2000:]}"
    return proc


@pytest.mark.parametrize("fault", list(GITLEAKS_FAULTS))
def test_install_hooks_self_check_reports_a_gitleaks_error_as_unfinished(tmp_path: Path, fault: str) -> None:
    """gitleaks 自己出错时，自检不能打 ✓：脚本要以非 0 退出，并把 gitleaks 的原话给装 hook 的人。

    判据只用 gitleaks 的原话。自检自己的文字证明不了转发，理由同两个 hook 那组。
    """
    marker = GITLEAKS_FAULTS[fault]
    assert marker not in INSTALL_HOOKS.read_text(encoding="utf-8"), (
        f"`{marker}` 写进了 install-hooks.sh 自己的文字，它证明不了转发"
    )
    proc = _run_install_hooks(tmp_path, fault)
    shown = proc.stdout + proc.stderr
    assert proc.returncode == 1, (
        f"gitleaks 出错了，自检却没失败（{fault}，returncode={proc.returncode}）。\n"
        "SECURITY.md 说「看到 ✓ 才算装好」，配置坏了照样打 ✓，这句就不成立。\n"
        f"{shown[-2000:]}"
    )
    assert marker in shown, (
        f"自检失败了，却没把 gitleaks 的原话给装 hook 的人（{fault}）。只看到 ✗，不知道该修什么。\n{shown[-2000:]}"
    )


def test_install_hooks_self_check_passes_with_the_repository_config(tmp_path: Path) -> None:
    """对照：仓库自己的配置下，自检要拦下伪造凭据，脚本以 0 退出。

    没有这一条，上面那条分不清「看见了 gitleaks 出错」和「自检什么都报失败」。它还守着 `--exit-code`：
    这个参数被删掉时，gitleaks 抓到伪造凭据也退 1，与出错同码，自检就把好好的配置报成没跑完。
    """
    proc = _run_install_hooks(tmp_path)
    assert proc.returncode == 0, (
        "仓库自己的配置下，install-hooks.sh 的自检没通过。检查脚本里的 `--exit-code` 还在不在。\n"
        f"{(proc.stdout + proc.stderr)[-2000:]}"
    )


@pytest.mark.parametrize("fault", list(GITLEAKS_FAULTS))
def test_scan_refuses_to_read_a_gitleaks_error_as_no_findings(tmp_path: Path, fault: str) -> None:
    """`_scan` 是这个文件里配置测试的尺子。gitleaks 自己出错时它要炸，不能返回 []。

    判据是 gitleaks 的原话出现在炸出来的消息里，用来确认炸的原因是 gitleaks 出错，而不是别的断言。
    这条证明不了 `_scan` 自己转发了 stderr：pytest 改写断言时，会把 `proc` 的 repr 连同 stderr
    一起写进消息（2026-09-29 实测）。
    """
    config = tmp_path / ".gitleaks.toml"
    shutil.copy(CONFIG, config)
    _break_config(tmp_path, fault)
    with pytest.raises(AssertionError, match=re.escape(GITLEAKS_FAULTS[fault])):
        _scan({"ordinary.py": "x = 1\n"}, config)


# ---------------------------------------------------------------------------
# 第五种坏法：有一类凭据，没有一条规则认得它
# ---------------------------------------------------------------------------
#
# 告警 webhook 的 URL 是凭据。Lark/飞书自定义机器人的 token 就是 URL 的最后一段，拿到它就能以机器人的
# 身份往告警群里发消息。2026-09-30 之前，默认规则与 `.gitleaks.toml` 都认不出它：用随机 UUID 拼一条
# 写进临时文件，扫出来是 `no leaks found`。可 09-29 发送失败的日志里有 39 行带着真 URL，本仓库又惯于
# 把日志行抄进公开的 `docs/RESEARCH_LOG.md`。
#
# 规则是 `.gitleaks.toml` 里的 `beidou-lark-webhook-url`，只认路径加 UUID。下面几组各守一件事：
# 两个 hook 拦得下，CI 与手动审计的命令扫得到，仓库里现有的假值不误报。SECURITY.md 第二节那张表上
# 「对告警 webhook」一列，第 1、2、4 层各对应这里的一组。

LARK_RULE = "beidou-lark-webhook-url"
LARK_HOSTS = ("open.larksuite.com", "open.feishu.cn")

# 泄漏时的三种样子。第一种是 09-29 那 39 行，第二种是 `~/.zshrc` 里存它的那一行。
# 第三种只有路径、没有 host：规则不锚 host，理由写在 `.gitleaks.toml` 那条规则上面。
LARK_LEAKS = {
    "failed-delivery-log": "alert delivery failed (https://open.larksuite.com/open-apis/bot/v2/hook/{token}): \n",
    "zshrc-export": 'export BEIDOU_ALERTS_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/{token}"\n',
    "request-line": "POST /open-apis/bot/v2/hook/{token} HTTP/1.1\n",
}


def _forged_lark_token() -> str:
    """Lark webhook 的 token 形态：36 位 UUID。每次随机生成，理由同 `_forged_binance_credential`。"""
    return str(uuid.uuid4())


@pytest.mark.parametrize("form", list(LARK_LEAKS))
def test_pre_commit_blocks_a_forged_lark_webhook(form: str) -> None:
    """假 webhook 进了暂存区，pre-commit 要拦下，命中要记在这条规则名下，token 要脱敏。

    规则名只在 stdout 里找。hook 自己的话都写到 stderr，stdout 只有 gitleaks 的命中清单
    （pre-push 那组「干净的推送 stdout 为空」守的就是这个分工）。在两者合起来的输出里找，
    hook 哪天把规则名写进拦截框，这条断言就什么也证明不了。
    """
    token = _forged_lark_token()
    proc = _run_pre_commit(LARK_LEAKS[form].format(token=token))
    shown = proc.stdout + proc.stderr
    assert proc.returncode == 1 and COMMIT_LEAK_BOX in proc.stderr, (
        f"暂存区里的假 webhook 没被 pre-commit 拦下（{form}，returncode={proc.returncode}）。\n"
        f"检查 `.gitleaks.toml` 里的 `{LARK_RULE}` 还在不在，全局 allowlist 最近加了什么。\n{shown[-2000:]}"
    )
    assert LARK_RULE in proc.stdout, (
        f"pre-commit 拦下了，stdout 的命中清单里却没有 `{LARK_RULE}`（{form}）。两种可能：\n"
        "hook 不带 `--verbose` 了，清单根本没列出来；或者拦它的是别的规则，这条规则自己已经坏了。\n"
        f"{shown[-2000:]}"
    )
    assert token not in shown, "pre-commit 把假 webhook 的 token 打到了屏幕上。检查 hook 里的 `--redact` 还在不在。"


@pytest.fixture(scope="module", params=LARK_HOSTS)
def lark_repo(request: pytest.FixtureRequest) -> Iterator[_MergeRepo]:
    """一个临时仓库：解冲突时把一条假 webhook 写进了 merge 提交。两个 host 各建一个。

    放进 merge 提交，是因为 CI 这一层要接住的正是本机 hook 一个都没跑的情况，最典型的是在 GitHub
    网页上解冲突。
    """
    url = f"https://{request.param}/open-apis/bot/v2/hook/{_forged_lark_token()}"
    with tempfile.TemporaryDirectory() as tmp:
        yield _build_merge_repo(Path(tmp) / "lark", url)


def test_pre_push_blocks_a_forged_lark_webhook(lark_repo: _MergeRepo) -> None:
    """pre-push 读同一份配置。它接的是 pre-commit 没跑的那些：`--no-verify`，或者提交时 hook 还没装。"""
    token = lark_repo.resolution.rsplit("/", 1)[1]
    proc = _run_pre_push(lark_repo, "existing-branch")
    shown = proc.stdout + proc.stderr
    assert proc.returncode == 1 and PUSH_LEAK_BOX in proc.stderr, (
        f"merge 提交里的假 webhook 没被 pre-push 拦下（returncode={proc.returncode}）。\n"
        f"检查 `.gitleaks.toml` 里的 `{LARK_RULE}` 还在不在，全局 allowlist 最近加了什么。\n{shown[-2000:]}"
    )
    assert LARK_RULE in proc.stdout, (
        f"pre-push 拦下了，stdout 的命中清单里却没有 `{LARK_RULE}`。两种可能：hook 不带 `--verbose` 了，\n"
        f"或者拦它的是别的规则，这条规则自己已经坏了。\n{shown[-2000:]}"
    )
    assert token not in shown, "pre-push 把假 webhook 的 token 打到了屏幕上。检查 hook 里的 `--redact` 还在不在。"


@pytest.mark.parametrize("source", list(HISTORY_SCANS))
def test_history_scans_catch_a_forged_lark_webhook(lark_repo: _MergeRepo, source: str) -> None:
    """CI 的 Secrets 门与 SECURITY.md 的手动审计命令，都要把这条假 webhook 记在这条规则名下。

    CI 红的时候 webhook 已经公开可读，所以它是事后的一层。但在 GitHub 网页上解冲突或改文件时，
    本机两个 hook 一个都不跑，发现得了它的只剩这一层。
    """
    scans = HISTORY_SCANS[source]()
    assert scans, f"{source} 里找不到用 gitleaks 扫 git 历史的命令。是门被删了，还是换了写法？"
    for argv in scans:
        proc, findings = _run_history_scan(argv, lark_repo)
        found = {(f["RuleID"], f["Commit"], f["File"]) for f in findings}
        assert (LARK_RULE, lark_repo.merge, "settings.py") in found, (
            f"{source} 的 `{shlex.join(argv)}` 没把 merge 提交里的假 webhook 记在 `{LARK_RULE}` 名下"
            f"（returncode={proc.returncode}，命中 {sorted(found)}）。\n"
            "检查 `.gitleaks.toml` 里的这条规则还在不在，全局 allowlist 最近加了什么。\n"
            f"stderr:\n{proc.stderr[-2000:]}"
        )
        assert proc.returncode != 0, f"{source} 的命令看见了命中却返回 0，CI 那一步不会变红"


# 仓库里已有的 Lark 形态假值，每个文件一种。按文件原样扫，不把字符串抄进这里：抄一份的话，
# 哪天那几个文件换了写法，这里照绿，红的却是下一次推送时 CI 的全历史扫描。
LARK_LOOKALIKE_FILES = (
    "tests/cli/test_alert_drill.py",  # 人手敲的 token：SUPER-SECRET-TOKEN-VALUE
    "tests/live/test_delivery_is_verified.py",  # 三个字符与一个字符的 token：abc、x
    "tests/live/test_alert_logs_never_carry_the_webhook_token.py",  # f-string 里的 {MARKER}
)


@pytest.mark.parametrize("path", LARK_LOOKALIKE_FILES)
def test_repository_lark_lookalikes_do_not_false_positive(path: str) -> None:
    """测试里的假 webhook 不该报。它们的 token 都不是 UUID 形态，而规则只认 UUID。

    任何一条误报，基线就不是零，而不是零基线的检查三天之内就会被忽略。
    """
    text = (ROOT / path).read_text(encoding="utf-8")
    assert "open-apis/bot/v2/hook/" in text, f"{path} 里已经没有 Lark 形态的假值了，把它从这一组里删掉"
    findings = _scan({Path(path).name: text})
    assert findings == [], (
        f"{path} 里的假 webhook 被报成了密钥：{[(f['RuleID'], f['StartLine']) for f in findings]}\n"
        "基线必须是零。别把规则的 token 那一段放宽到认得出人手敲的假值；真要放宽，按 SECURITY.md 第三节写 allowlist。"
    )


# ---------------------------------------------------------------------------
# 命中之后怎么处置：按 RuleID 分流
# ---------------------------------------------------------------------------
#
# 2026-09-30 之前，两个「有疑似密钥」框与 SECURITY.md 第四节只写了 Binance 的处置：去交易所作废重发。
# 命中上面那条 webhook 规则时照着做，换掉的是没漏的交易所 key，漏了的地址照样能往告警群发消息。
#
# 框是带引号的 heredoc，不看命中的是哪条规则，打出来的永远是同一段字。分流靠读的人对照清单里的
# `RuleID:` 那一行，所以框里要写出规则的名字。框写在 stderr，上面那组只在 stdout 里找规则名，不受影响。


def _quoted_box(hook: Path, title: str) -> str:
    """hook 里标题为 `title` 的那个框。

    只认带引号的 heredoc（`<<'MSG'`）。它不展开变量，文件里的字就是屏幕上的字，所以这里读文件等于读屏幕。
    哪天改成不带引号，这里就找不到框。
    """
    boxes = re.findall(r"<<'MSG'\n(.*?)\nMSG\n", hook.read_text(encoding="utf-8"), re.S)
    matching = [box for box in boxes if title in box]
    assert len(matching) == 1, (
        f"`.githooks/{hook.name}` 里标题为「{title}」的带引号 heredoc 应该恰好一个，找到 {len(matching)} 个。\n"
        "框改成不带引号的 heredoc 会展开变量，屏幕上的字就不再是文件里的字。"
    )
    return matching[0]


@pytest.mark.parametrize(
    ("hook", "title"), [(PRE_COMMIT, COMMIT_LEAK_BOX), (HOOK, PUSH_LEAK_BOX)], ids=["pre-commit", "pre-push"]
)
def test_the_leak_box_routes_a_webhook_hit_by_rule_id(hook: Path, title: str) -> None:
    """「有疑似密钥」的框要写出 webhook 规则的名字，也要守住「不要新建 `env.sh`」。

    第二条是换 webhook 时最容易踩的坑。凭据此刻在 `~/.zshrc`，为放新地址去建 `env.sh`，`~/.zshrc` 就不再
    被读，Binance 的两个变量跟着消失，实盘循环下一次启动以 78 退出。
    """
    box = _quoted_box(hook, title)
    assert LARK_RULE in box, (
        f"`.githooks/{hook.name}` 被拦时的框里没有 `{LARK_RULE}`。读的人对不上清单里的 `RuleID:`，\n"
        "命中 webhook 也会照 Binance 那支去交易所作废 key，漏了的地址照样能用。"
    )
    assert "不要为了这一步新建 `env.sh`" in box, (
        f"`.githooks/{hook.name}` 被拦时的框不再说「不要为了这一步新建 `env.sh`」。\n"
        "照着框去建 `env.sh`，`~/.zshrc` 就不再被读，实盘循环下一次启动以 78 退出。"
    )


def test_security_md_routes_a_webhook_hit_by_rule_id() -> None:
    """SECURITY.md 第四节同样要分出 webhook 那一支。两个框都让人去那里看完整步骤。"""
    text = SECURITY.read_text(encoding="utf-8")
    assert "## 四、真漏了怎么办" in text, "SECURITY.md 第四节的标题改了，两个框里的「SECURITY.md 第四节」要跟着核对"
    section = text.split("## 四、真漏了怎么办", 1)[1].split("\n## ", 1)[0]
    assert LARK_RULE in section, (
        f"SECURITY.md 第四节「真漏了怎么办」里没有 `{LARK_RULE}`。两个框都让人去那里看完整步骤，\n"
        "那里只剩交易所那一支，命中 webhook 的人会照着去作废没漏的 key。"
    )
