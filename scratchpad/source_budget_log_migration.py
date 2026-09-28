"""WP-C1（2026-09-28，操作者裁定 Q2a）：source budget 的抬顶理由从测试文件原文逐字搬到 `docs/SOURCE_BUDGET_LOG.md`。

这个脚本是那次搬迁的记录。输入是任意版本、尚未搬迁的 `tests/architecture/test_source_budget.py`，一条命令写出
两个文件；写完从磁盘读回这两个文件，反向拼回输入，逐字节比较。

用法（在仓库根目录。输入取 origin/main 上那份；默认写回仓库，会覆盖工作树里的测试文件）：

    git show origin/main:tests/architecture/test_source_budget.py > /tmp/tsb.py
    python scratchpad/source_budget_log_migration.py /tmp/tsb.py
    python scratchpad/source_budget_log_migration.py /tmp/tsb.py --out /tmp/again --roundtrip-out /tmp/rt.py
    diff -u /tmp/tsb.py /tmp/rt.py                                    # 没有输出
    cmp docs/SOURCE_BUDGET_LOG.md /tmp/again/docs/SOURCE_BUDGET_LOG.md  # 同一输入两次输出逐字节相同

`--out DIR` 把两个文件写到 DIR 下同样的相对路径（默认写回仓库）。`--repo DIR` 是读 git 历史与 `docs/` 的仓库
（默认是当前目录所在的仓库）。`--roundtrip-out FILE` 把反向拼回的原文件另存一份，供人手 `diff`。
退出码：0 搬完且往返逐字节相同；1 往返不同或代码的 AST 变了（印出 diff）；2 输入不是能无歧义搬迁的形态（什么也不写）。

**切块规则。** `CEILING = {` 之上连续的注释是「总述」；表内每个 `"包": 数,` 条目之上连续的注释是那个包的理由块。
总述之上必须是空行；表内只能有注释行与条目行；最后一个条目之后不能再有注释。不合这几条就退出码 2，不猜。

**写出什么。** `docs/SOURCE_BUDGET_LOG.md`：说明，旧行号引用对照表，「总述（原文）」一节，每个包一节。节内的代码块
是注释去掉行首 `# `（表内还有四格缩进）之后的逐字内容，单独一个 `#` 的行成了空行。代码块的围栏比内容里最长的
一串反引号还长，所以块的边界不会有歧义。原文上方没有注释的条目，那一节没有代码块。测试文件：理由块换成一行
指针，总述换成两行说明，其余逐字不动。只有 `# ` 后面什么都没有、或 `#` 后面不跟空格的行无法无歧义还原；
ruff format 不会留下这两种行，遇到了就退出码 2。

**旧行号引用对照表。** 扫 `git ls-files` 里 `docs/` 下的 `.md`（本文件除外）与 `CLAUDE.md`，找写着
`test_source_budget.py:<行号>` 的地方，以及同一行里紧跟其后的 `` `:<行号>` ``。行号指的是写下引用那天的文件
版本：用 `git log -S` 找到引入那段文字的最早提交（文字在文档里不唯一时用 `git blame`），读那个提交里的测试
文件，看这几行当时是什么。这几行不是完整的语句时，再沿那个提交的祖先往前 72 小时，找这几行正好是完整语句的
最近一版一起列出——作者写引用时看的常常是更早一版。只写文件名、不带行号的引用不进表。
"""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

BUDGET_PATH = "tests/architecture/test_source_budget.py"
LOG_PATH = "docs/SOURCE_BUDGET_LOG.md"
SUMMARY_HEADING = "总述（原文）"
TABLE_HEADING = "旧行号引用对照表"
OPEN = "CEILING = {"
CLOSE = "}"
INDENT = "    "
POINTER_PREFIX = f"{INDENT}# 抬顶记录：{LOG_PATH}#"
HEADER = (
    f"# 抬顶记录：{LOG_PATH}。原先写在这里的总述和每个条目之上的理由，已原文逐字搬到那里（WP-C1，",
    "# 2026-09-28 操作者裁定 Q2a）。以后抬顶，理由写进那个文件对应包的一节；这里每个条目上方只留一行指向那一节。",
)
ENTRY = re.compile(r'^    "(?P<package>\w+)": (?P<value>[0-9_]+),$')
FENCE_OPEN = re.compile(r"(`{3,})text")
REFERENCE = re.compile(
    r"(?P<path>[A-Za-z0-9_./-]*[A-Za-z0-9_-]\.(?:py|md|ya?ml|jsonl?|toml|sh|plist|txt|csv|parquet))(?![A-Za-z0-9])"
    r"(?P<loc>:\d+(?:[-–]\d+)?)?"
    r"|`(?P<cont>:\d+(?:[-–]\d+)?)`"
)
WINDOW_HOURS = 72
EXCERPT_WIDTH = 40


class Refused(Exception):
    """输入不是能无歧义搬迁的形态。宁可停下，也不猜。"""


# ---------------------------------------------------------------------------------------------------------
# 切块、瘦身、写记录、反向拼回


@dataclass(frozen=True)
class Entry:
    package: str
    line: str  # 条目行，逐字
    line_no: int  # 条目行在输入里的行号（1 起）
    block: tuple[str, ...]  # 条目之上的理由，已去掉 `    # ` 前缀


@dataclass(frozen=True)
class Budget:
    head: tuple[str, ...]  # 总述之前的全部行，逐字
    summary: tuple[str, ...]  # 总述，已去掉 `# ` 前缀
    summary_from: int  # 总述第一行在输入里的行号
    entries: tuple[Entry, ...]
    tail: tuple[str, ...]  # 从 `}` 到文件末尾，逐字
    total: int  # 输入的行数

    @property
    def moved(self) -> int:
        return len(self.summary) + sum(len(entry.block) for entry in self.entries)


def anchor(heading: str) -> str:
    """GitHub 由标题生成 anchor 的规则：小写；字母、数字、下划线、连字符、空格以外的字符去掉；空格换成连字符。"""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def _uncomment(line: str, indent: str, line_no: int) -> str:
    if line == f"{indent}#":
        return ""
    if line.startswith(f"{indent}# ") and len(line) > len(indent) + 2:
        return line[len(indent) + 2 :]
    raise Refused(f"第 {line_no} 行 {line!r}：既不是单独的 `#`，也不是 `# ` 加内容，去掉前缀后无法无歧义还原")


def _comment(content: str, indent: str) -> str:
    return f"{indent}# {content}" if content else f"{indent}#"


def _split(text: str) -> list[str]:
    if not text.endswith("\n") or "\r" in text:
        raise Refused("文本要以 \\n 结尾，且不含 \\r")
    return text[:-1].split("\n")


def parse(text: str) -> Budget:
    lines = _split(text)
    if any(line.startswith(POINTER_PREFIX) for line in lines) or any(line in HEADER for line in lines):
        raise Refused(
            "输入已经搬迁过（里面有指针行）。输入要用搬迁之前的版本，例如 git show origin/main:" + BUDGET_PATH
        )
    opens = [index for index, line in enumerate(lines) if line == OPEN]
    if len(opens) != 1:
        raise Refused(f"`{OPEN}` 出现 {len(opens)} 次，应当恰好一次")
    top = start = opens[0]
    while start > 0 and lines[start - 1].startswith("#"):
        start -= 1
    if start == 0 or lines[start - 1] != "":
        raise Refused(f"总述（第 {start + 1} 行起）之上应当是一个空行")
    summary = tuple(_uncomment(line, "", start + offset + 1) for offset, line in enumerate(lines[start:top]))
    entries: list[Entry] = []
    pending: list[str] = []
    index = top + 1
    while index < len(lines) and lines[index] != CLOSE:
        line = lines[index]
        if match := ENTRY.match(line):
            entries.append(Entry(match["package"], line, index + 1, tuple(pending)))
            pending = []
        elif line.startswith(f"{INDENT}#"):
            pending.append(_uncomment(line, INDENT, index + 1))
        else:
            raise Refused(f'`CEILING` 表内第 {index + 1} 行既不是注释，也不是 `"包": 数,` 条目：{line!r}')
        index += 1
    if index == len(lines):
        raise Refused(f"`{OPEN}` 之后没有单独一行的 `{CLOSE}`")
    if pending:
        raise Refused(f"`CEILING` 表的最后一个条目之后还有 {len(pending)} 行注释，没有条目可挂")
    packages = [entry.package for entry in entries]
    if not packages or len(set(packages)) != len(packages):
        raise Refused(f"`CEILING` 的条目为空或重名：{packages}")
    return Budget(tuple(lines[:start]), summary, start + 1, tuple(entries), tuple(lines[index:]), len(lines))


def render_slim(budget: Budget) -> str:
    lines = [*budget.head, *HEADER, OPEN]
    for entry in budget.entries:
        lines += [POINTER_PREFIX + anchor(entry.package), entry.line]
    return "\n".join([*lines, *budget.tail]) + "\n"


def _fence(budget: Budget) -> str:
    contents = [*budget.summary, *(content for entry in budget.entries for content in entry.block)]
    longest = max((len(run) for content in contents for run in re.findall(r"`+", content)), default=0)
    return "`" * max(3, longest + 1)


def render_log(budget: Budget, blob: str, table: list[str]) -> str:
    fence = _fence(budget)
    summary_to = budget.summary_from + len(budget.summary) - 1
    out = [
        "# source budget ratchet 抬顶记录",
        "",
        "M-003 的 source budget ratchet 给每个包定了行数 ceiling，就是 `tests/architecture/test_source_budget.py`",
        "里的 `CEILING`。抬顶只允许发生在写明理由的那个 commit 里。这个文件收的是那些理由。",
        "",
        "搬迁之前，理由写在测试文件里，是紧挨着 `CEILING` 的注释。2026-09-28 操作者裁定 Q2a：理由搬到这里，",
        f"测试文件每个条目上方只留一行指针（WP-C1）。搬迁的输入是那个文件的 blob `{blob}`，共 {budget.total:,} 行，",
        f"其中理由注释 {budget.moved:,} 行。",
        "",
        "搬迁由 `scratchpad/source_budget_log_migration.py` 完成，规则如下：",
        "",
        "- 「总述（原文）」一节是 `CEILING = {` 之上那段注释。每个包一节，是原文里那个条目之上的注释。",
        "- 代码块里是注释逐字的内容。只去掉了行首的 `# `，`CEILING` 表内的注释还去掉了四格缩进。"
        "单独一个 `#` 的行成了空行。",
        "- 各块按原文顺序排列，不合并，不改字。原文里上方没有注释的条目，那一节没有代码块。",
        "- 脚本写完这个文件和瘦身后的测试文件，再用这两个文件反向拼回原文件，与输入逐字节比较。不相同就不算搬完。",
        "",
        "原文每一行仍在同一行上，按现象措辞 grep 照样搜得到。",
        "",
        "以后抬顶，理由写进对应包那一节的末尾，放在原文代码块之后，写明日期、增量（旧顶 → 新顶）和测量数据。",
        "测试文件里那一行指针不动。`tests/architecture/test_every_ceiling_points_at_its_record.py` 守四件事：",
        "每个条目上方都有指向这里的一行；`CEILING` 表里只有指针和条目；这里每个包都有一节；这个文件不少于 4,000 行。",
        "",
        f"## {TABLE_HEADING}",
        "",
        *table,
        "",
        f"## {SUMMARY_HEADING}",
        "",
        f"原文：blob 里第 {budget.summary_from}–{summary_to} 行，`{OPEN}` 之上的注释，共 {len(budget.summary):,} 行。",
        "",
        f"{fence}text",
        *budget.summary,
        fence,
    ]
    for entry in budget.entries:
        out += ["", f"## {entry.package}", ""]
        if entry.block:
            first = entry.line_no - len(entry.block)
            out += [
                f"原文：blob 里第 {first}–{entry.line_no - 1} 行，`{entry.line.strip()}` 之上的注释，"
                f"共 {len(entry.block):,} 行。",
                "",
                f"{fence}text",
                *entry.block,
                fence,
            ]
        else:
            out.append(
                f"原文：`{entry.line.strip()}`（blob 里第 {entry.line_no} 行）之上没有注释，所以这一节没有代码块。"
            )
    return "\n".join(out) + "\n"


def log_blocks(log: str) -> dict[str, tuple[str, ...]]:
    """记录里每个二级标题下的第一个代码块；没有代码块的标题对应空块。代码块里的行不当标题读。"""
    blocks: dict[str, tuple[str, ...]] = {}
    fenced: set[str] = set()
    heading: str | None = None
    fence: str | None = None
    body: list[str] = []
    for line in _split(log):
        if fence is not None:
            if line != fence:
                body.append(line)
                continue
            if heading is not None and heading not in fenced:
                blocks[heading] = tuple(body)
                fenced.add(heading)
            fence, body = None, []
        elif opening := FENCE_OPEN.fullmatch(line):
            fence = opening[1]
        elif line.startswith("## "):
            heading = line[3:]
            if heading in blocks:
                raise Refused(f"记录里标题重复：{heading}")
            blocks[heading] = ()
    if fence is not None:
        raise Refused("记录里有没收尾的代码块")
    return blocks


def reverse(slim: str, log: str) -> str:
    """只用瘦身后的测试文件和记录两个文件的文本，拼回搬迁之前的测试文件。"""
    blocks = log_blocks(log)
    lines = _split(slim)
    used: list[str] = []

    def block(heading: str) -> tuple[str, ...]:
        if heading not in blocks:
            raise Refused(f"记录里没有 `## {heading}`")
        used.append(heading)
        return blocks[heading]

    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if tuple(lines[index : index + len(HEADER)]) == HEADER:
            out += [_comment(content, "") for content in block(SUMMARY_HEADING)]
            index += len(HEADER)
            continue
        if line.startswith(POINTER_PREFIX):
            match = ENTRY.match(lines[index + 1]) if index + 1 < len(lines) else None
            if match is None or line != POINTER_PREFIX + anchor(match["package"]):
                raise Refused(f"瘦身文件第 {index + 1} 行的指针下面不是它指向的那个条目")
            out += [_comment(content, INDENT) for content in block(match["package"])]
            index += 1
            continue
        out.append(line)
        index += 1
    unused = sorted(set(blocks) - set(used) - {TABLE_HEADING})
    if unused or len(used) != len(set(used)):
        raise Refused(f"记录与瘦身文件对不上：没用上的节 {unused}，用过的节 {used}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------------------------------------
# 旧行号引用对照表


@dataclass(frozen=True)
class Ref:
    doc: str
    line_no: int
    line: str  # 引用所在的那一行，原样
    token: str  # 原样，例如 `:536-543`
    first: int
    last: int
    needle: str | None  # 在文档里唯一的一段文字，用来 `git log -S`


@dataclass(frozen=True)
class Unit:
    start: int
    end: int
    name: str
    top: bool  # 顶层语句；否则是 `CEILING` 表里的一个条目
    ident: str | None = None  # 顶层赋值或定义的名字


@dataclass(frozen=True)
class Reading:
    """某一版测试文件的第 first–last 行当时是什么。"""

    aligned: bool  # 这几行正好是一条或几条完整的语句（或者是一条语句的第一行）
    what: str
    code: tuple[str, ...]  # 涉及的代码（留在测试文件里的部分）
    moved: tuple[tuple[str, str], ...]  # 涉及的理由原文：（所在块，去掉前缀的一行）


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout.decode("utf-8")


def _needle(text: str, line: str, start: int, end: int) -> str | None:
    for candidate in (line[start:end], line[:end], line):
        if candidate and text.count(candidate) == 1:
            return candidate
    return None


def find_refs(repo: Path) -> list[Ref]:
    listed = git(repo, "ls-files", "-z", "--", "docs", "CLAUDE.md").split("\0")
    docs = sorted(path for path in listed if path.endswith(".md") and path != LOG_PATH)
    refs: list[Ref] = []
    for doc in docs:
        text = (repo / doc).read_bytes().decode("utf-8")
        for line_no, line in enumerate(text.split("\n"), 1):
            if "test_source_budget.py" not in line:
                continue
            origin: int | None = None
            for match in REFERENCE.finditer(line):
                if match["path"]:
                    origin = match.start() if match["path"].endswith("test_source_budget.py") else None
                token = match["loc"] if match["path"] else match["cont"]
                if origin is None or not token:
                    continue
                first, _, last = token[1:].replace("–", "-").partition("-")
                needle = _needle(text, line, origin, match.end())
                refs.append(Ref(doc, line_no, line, token, int(first), int(last or first), needle))
    return refs


def _units(text: str) -> list[Unit]:
    try:
        module = ast.parse(text)
    except SyntaxError:
        return []
    units: list[Unit] = []
    for index, node in enumerate(module.body):
        start = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])
        end = node.end_lineno or node.lineno
        ident: str | None = None
        if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            name = "模块 docstring"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            ident = node.name
            name = f"`{'class' if isinstance(node, ast.ClassDef) else 'def'} {ident}`"
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            name = "import"
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            ident = node.targets[0].id
            name = f"`{ident}`"
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            ident = node.target.id
            name = f"`{ident}`"
        else:
            name = f"第 {start}–{end} 行的语句"
        units.append(Unit(start, end, name, True, ident))
        if ident == "CEILING" and isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Dict):
            for key, value in zip(node.value.keys, node.value.values, strict=True):
                if isinstance(key, ast.Constant):
                    entry = f"`CEILING` 的 `{key.value}` 条目"
                    units.append(Unit(key.lineno, value.end_lineno or key.lineno, entry, False))
    return units


def _mentioned(ref: Ref, text: str) -> str:
    """引用所在那一行用反引号提到、又是这一版顶层名字的，各在第几行。引用没落在完整语句上时，这是最近的线索。"""
    spans = [
        span
        for span in re.findall(r"`([^`]+)`", ref.line)
        if "test_source_budget.py" not in span and not re.fullmatch(r":\d+(?:[-–]\d+)?", span)
    ]
    found = []
    for unit in _units(text):
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(unit.ident or '')}(?![A-Za-z0-9_])"
        if unit.top and unit.ident and any(re.search(pattern, span) for span in spans):
            lines = f"第 {unit.start} 行" if unit.start == unit.end else f"第 {unit.start}–{unit.end} 行"
            found.append(f"{unit.name} 在{lines}")
    return f"。同一行提到的 {'、'.join(found)}" if found else ""


def read(text: str, first: int, last: int) -> Reading:
    lines = _split(text)
    units = _units(text)
    kinds: dict[int, tuple[str, str]] = {}
    for unit in units:
        for line_no in range(unit.start, unit.end + 1):
            kinds[line_no] = ("code", unit.name)
    opens = [index for index, line in enumerate(lines) if re.fullmatch(r"CEILING\b.*\{", line)]
    if opens:
        top = start = opens[0]
        while start > 0 and lines[start - 1].startswith("#"):
            start -= 1
        for index in range(start, top):
            kinds[index + 1] = ("moved", "总述")
        pending: list[int] = []
        for index in range(top + 1, len(lines)):
            if lines[index] == CLOSE:
                break
            if match := ENTRY.match(lines[index]):
                for line_no in pending:
                    kinds[line_no] = ("moved", match["package"])
                pending = []
            elif lines[index].lstrip().startswith("#"):
                pending.append(index + 1)
    words: list[str] = []
    code: list[str] = []
    moved: list[tuple[str, str]] = []
    for line_no in range(first, last + 1):
        line = lines[line_no - 1]
        kind, name = kinds.get(line_no, ("blank", "空行") if not line.strip() else ("comment", "别的注释"))
        if kind == "moved":
            moved.append((name, line.lstrip()[2:]))
            word = "总述正文" if name == "总述" else f"`{name}` 条目之上的理由"
        else:
            word = name
            if kind == "code":
                code.append(name)
        if not words or words[-1] != word:
            words.append(word)
    starts = {unit.start for unit in units}
    aligned = first in starts and (first == last or last in {unit.end for unit in units})
    if aligned:
        covering = [unit for unit in units if unit.top and first <= unit.start and unit.end <= last]
        covering = covering or [unit for unit in units if first <= unit.start and unit.end <= last]
        head = next(unit for unit in units if unit.start == first)
        if first == last and head.end > first:
            what = f"{head.name} 的第一行"
            code = [head.name]
        elif len(covering) == 1 and (covering[0].start, covering[0].end) == (first, last):
            what = f"{covering[0].name}（整条）"
            code = [covering[0].name]
        else:
            what = "、".join(dict.fromkeys(unit.name for unit in covering))
            code = [unit.name for unit in covering]
    else:
        what = "、".join(words)
    return Reading(aligned, what, tuple(dict.fromkeys(code)), tuple(moved))


def _excerpt(text: str) -> str:
    """原文开头一段，放进表格里的代码段：宽度（全角算 2）不过 EXCERPT_WIDTH，`|` 转义。"""
    kept: list[str] = []
    width = 0
    for char in text.strip():
        width += 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if width > EXCERPT_WIDTH:
            kept.append("…")
            break
        kept.append(char)
    cut = "".join(kept)
    longest = max((len(run) for run in re.findall(r"`+", cut)), default=0)
    ticks = "`" * (longest + 1)
    padded = f" {cut} " if longest else cut
    return f"{ticks}{padded}{ticks}".replace("|", "\\|")


def _where(reading: Reading, budget: Budget) -> str:
    parts: list[str] = []
    if reading.code:
        names = {unit.name for unit in _units(render_slim(budget))}
        present = [name for name in reading.code if name in names]
        gone = [name for name in reading.code if name not in names]
        for label, names_ in (("测试文件里的", present), ("测试文件里已经没有", gone)):
            if names_:
                listed = "、".join(names_)
                parts.append(f"{label} {listed}" if listed.startswith("`") else label + listed)
    lines = [content for _, content in reading.moved if content.strip()]
    if lines:
        sections = [(SUMMARY_HEADING, budget.summary), *((entry.package, entry.block) for entry in budget.entries)]
        found = [heading for heading, block in sections if lines[0] in block]
        if found:
            parts.append(f"[{found[0]}](#{anchor(found[0])}) 从 {_excerpt(lines[0])} 起的 {len(reading.moved)} 行")
        else:
            parts.append(f"那几行理由后来改过，现在的记录里找不到 {_excerpt(lines[0])}")
    return "；".join(parts) or "—"


def _stamp(repo: Path, commit: str) -> int:
    return int(git(repo, "log", "-1", "--format=%ct", commit).strip())


def _utc(stamp: int) -> str:
    return datetime.fromtimestamp(stamp, tz=UTC).strftime("%Y-%m-%d %H:%MZ")


def _introduced(repo: Path, ref: Ref) -> str | None:
    if ref.needle is not None:
        found = git(repo, "log", "--reverse", "--format=%H", f"-S{ref.needle}", "--", ref.doc).split()
        if found:
            return found[0]
    blame = git(repo, "blame", "--porcelain", "-L", f"{ref.line_no},{ref.line_no}", "--", ref.doc).split()
    return None if not blame or set(blame[0]) == {"0"} else blame[0]


def table(repo: Path, budget: Budget) -> tuple[list[str], dict[str, int]]:
    cache: dict[str, str | None] = {}

    def version(commit: str) -> str | None:
        if commit not in cache:
            shown = subprocess.run(
                ["git", "-C", str(repo), "show", f"{commit}:{BUDGET_PATH}"], capture_output=True, check=False
            )
            cache[commit] = shown.stdout.decode("utf-8") if shown.returncode == 0 else None
        return cache[commit]

    refs = find_refs(repo)
    counts = {"direct": 0, "earlier": 0, "unaligned": 0, "failed": 0}
    if not refs:
        return ["`docs/` 与 `CLAUDE.md` 里没有带行号的 `test_source_budget.py` 引用。"], counts
    rows = [
        "别的文档里写着 `test_source_budget.py:<行号>` 的引用，原文不改。那些行号指的是写下引用那天的文件版本，",
        "不是今天的。这张表逐条定位：先用 `git log -S` 找到引入那段文字的提交，再读那个提交里的测试文件，看这几行",
        f"当时是什么。这几行不是完整的语句时，再沿那个提交的祖先往前 {WINDOW_HOURS} 小时，找这几行正好是完整语句的",
        "最近一版，一起列出——作者写引用时看的常常是更早一版。",
        "",
        "扫描范围是 `docs/` 下全部 `.md`（本文件除外）和 `CLAUDE.md`。只写文件名、不带行号的引用不在表里，例如",
        "RESEARCH_LOG 里的「理由写在 `test_source_budget.py`」；那些理由现在都在本文件。",
        "",
        "| 引用出处 | 行号 | 引入提交 | 那一版这几行是什么 | 现在在哪 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for ref in refs:
        cells = [f"`{ref.doc}:{ref.line_no}`", f"`{ref.token}`"]
        commit = _introduced(repo, ref)
        if commit is None:
            counts["failed"] += 1
            rows.append("| " + " | ".join([*cells, "—", "未能定位：找不到引入这段文字的提交", "—"]) + " |")
            continue
        when = _stamp(repo, commit)
        cells.append(f"`{commit[:8]}` {_utc(when)}")
        text = version(commit)
        if text is None:
            counts["failed"] += 1
            rows.append("| " + " | ".join([*cells, f"未能定位：那个提交里没有 `{BUDGET_PATH}`", "—"]) + " |")
            continue
        size = len(_split(text))
        if not 1 <= ref.first <= ref.last <= size:
            counts["failed"] += 1
            rows.append("| " + " | ".join([*cells, f"未能定位：那一版只有 {size:,} 行", "—"]) + " |")
            continue
        reading = read(text, ref.first, ref.last)
        if reading.aligned:
            counts["direct"] += 1
            rows.append("| " + " | ".join([*cells, reading.what, _where(reading, budget)]) + " |")
            continue
        earlier: tuple[str, int, Reading] | None = None
        history = git(repo, "log", "--format=%H %ct", commit, "--", BUDGET_PATH).split("\n")
        for item in filter(None, history):
            candidate, stamp = item.split()
            if int(stamp) < when - WINDOW_HOURS * 3600:
                continue
            old = version(candidate)
            if old is None or ref.last > len(_split(old)):
                continue
            if (old_reading := read(old, ref.first, ref.last)).aligned:
                earlier = (candidate, int(stamp), old_reading)
                break
        if earlier is None:
            counts["unaligned"] += 1
            what = f"{reading.what}。往前 {WINDOW_HOURS} 小时内，没有哪一版的这几行是完整的语句{_mentioned(ref, text)}"
            rows.append("| " + " | ".join([*cells, what, _where(reading, budget)]) + " |")
            continue
        counts["earlier"] += 1
        candidate, stamp, old_reading = earlier
        hours = (when - stamp) / 3600
        what = f"{reading.what}。往前 {hours:.1f} 小时的 `{candidate[:8]}` 那一版，这几行正好是 {old_reading.what}"
        where = f"{_where(old_reading, budget)}。按 `{commit[:8]}` 那一版读，则是{_where(reading, budget)}"
        rows.append("| " + " | ".join([*cells, what, where]) + " |")
    return rows, counts


# ---------------------------------------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("input", type=Path, help="搬迁之前的 test_source_budget.py（任意版本）")
    parser.add_argument("--repo", type=Path, help="读 git 历史与 docs/ 的仓库，默认当前目录所在的仓库")
    parser.add_argument("--out", type=Path, help="输出根目录，默认写回 --repo")
    parser.add_argument("--roundtrip-out", type=Path, help="把反向拼回的原文件另存到这里")
    args = parser.parse_args(argv)
    repo = args.repo or Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, check=True, text=True
        ).stdout.strip()
    )
    out = args.out or repo
    original = args.input.read_bytes()
    try:
        budget = parse(original.decode("utf-8"))
        blob = hashlib.sha1(b"blob %d\0" % len(original) + original, usedforsecurity=False).hexdigest()
        rows, counts = table(repo, budget)
        outputs = {out / LOG_PATH: render_log(budget, blob, rows), out / BUDGET_PATH: render_slim(budget)}
    except Refused as refused:
        print(f"没有搬迁，什么也没写：{refused}", file=sys.stderr)
        return 2
    for path, text in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    log_file, slim_file = out / LOG_PATH, out / BUDGET_PATH
    written_log, written_slim = log_file.read_bytes(), slim_file.read_bytes()
    try:
        rebuilt = reverse(written_slim.decode("utf-8"), written_log.decode("utf-8")).encode("utf-8")
    except Refused as refused:
        print(f"往返校验失败：写出的两个文件拼不回原文件：{refused}", file=sys.stderr)
        return 1
    if args.roundtrip_out is not None:
        args.roundtrip_out.parent.mkdir(parents=True, exist_ok=True)
        args.roundtrip_out.write_bytes(rebuilt)
    print(f"输入：{args.input}（{budget.total:,} 行，blob {blob}）")
    blocks = "、".join(f"{entry.package} {len(entry.block):,}" for entry in budget.entries)
    print(f"切块：总述 {len(budget.summary):,} 行；理由块 {blocks}；合计搬走 {budget.moved:,} 行")
    for path, data in ((log_file, written_log), (slim_file, written_slim)):
        count = data.count(b"\n")
        print(f"写出：{path}（{count:,} 行，sha256 {_sha256(data)}）")
    print(
        f"旧行号引用 {sum(counts.values())} 处：按引入提交那一版定位 {counts['direct']}，"
        f"另找到更早一版 {counts['earlier']}，那一版不是完整语句且往前也没找到 {counts['unaligned']}，"
        f"未能定位 {counts['failed']}"
    )
    same_code = ast.dump(ast.parse(original)) == ast.dump(ast.parse(written_slim))
    print("代码：瘦身前后的 AST 相同，动的只有注释" if same_code else "代码：瘦身前后的 AST 不同")
    if rebuilt == original and same_code:
        print(f"往返校验：从两个输出文件拼回的原文件与输入逐字节相同（sha256 {_sha256(rebuilt)}）")
        return 0
    diff = difflib.unified_diff(
        original.decode("utf-8").splitlines(keepends=True),
        rebuilt.decode("utf-8").splitlines(keepends=True),
        fromfile=str(args.input),
        tofile="拼回的原文件",
    )
    sys.stdout.writelines(diff)
    print("往返校验失败：上面是差异", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
