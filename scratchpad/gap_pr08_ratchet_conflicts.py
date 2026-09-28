"""GAP-PR08：ratchet 文件 `tests/architecture/test_source_budget.py` 的合并冲突成本。零 ledger、只读。

执行手册 §3.13（`docs/analysis/2026-09-28-production-refactor-execution-plan.md`）预写的阈值：
**每周 ≥ 1 次可归因冲突 → WP-C1 的价值成立，否则它只是整洁。** 阈值在看数据之前写下，这里照用，不改。

**为什么要重做三方合并。** 合并提交记录的是解完冲突的结果，从提交本身看不出当时要不要人解。所以对
`git log --merges --since=<since> <ref>` 的每个合并，用 `git merge-tree --write-tree` 在它的两个父提交上
重做一次三方合并（ort，与 `git merge` 默认相同），记下冲突文件的全集。这样既能数该文件冲突几次，也能说
它占全部冲突的多少。`merge-tree` 与下面的 `hash-object -w` 只往对象库写对象，不动任何 ref。

合并分四类，按提交在 `<ref>` 第一父链上的位置判，不按提交信息的措辞判（措辞有十几种写法）：

- `PR 合并`：GitHub 的「Merge pull request #N」。GitHub 拒绝合并有冲突的 PR，这一类重做应当全部干净，
  是对照：若它们也报冲突，说明重做的算法与当时不同，其余读数要打折。
- `分支上并入 main`：第二父提交在第一父链上、合并提交本身不在。PR 分支上的
  「Merge remote-tracking branch 'origin/main' into …」「Merge main（…）进 … 分支」都在这一类。
- `main 第一父链上的本地合并`：合并提交在第一父链上、又不是 PR 合并。PR 流程成为常态（09-16 的 #17 起）之前，
  在本地合进 main 的那些（09-03 至 09-15）。
- `其它本地合并`：两者都不是，多为分支合进分支。

后三类都是本地合并：冲突在那里由人（或 agent）解。每个本地合并还读两件事：它相对第一父提交改没改该文件
（`git diff M^1 M -- <file>`），以及它属于哪个 PR（PR 合并 `M` 的 `M^1..M^2` 里的合并提交归这个 PR；分支被
多个 PR 复用时归第一个把它带进 main 的）。

**WP-C1 的反事实。** WP-C1 把理由注释逐字搬到 `docs/SOURCE_BUDGET_LOG.md`，CEILING 的值留下，每个条目上方
留一行指向记录的注释（手册 §3.7）。对该文件冲突的每个合并，把 base、两个父提交三份该文件各拆成这两份
（`wp_c1_shapes`），分别用 `git merge-file`（histogram，与 ort 相同）重做：测试文件上仍冲突的是 CEILING
数值行，搬迁动不了；记录文件上冲突的是搬走的文字，只是换了个文件。两份都干净的，才是 WP-C1 真正消掉的冲突。
同一条 merge-file 路径先在原文上重做一次，与 ort 的结论逐个比对，对不上的单列。

**PR 时长。** `gh pr list --state merged --json …` 的 `createdAt → mergedAt`，比较 diff 含该文件与不含的 PR
的中位数。只取该文件建成之后合入的 PR（之前的 PR 不可能碰它）。`files` 字段一个 PR 最多给 100 个路径；
`changedFiles` 超过 100 的 PR 改用它的合并提交 `git diff --name-only M^1 M` 判。混杂因素：碰该文件的 PR 本来
就更大（包长过了顶才会碰它），所以另按改动行数分四档各比一次，并单列「分支上真的解过该文件冲突」的 PR，
以及这些解冲突的合并发生在 PR 打开之前还是之后。

复现（仓库根目录下；`gh` 偶尔 503，重试即可）：

    gh pr list --state merged --limit 400 \\
        --json number,title,createdAt,mergedAt,files,additions,deletions,changedFiles,headRefName > prs.json
    /Users/maguannan/beidou/.venv/bin/python scratchpad/gap_pr08_ratchet_conflicts.py \\
        --ref 6aee43c152dfdbf15a84b5034f79b3c74ca120d5 --prs prs.json

2026-09-28 的读数（ref `6aee43c1` = #206 合入后的 origin/main，since `2026-08-28T00:00:00+00:00`，
`gh` 取于 08:36Z）：

- 374 个合并，重做后 76 个有冲突、冲突文件共 115 次。该文件冲突 44 次，占冲突文件次数的 38.3%、占有冲突的
  合并 57.9%，排第一（`docs/RESEARCH_LOG.md` 38 次第二）；其中 22 次它是唯一的冲突文件。190 个 PR 合并重做全部干净。
- 手册窗口 4.48 周 → 9.83 次/周；该文件建成（09-04T05:15Z）以来 3.45 周 → 12.77 次/周；ISO 周 W36–W39 各 4、17、10、13 次。
- 分支上并入 main 的 78 个合并里 51 个相对第一父提交改了该文件：19 个是重做有冲突的，32 个是干净带入 main 的改动。
- WP-C1 反事实：merge-file 在原文上 44 个全部复现；WP-C1 形状下测试文件仍冲突 41 个（45 个冲突块全含
  CEILING 数值行）、记录文件冲突 41 个，两者任一 44 个，真正消掉 0 个。
- PR 时长（09-04 之后合入的 188 个）：碰该文件 74 个，开到合中位 0.15 h；不碰 114 个，0.15 h；四个改动行数档内
  也都在 0.14–0.19 h。归到 PR 的 16 个该文件冲突合并，9 个在 PR 打开之前，7 个在打开之后。

完整表与判读见 `docs/RESEARCH_LOG.md`「2026-09-28 · GAP-PR08 / GAP-PR10」一节。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RATCHET = "tests/architecture/test_source_budget.py"
PR_SUBJECT = re.compile(r"^Merge pull request #(\d+)")
# E-PR38 数 CEILING 数值行用的是同一个形状：`"beidou_xxx": 12_345,`
CEILING_VALUE = re.compile(r'^\s*"(beidou_[a-z]+)": [0-9_]+,')
WEEK_SECONDS = 7 * 86_400
KINDS = ("PR 合并", "分支上并入 main", "main 第一父链上的本地合并", "其它本地合并")


def git(*args: str, ok: tuple[int, ...] = (0,), stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(["git", *args], capture_output=True, text=True, check=False, input=stdin)
    if done.returncode not in ok:
        raise RuntimeError(f"git {' '.join(args)} -> {done.returncode}: {done.stderr.strip()}")
    return done


def when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)


@dataclass
class Merge:
    sha: str
    parents: list[str]
    at: datetime
    subject: str
    kind: str = ""
    pr: int | None = None  # PR 合并：它自己的编号；本地合并：它所属的 PR
    conflicts: list[str] = field(default_factory=list)
    redo_failed: str = ""
    ratchet_conflict: bool = False
    ratchet_hunks: int = 0
    ratchet_changed_vs_p1: bool = False
    # WP-C1 反事实：原文在 merge-file 上的冲突块数（对照 ort）；WP-C1 形状的测试文件、搬出去的记录文件
    # 各自的冲突块数；merge-base 个数
    control_hunks: int | None = None
    test_hunks: int | None = None
    test_value_hunks: int = 0  # 其中含 CEILING 数值行的块
    log_hunks: int | None = None
    bases: int = 0

    @property
    def still_needs_hand(self) -> bool:
        """WP-C1 之后这个合并在测试文件或记录文件上仍要人解。"""
        return bool(self.test_hunks or self.log_hunks)


def merges_since(ref: str, since: str) -> list[Merge]:
    out = git("log", "--merges", f"--since={since}", ref, "--format=%H%x1f%P%x1f%cI%x1f%s").stdout
    rows: list[Merge] = []
    for line in out.splitlines():
        sha, parents, stamp, subject = line.split("\x1f", 3)
        rows.append(Merge(sha, parents.split(), when(stamp), subject))
    return rows


def blob(rev: str) -> str:
    done = git("show", f"{rev}:{RATCHET}", ok=(0, 128))
    return done.stdout if done.returncode == 0 else ""


def wp_c1_shapes(text: str) -> tuple[str, str]:
    """按手册 §3.7 的形状把一份该文件拆成两份：WP-C1 之后的测试文件，与搬出去的 `SOURCE_BUDGET_LOG.md`。

    搬的是 `PLAN_BUDGET` 之后到 CEILING 表的 `}` 之间的全部 `#` 注释行：表上方的「总述」块与每个条目上方
    的理由块。测试文件里每个 `"beidou_x": N,` 行上方换成一行指向记录的注释（手册原文：每条目上方留一行
    `# 抬顶记录：docs/SOURCE_BUDGET_LOG.md#<anchor>`），「总述」处留一行；CEILING 的值一个不动。记录文件
    按原文顺序逐块排列，每块前面一行 `## <包名>`（「总述」块是 `## 总述`），块内逐字。

    指向行在三份版本里逐字相同，它的作用是把相邻条目隔开：不留这一行而是整段删掉注释，相邻两个包的数值行
    会贴在一起，两边各抬一个包就被 git 当成冲突，反事实会多数出一批 WP-C1 并不会造成的冲突。
    """
    lines = text.splitlines(keepends=True)
    table = next((i for i, line in enumerate(lines) if line.startswith("CEILING = {")), None)
    if table is None:
        return text, ""
    end = next((i for i in range(table, len(lines)) if lines[i].startswith("}")), len(lines) - 1)
    plan = next((i for i, line in enumerate(lines[:table]) if line.startswith("PLAN_BUDGET")), table - 1)
    shaped: list[str] = lines[: plan + 1]
    log: list[str] = []
    run: list[str] = []
    for index in range(plan + 1, end + 1):
        line = lines[index]
        if line.lstrip().startswith("#"):
            run.append(line)
            continue
        value = CEILING_VALUE.match(line)
        if index == table:
            shaped.append("# 抬顶记录：docs/SOURCE_BUDGET_LOG.md#总述\n")
            section = "总述"
        elif value is not None:
            shaped.append(f"    # 抬顶记录：docs/SOURCE_BUDGET_LOG.md#{value.group(1)}\n")
            section = value.group(1)
        else:
            section = ""
        if run:
            log.extend([f"## {section or '（块后无条目）'}\n", *run])
            run = []
        shaped.append(line)
    return "".join(shaped + lines[end + 1 :]), "".join(log)


def merge_file(base: str, ours: str, theirs: str) -> tuple[int, list[list[str]]]:
    """`git merge-file --object-id`：冲突块数（退出码）与每个冲突块两边的行。三份内容先写成 blob。"""
    ids = [git("hash-object", "-w", "--stdin", stdin=text).stdout.strip() for text in (ours, base, theirs)]
    done = git("merge-file", "--object-id", "--quiet", "--diff-algorithm", "histogram", *ids, ok=tuple(range(128)))
    hunks: list[list[str]] = []
    inside = False
    for line in git("cat-file", "-p", done.stdout.strip()).stdout.splitlines():
        if line.startswith("<<<<<<< "):
            inside = True
            hunks.append([])
        elif line.startswith(">>>>>>> "):
            inside = False
        elif inside and not line.startswith("======="):
            hunks[-1].append(line)
    return done.returncode, hunks


def redo(merge: Merge) -> None:
    if len(merge.parents) != 2:
        merge.redo_failed = f"{len(merge.parents)} 个父提交"
        return
    done = git("merge-tree", "--write-tree", "--name-only", "--no-messages", *merge.parents, ok=(0, 1, 128))
    if done.returncode == 128:
        merge.redo_failed = done.stderr.strip()[:120]
        return
    lines = done.stdout.splitlines()
    tree, names = lines[0], [name for name in lines[1:] if name]
    merge.conflicts = sorted(set(names)) if done.returncode == 1 else []
    merge.ratchet_conflict = RATCHET in merge.conflicts
    if not merge.ratchet_conflict:
        return
    merged = git("cat-file", "-p", f"{tree}:{RATCHET}").stdout.splitlines()
    merge.ratchet_hunks = sum(1 for line in merged if line.startswith("<<<<<<< "))
    bases = git("merge-base", "--all", *merge.parents).stdout.split()
    merge.bases = len(bases)
    base, ours, theirs = blob(bases[0]), blob(merge.parents[0]), blob(merge.parents[1])
    merge.control_hunks, _ = merge_file(base, ours, theirs)
    shaped = [wp_c1_shapes(text) for text in (base, ours, theirs)]
    merge.test_hunks, hunks = merge_file(*(test for test, _ in shaped))
    merge.test_value_hunks = sum(1 for hunk in hunks if any(CEILING_VALUE.match(line) for line in hunk))
    merge.log_hunks, _ = merge_file(*(log for _, log in shaped))


def classify(merges: list[Merge], ref: str) -> None:
    first_parent = set(git("rev-list", "--first-parent", ref).stdout.split())
    by_sha = {m.sha: m for m in merges}
    for merge in merges:
        if match := PR_SUBJECT.match(merge.subject):
            merge.kind, merge.pr = KINDS[0], int(match.group(1))
        elif merge.sha not in first_parent and merge.parents[-1] in first_parent:
            merge.kind = KINDS[1]
        elif merge.sha in first_parent:
            merge.kind = KINDS[2]
        else:
            merge.kind = KINDS[3]
        merge.ratchet_changed_vs_p1 = (
            git("diff", "--quiet", merge.parents[0], merge.sha, "--", RATCHET, ok=(0, 1)).returncode == 1
        )
    # 一个本地合并归第一个把它带进 main 的 PR：按合并时间从早到晚认领。
    for merge in sorted((m for m in merges if m.kind == KINDS[0]), key=lambda m: m.at):
        branch = git("rev-list", "--merges", f"{merge.parents[0]}..{merge.parents[1]}").stdout.split()
        for sha in branch:
            owned = by_sha.get(sha)
            if owned is not None and owned.kind != KINDS[0] and owned.pr is None:
                owned.pr = merge.pr


def iso_week(at: datetime) -> str:
    year, week, _ = at.isocalendar()
    return f"{year}-W{week:02d}"


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def fmt(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def pr_table(prs_path: Path, merges: list[Merge], born: datetime) -> dict[str, Any]:
    prs = json.loads(prs_path.read_text(encoding="utf-8"))
    pr_merge = {m.pr: m for m in merges if m.kind == KINDS[0]}
    paid: dict[int, list[Merge]] = {}
    for m in merges:
        if m.kind != KINDS[0] and m.ratchet_conflict and m.pr is not None:
            paid.setdefault(m.pr, []).append(m)
    rows = []
    unresolved = []
    before_open = after_open = 0
    paying: list[dict[str, Any]] = []
    for pr in prs:
        merged, created = when(pr["mergedAt"]), when(pr["createdAt"])
        if merged < born:
            continue
        paths = {f["path"] for f in pr.get("files") or []}
        touches: bool | None = RATCHET in paths
        if len(paths) < int(pr.get("changedFiles") or 0) and not touches:
            # `files` 截在 100 个：用合并提交自己的 diff 判
            merge = pr_merge.get(pr["number"])
            if merge is None:
                touches = None
                unresolved.append(pr["number"])
            else:
                names = git("diff", "--name-only", merge.parents[0], merge.sha).stdout.split()
                touches = RATCHET in names
        mine = paid.get(pr["number"], [])
        early = sum(1 for m in mine if m.at < created)
        before_open += early
        after_open += len(mine) - early
        if mine:
            paying.append(
                {
                    "number": pr["number"],
                    "hours": (merged - created).total_seconds() / 3600.0,
                    "before_open": early,
                    "after_open": len(mine) - early,
                    "title": str(pr.get("title") or "")[:60],
                }
            )
        rows.append(
            {
                "number": pr["number"],
                "hours": (merged - created).total_seconds() / 3600.0,
                "lines": int(pr.get("additions") or 0) + int(pr.get("deletions") or 0),
                "files": int(pr.get("changedFiles") or 0),
                "touches": touches,
                "paid": pr["number"] in paid,
            }
        )
    known = [r for r in rows if r["touches"] is not None]
    groups = {
        "碰该文件": [r for r in known if r["touches"]],
        "不碰": [r for r in known if not r["touches"]],
        "分支上解过该文件冲突": [r for r in known if r["paid"]],
        "碰该文件、分支上没解过它的冲突": [r for r in known if r["touches"] and not r["paid"]],
    }
    summary = {
        name: {
            "n": len(group),
            "median_hours": median([r["hours"] for r in group]),
            "p90_hours": sorted(r["hours"] for r in group)[int(0.9 * (len(group) - 1))] if group else None,
            "median_lines": median([float(r["lines"]) for r in group]),
            "median_files": median([float(r["files"]) for r in group]),
        }
        for name, group in groups.items()
    }
    # 按改动行数四分：同一档里比，碰该文件的 PR 更大这件事在档内被压掉一大半。
    ordered = sorted(r["lines"] for r in known)
    cuts = [ordered[int(len(ordered) * q)] for q in (0.25, 0.5, 0.75)] if ordered else []
    strata = []
    for index in range(4):
        low = cuts[index - 1] if index else 0
        high = cuts[index] if index < 3 else None
        band = [r for r in known if r["lines"] >= low and (high is None or r["lines"] < high)]
        strata.append(
            {
                "lines": f"[{low}, {high if high is not None else '∞'})",
                "touch_n": sum(1 for r in band if r["touches"]),
                "touch_median_hours": median([r["hours"] for r in band if r["touches"]]),
                "other_n": sum(1 for r in band if not r["touches"]),
                "other_median_hours": median([r["hours"] for r in band if not r["touches"]]),
            }
        )
    return {
        "prs": len(rows),
        "unresolved": unresolved,
        "groups": summary,
        "strata": strata,
        "paid_merges_before_open": before_open,
        "paid_merges_after_open": after_open,
        "paying": sorted(paying, key=lambda row: row["number"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ref", default="origin/main", help="读哪一个提交的历史；入库读数用的是钉住的 hash")
    parser.add_argument("--since", default="2026-08-28T00:00:00+00:00")
    parser.add_argument("--prs", type=Path, default=None, help="`gh pr list … --json …` 的输出；不给就不算 PR 时长")
    parser.add_argument("--json", type=Path, default=None, help="把逐个合并的读数写到这里")
    args = parser.parse_args()

    ref = git("rev-parse", args.ref).stdout.strip()
    ref_at = when(git("log", "-1", "--format=%cI", ref).stdout.strip())
    since = when(args.since)
    born = when(git("log", "--diff-filter=A", "--format=%cI", ref, "--", RATCHET).stdout.split()[-1])
    merges = merges_since(ref, args.since)
    for merge in merges:
        redo(merge)
    classify(merges, ref)

    print(
        f"ref {ref[:8]}（{ref_at:%Y-%m-%d %H:%M}Z），since {since:%Y-%m-%d %H:%M}Z；{RATCHET} 建于 {born:%m-%d %H:%M}Z"
    )
    failed = [m for m in merges if m.redo_failed]
    print(f"合并提交 {len(merges)} 个；重做失败 {len(failed)} 个 {[m.sha[:8] for m in failed]}")

    print("\n| 类别 | 合并数 | 重做后有冲突 | 其中该文件冲突 | 该文件是唯一冲突文件 | 相对第一父提交改了该文件 |")
    print("| --- | ---: | ---: | ---: | ---: | ---: |")
    for kind in [*KINDS, "合计"]:
        group = [m for m in merges if kind in (m.kind, "合计")]
        print(
            f"| {kind} | {len(group)} | {sum(1 for m in group if m.conflicts)} "
            f"| {sum(1 for m in group if m.ratchet_conflict)} "
            f"| {sum(1 for m in group if m.conflicts == [RATCHET])} "
            f"| {sum(1 for m in group if m.ratchet_changed_vs_p1)} |"
        )

    per_file = Counter(name for m in merges for name in m.conflicts)
    events = sum(per_file.values())
    conflicted = [m for m in merges if m.conflicts]
    print(f"\n冲突文件次数合计 {events}（{len(conflicted)} 个合并）；按文件：")
    print("| 文件 | 冲突次数 | 占冲突文件次数 | 占有冲突的合并 |")
    print("| --- | ---: | ---: | ---: |")
    for name, count in per_file.most_common(8):
        print(f"| `{name}` | {count} | {count / events:.1%} | {count / len(conflicted):.1%} |")

    hit = sorted((m for m in merges if m.ratchet_conflict), key=lambda m: m.at)
    print(f"\n该文件冲突的 {len(hit)} 个合并：")
    print("| 合并 | 时刻（UTC） | 类别 | PR | 冲突文件数 | 该文件冲突块 | WP-C1 后：测试文件 / 记录文件冲突块 |")
    print("| --- | --- | --- | --- | ---: | ---: | --- |")
    for m in hit:
        print(
            f"| `{m.sha[:8]}` | {m.at:%m-%d %H:%M} | {m.kind} | {f'#{m.pr}' if m.pr else '—'} "
            f"| {len(m.conflicts)} | {m.ratchet_hunks} | {m.test_hunks} / {m.log_hunks} |"
        )
    disagree = [m.sha[:8] for m in hit if not m.control_hunks]
    print(
        f"\nWP-C1 反事实：{len(hit)} 个里 merge-file 在原文上复现冲突 {len(hit) - len(disagree)} 个（对不上：{disagree}；"
        f"多个 merge-base 的 {sum(1 for m in hit if m.bases > 1)} 个）。WP-C1 形状下："
        f"测试文件仍冲突 {sum(1 for m in hit if m.test_hunks)} 个（冲突块 {sum(m.test_hunks or 0 for m in hit)} 个，"
        f"含 CEILING 数值行的 {sum(m.test_value_hunks for m in hit)} 个，搬迁动不了）；"
        f"搬出去的记录文件冲突 {sum(1 for m in hit if m.log_hunks)} 个；"
        f"两者任一、仍要人解 {sum(1 for m in hit if m.still_needs_hand)} 个；"
        f"两边都干净、真正消失 {sum(1 for m in hit if not m.still_needs_hand)} 个 "
        f"{[m.sha[:8] for m in hit if not m.still_needs_hand]}"
    )

    local = [m for m in merges if m.kind == KINDS[1]]
    touched = [m for m in local if m.ratchet_changed_vs_p1]
    print(
        f"\n{KINDS[1]} {len(local)} 个；相对第一父提交改了该文件 {len(touched)} 个，"
        f"其中重做有该文件冲突 {sum(1 for m in touched if m.ratchet_conflict)} 个、"
        f"干净带入 main 那边的改动 {sum(1 for m in touched if not m.ratchet_conflict)} 个"
    )

    print("\n按 ISO 周（合并提交时刻，UTC）：")
    print("| 周 | 合并数 | 有冲突的合并 | 该文件冲突 | 其中只因它 | WP-C1 后测试文件仍冲突 | WP-C1 后仍要人解 |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for week in sorted({iso_week(m.at) for m in merges}):
        group = [m for m in merges if iso_week(m.at) == week]
        print(
            f"| {week} | {len(group)} | {sum(1 for m in group if m.conflicts)} "
            f"| {sum(1 for m in group if m.ratchet_conflict)} "
            f"| {sum(1 for m in group if m.conflicts == [RATCHET])} "
            f"| {sum(1 for m in group if m.ratchet_conflict and m.test_hunks)} "
            f"| {sum(1 for m in group if m.ratchet_conflict and m.still_needs_hand)} |"
        )
    for label, start in (("手册窗口", since), ("该文件建成以来", born)):
        weeks = (ref_at - start).total_seconds() / WEEK_SECONDS
        window = [m for m in hit if m.at >= start]
        only = sum(1 for m in window if m.conflicts == [RATCHET])
        gone = sum(1 for m in window if not m.still_needs_hand)
        print(
            f"{label}：{start:%m-%d %H:%M} → {ref_at:%m-%d %H:%M} 共 {weeks:.2f} 周；该文件冲突 {len(window)} 次 = "
            f"{len(window) / weeks:.2f} 次/周；只因它 {only} 次 = {only / weeks:.2f} 次/周；"
            f"WP-C1 真正消掉的 {gone} 次 = {gone / weeks:.2f} 次/周"
        )

    if args.prs is not None:
        table = pr_table(args.prs, merges, born)
        print(
            f"\nPR 时长（createdAt → mergedAt；该文件建成后合入的 {table['prs']} 个 PR；"
            f"无法判定是否碰该文件：{table['unresolved']}）"
        )
        print("| 组 | PR 数 | 开到合中位（小时） | P90（小时） | 改动行数中位 | 文件数中位 |")
        print("| --- | ---: | ---: | ---: | ---: | ---: |")
        for name, row in table["groups"].items():
            print(
                f"| {name} | {row['n']} | {fmt(row['median_hours'], 2)} | {fmt(row['p90_hours'], 2)} "
                f"| {fmt(row['median_lines'], 0)} | {fmt(row['median_files'], 0)} |"
            )
        print("\n按改动行数四分（同档内比）：")
        print("| 改动行数 | 碰该文件：PR 数 | 中位（小时） | 不碰：PR 数 | 中位（小时） |")
        print("| --- | ---: | ---: | ---: | ---: |")
        for row in table["strata"]:
            print(
                f"| {row['lines']} | {row['touch_n']} | {fmt(row['touch_median_hours'], 2)} "
                f"| {row['other_n']} | {fmt(row['other_median_hours'], 2)} |"
            )
        print(
            f"\n归到 PR 的该文件冲突合并：PR 打开之前 {table['paid_merges_before_open']} 个、"
            f"打开之后 {table['paid_merges_after_open']} 个"
        )
        print("| PR | 开到合（小时） | 该文件冲突合并：打开前 / 打开后 | 标题 |")
        print("| --- | ---: | --- | --- |")
        for row in table["paying"]:
            print(
                f"| #{row['number']} | {row['hours']:.2f} | {row['before_open']} / {row['after_open']} | {row['title']} |"
            )

    if args.json is not None:
        args.json.write_text(
            json.dumps(
                [
                    {
                        "sha": m.sha,
                        "at": m.at.isoformat(),
                        "kind": m.kind,
                        "pr": m.pr,
                        "subject": m.subject,
                        "conflicts": m.conflicts,
                        "ratchet_conflict": m.ratchet_conflict,
                        "ratchet_hunks": m.ratchet_hunks,
                        "control_hunks": m.control_hunks,
                        "wp_c1_test_hunks": m.test_hunks,
                        "wp_c1_test_value_hunks": m.test_value_hunks,
                        "wp_c1_log_hunks": m.log_hunks,
                        "bases": m.bases,
                        "ratchet_changed_vs_p1": m.ratchet_changed_vs_p1,
                        "redo_failed": m.redo_failed,
                    }
                    for m in merges
                ],
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
