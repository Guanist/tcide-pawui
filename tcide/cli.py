# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""命令行入口（库侧：不依赖 PawUI，也不需要 Qt）。

对应 TCIDE 原版只有 GUI、没有可脚本化的 CLI。这里把 `tcide/` 的能力全部暴露出来：

    tcide check [路径...]      静态检查（lint 规则 + 语法错误），有 error 则退出码 1
    tcide lint [路径...]       同 check，附按规则统计
    tcide analyze              架构分析（循环依赖/分层/扇入扇出/异味）
    tcide index                索引概览（语言分布、符号最多的文件、解析错误）
    tcide search <词>          跨文件搜索
    tcide outline <文件>       符号大纲
    tcide snippet [前缀]       列出片段 / 展开片段
    tcide complete <前缀>      补全建议
    tcide hex <文件>           Hex 视图
    tcide git [status|diff|suggest]   Git 状态 / 差异 / 提交建议
    tcide heal <文件|->        解析错误输出并给出修复建议
    tcide version

设计约定：
- **`main()` 返回退出码**，不做 `sys.exit`，便于测试与嵌入；
- ``--json`` 输出的都是合法 JSON（供脚本消费）；
- 所有路径经 `Workspace`，越界会被拒。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .arch import ArchAnalyzer
from .autoheal import AutoHeal
from .completion import Completer
from .debug import DebugConfigStore, DebugSession
from .gitintel import GitIntelligence
from .gitops import GitError, GitRepo
from .hexview import HexViewer
from .indexer import ProjectIndex
from .lint import SEVERITIES, Linter
from .memory import ProjectMemoryBuilder
from .perf import PerfMonitor
from .snippets import SnippetLibrary
from .workspace import Workspace, WorkspaceError

__version__ = "0.1.0"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


# ---------------------------------------------------------------------------
# 公共装配
# ---------------------------------------------------------------------------

class Context:
    """把常用对象按需构造一次（避免每个子命令都重新扫描）。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._workspace: Workspace | None = None
        self._index: ProjectIndex | None = None
        self._memory = None

    @property
    def workspace(self) -> Workspace:
        if self._workspace is None:
            self._workspace = Workspace(self.root)
        return self._workspace

    @property
    def index(self) -> ProjectIndex:
        if self._index is None:
            self._index = ProjectIndex(self.workspace)
            self._index.scan()
        return self._index

    @property
    def memory(self):
        if self._memory is None:
            self._memory = ProjectMemoryBuilder(self.workspace).build()
        return self._memory


def _emit(payload, *, as_json: bool, text: str = "") -> None:
    """统一输出：``--json`` 时打 JSON，否则打人类可读文本。"""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif text:
        print(text)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _resolve_targets(context: Context, paths: list[str]) -> list[str]:
    """把命令行给的路径收敛成「相对项目根的路径」列表。"""
    if not paths:
        return list(context.workspace.iter_files())
    targets: list[str] = []
    for item in paths:
        resolved = context.workspace.resolve(item)
        if resolved.is_dir():
            prefix = context.workspace.relative(resolved)
            prefix = f"{prefix}/" if prefix else ""
            targets += [path for path in context.workspace.iter_files()
                        if path.startswith(prefix)]
        else:
            targets.append(context.workspace.relative(resolved))
    return sorted(set(targets))


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_check(args, context: Context) -> int:
    linter = Linter(context.workspace, max_line_length=args.max_line)
    issues = []
    for path in _resolve_targets(context, args.paths):
        issues += linter.lint_file(path)
    stats = Linter.summary(issues)
    _emit({"summary": stats,
           "issues": [issue.to_dict() for issue in issues]},
          as_json=args.json, text=Linter.report(issues, limit=args.limit))
    if args.json is False:
        print(f"\n{stats['total']} 条：错误 {stats['by_severity']['error']}，"
              f"警告 {stats['by_severity']['warning']}，提示 {stats['by_severity']['info']}")
    failing = stats["by_severity"]["error"]
    if args.strict:
        failing += stats["by_severity"]["warning"]
    return EXIT_FAILURE if failing else EXIT_OK


def cmd_lint(args, context: Context) -> int:
    linter = Linter(context.workspace, max_line_length=args.max_line)
    issues = []
    for path in _resolve_targets(context, args.paths):
        issues += linter.lint_file(path)
    stats = Linter.summary(issues)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return EXIT_FAILURE if stats["by_severity"]["error"] else EXIT_OK
    print(f"共 {stats['total']} 条")
    for severity in SEVERITIES:
        print(f"  {severity:8} {stats['by_severity'][severity]}")
    print()
    print("按规则：")
    for rule, count in stats["by_rule"].items():
        print(f"  {rule:22} {count}")
    return EXIT_FAILURE if stats["by_severity"]["error"] else EXIT_OK


def cmd_analyze(args, context: Context) -> int:
    analyzer = ArchAnalyzer(context.index, long_function=args.long_function,
                            god_lines=args.god_lines)
    report = analyzer.analyze()
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(analyzer.report())
    # 有循环依赖就算失败（架构问题里最硬的一条）
    return EXIT_FAILURE if report.cycles else EXIT_OK


def cmd_index(args, context: Context) -> int:
    index = context.index
    data = {"stats": index.last_stats.to_dict(), "summary": index.summary()}
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return EXIT_OK
    stats = data["stats"]
    print(f"文件 {stats['files']}（跳过 {stats['skipped']}），符号 {stats['symbols']}，"
          f"用时 {stats['duration_ms']}ms")
    print("语言分布：" + "、".join(f"{k}({v})" for k, v in
                                  list(stats["by_language"].items())[:8]))
    summary = data["summary"]
    if summary["errors"]:
        print("\n解析失败：")
        for path, error in summary["errors"][:5]:
            print(f"  {path}: {error}")
    if summary["most_symbols"]:
        print("\n符号最多：")
        for path, count in summary["most_symbols"]:
            print(f"  {count:4} {path}")
    return EXIT_OK


def cmd_search(args, context: Context) -> int:
    hits = context.workspace.search(args.query, regex=args.regex,
                                    case_sensitive=args.case,
                                    include=args.include or None,
                                    max_results=args.limit)
    if args.json:
        print(json.dumps([hit.to_dict() for hit in hits], ensure_ascii=False,
                         indent=2))
    elif not hits:
        print("（无命中）")
    else:
        for hit in hits:
            print(f"{hit.path}:{hit.line}: {hit.text}")
    return EXIT_OK if hits else EXIT_FAILURE


def cmd_outline(args, context: Context) -> int:
    path = context.workspace.relative(context.workspace.resolve(args.file))
    symbols = context.index.outline(path)
    if args.json:
        print(json.dumps([symbol.to_dict() for symbol in symbols],
                         ensure_ascii=False, indent=2))
        return EXIT_OK
    if not symbols:
        print(f"（{path} 没有提取到符号）")
        return EXIT_FAILURE
    for symbol in symbols:
        flag = "" if symbol.precise else "  (正则推断)"
        print(f"{symbol.line:>5}  {symbol.kind:<9} {symbol.detail or symbol.name}{flag}")
    return EXIT_OK


def cmd_snippet(args, context: Context) -> int:
    library = SnippetLibrary()
    if not args.prefix:
        if args.json:
            print(json.dumps([s.to_dict() for s in library.snippets],
                             ensure_ascii=False, indent=2))
            return EXIT_OK
        print(f"共 {len(library.snippets)} 条片段：")
        for language, count in library.counts().items():
            print(f"  {language:12} {count}")
        return EXIT_OK
    snippet = library.by_prefix(args.prefix)
    if snippet is None:
        print(f"tcide: 没有这个片段: {args.prefix}", file=sys.stderr)
        return EXIT_FAILURE
    values = dict(item.split("=", 1) for item in args.set if "=" in item)
    if args.json:
        print(json.dumps({"prefix": snippet.prefix, "name": snippet.name,
                          "language": snippet.language,
                          "placeholders": snippet.placeholders,
                          "body": snippet.render(**values)},
                         ensure_ascii=False, indent=2))
    else:
        print(snippet.render(**values), end="")
    return EXIT_OK


def cmd_complete(args, context: Context) -> int:
    completer = Completer(context.index, workspace=context.workspace)
    results = completer.complete(args.prefix, path=args.file, limit=args.limit)
    if args.json:
        print(json.dumps([item.to_dict() for item in results],
                         ensure_ascii=False, indent=2))
    elif not results:
        print("（没有建议）")
    else:
        for item in results:
            print(f"{item.score:4.1f}  {item.kind:<8} {item.text}")
    return EXIT_OK if results else EXIT_FAILURE


def cmd_hex(args, context: Context) -> int:
    viewer = HexViewer(context.workspace, width=args.width)
    path = context.workspace.relative(context.workspace.resolve(args.file))
    if args.info:
        data = viewer.info(path)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return EXIT_OK
    if args.find:
        offsets = viewer.find(path, args.find, limit=args.limit)
        print(json.dumps(offsets) if args.json
              else "、".join(str(offset) for offset in offsets) or "（没找到）")
        return EXIT_OK if offsets else EXIT_FAILURE
    print(viewer.render(path, offset=args.offset, rows=args.rows))
    return EXIT_OK


def cmd_git(args, context: Context) -> int:
    repo = GitRepo(context.root)
    if not repo.is_repo():
        print(f"（{context.root} 不是 git 仓库）")
        return EXIT_FAILURE
    if args.action == "status":
        repo_summary = repo.summary()
        status = repo.status()
        data = {"summary": repo_summary,
                "changes": [change.to_dict() for change in status.changes]}
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"分支 {repo_summary['branch']}"
                  + (f" → {repo_summary['upstream']}" if repo_summary["upstream"] else ""))
            print(f"暂存 {repo_summary['staged']} / 未暂存 {repo_summary['unstaged']}"
                  f" / 未跟踪 {repo_summary['untracked']} / 冲突 {repo_summary['conflicted']}")
            for change in status.changes:
                print(f"  {change.kind:<10} {change.path}")
        return EXIT_OK
    if args.action == "diff":
        diff = repo.diff(staged=args.staged, path=args.path or None)
        print(diff or "（无差异）")
        return EXIT_OK
    intelligence = GitIntelligence(repo, context.index)
    if args.action == "suggest":
        suggestion = intelligence.suggest_commit(staged=args.staged)
        if args.json:
            print(json.dumps(suggestion.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(suggestion.message)
        return EXIT_OK
    if args.action == "report":
        print(intelligence.report(staged=args.staged))
        return EXIT_OK
    print(f"tcide: 未知 git 动作 {args.action}", file=sys.stderr)
    return EXIT_USAGE


def cmd_heal(args, context: Context) -> int:
    if args.file in ("-", ""):
        output = sys.stdin.read()
    else:
        output = context.workspace.read(args.file)
    heal = AutoHeal(context.index, context.workspace)
    diagnoses = heal.analyze(output)
    if args.json:
        print(json.dumps([item.to_dict() for item in diagnoses],
                         ensure_ascii=False, indent=2))
    else:
        print(heal.report(output))
    return EXIT_OK if not diagnoses else EXIT_FAILURE


def cmd_memory(args, context: Context) -> int:
    memory = context.memory
    if args.json:
        print(json.dumps(memory.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(ProjectMemoryBuilder.to_markdown(memory))
    return EXIT_OK


def cmd_debug(args, context: Context) -> int:
    if args.action == "defaults":
        config = DebugConfigStore.defaults_for(context.memory)
        print(json.dumps(config.to_dict(), ensure_ascii=False, indent=2))
        return EXIT_OK
    store = DebugConfigStore(context.workspace)
    store.load()
    if args.action == "list":
        print(json.dumps(store.to_json(), ensure_ascii=False, indent=2)
              if args.json else "\n".join(store.names()) or "（没有启动配置）")
        return EXIT_OK
    if args.action == "breakpoints":
        session = DebugSession()
        for item in args.set or []:
            point = None
            from .debug import Breakpoint

            point = Breakpoint.parse(item)
            if point is not None:
                session.add(point.path, point.line)
        print(session.report())
        return EXIT_OK
    return EXIT_USAGE


def cmd_perf(args, context: Context) -> int:
    monitor = PerfMonitor(budget_mb=args.budget)
    for name in args.measure or []:
        with monitor.measure(name, track_memory=True):
            context.workspace.read(name) if context.workspace.exists(name) else None
    monitor.sample()
    if args.json:
        print(json.dumps(monitor.snapshot(), ensure_ascii=False, indent=2))
    else:
        print(monitor.report())
    return EXIT_OK


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tcide", description="虎猫 TCIDE 的命令行（库侧：不依赖 PawUI/Qt）")
    parser.add_argument("-C", "--project", default=".", help="项目根目录（默认当前目录）")
    parser.add_argument("--version", action="version",
                        version=f"tcide {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    def add(name: str, help_text: str):
        sub = subparsers.add_parser(name, help=help_text)
        return sub

    check = add("check", "静态检查（有 error 则退出码 1）")
    check.add_argument("paths", nargs="*", help="文件或目录（默认全项目）")
    check.add_argument("--json", action="store_true", help="输出 JSON")
    check.add_argument("--strict", action="store_true", help="warning 也算失败")
    check.add_argument("--max-line", type=int, default=120, help="行长度上限")
    check.add_argument("--limit", type=int, default=30, help="文本输出最多几条")
    check.set_defaults(func=cmd_check)

    lint = add("lint", "按规则统计")
    lint.add_argument("paths", nargs="*")
    lint.add_argument("--json", action="store_true")
    lint.add_argument("--max-line", type=int, default=120)
    lint.set_defaults(func=cmd_lint)

    analyze = add("analyze", "架构分析（循环依赖/分层/异味）")
    analyze.add_argument("--json", action="store_true")
    analyze.add_argument("--long-function", type=int, default=80)
    analyze.add_argument("--god-lines", type=int, default=800)
    analyze.set_defaults(func=cmd_analyze)

    index = add("index", "索引概览")
    index.add_argument("--json", action="store_true")
    index.set_defaults(func=cmd_index)

    search = add("search", "跨文件搜索")
    search.add_argument("query")
    search.add_argument("--regex", action="store_true")
    search.add_argument("--case", action="store_true", help="区分大小写")
    search.add_argument("--include", default="", help="文件名 glob，如 *.py")
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=cmd_search)

    outline = add("outline", "符号大纲")
    outline.add_argument("file")
    outline.add_argument("--json", action="store_true")
    outline.set_defaults(func=cmd_outline)

    snippet = add("snippet", "列出或展开代码片段")
    snippet.add_argument("prefix", nargs="?", default="")
    snippet.add_argument("--set", action="append", default=[],
                         help="占位符赋值，如 --set name=foo")
    snippet.add_argument("--json", action="store_true")
    snippet.set_defaults(func=cmd_snippet)

    complete = add("complete", "补全建议")
    complete.add_argument("prefix")
    complete.add_argument("--file", default="", help="按哪个文件判断语言")
    complete.add_argument("--limit", type=int, default=20)
    complete.add_argument("--json", action="store_true")
    complete.set_defaults(func=cmd_complete)

    hex_command = add("hex", "十六进制视图")
    hex_command.add_argument("file")
    hex_command.add_argument("--rows", type=int, default=16)
    hex_command.add_argument("--offset", type=int, default=0)
    hex_command.add_argument("--width", type=int, default=16)
    hex_command.add_argument("--info", action="store_true", help="只看文件信息")
    hex_command.add_argument("--find", default="", help="搜索字节串")
    hex_command.add_argument("--limit", type=int, default=20)
    hex_command.add_argument("--json", action="store_true")
    hex_command.set_defaults(func=cmd_hex)

    git = add("git", "Git 状态 / 差异 / 提交建议")
    git.add_argument("action", nargs="?", default="status",
                     choices=["status", "diff", "suggest", "report"])
    git.add_argument("--staged", action="store_true")
    git.add_argument("--path", default="")
    git.add_argument("--json", action="store_true")
    git.set_defaults(func=cmd_git)

    heal = add("heal", "解析错误输出并给出修复建议（文件或 - 读 stdin）")
    heal.add_argument("file", nargs="?", default="-")
    heal.add_argument("--json", action="store_true")
    heal.set_defaults(func=cmd_heal)

    memory = add("memory", "项目静态上下文（含规则）")
    memory.add_argument("--json", action="store_true")
    memory.set_defaults(func=cmd_memory)

    debug = add("debug", "启动配置与断点")
    debug.add_argument("action", nargs="?", default="defaults",
                       choices=["defaults", "list", "breakpoints"])
    debug.add_argument("--set", action="append", default=[])
    debug.add_argument("--json", action="store_true")
    debug.set_defaults(func=cmd_debug)

    perf = add("perf", "性能采样（Python 侧）")
    perf.add_argument("--measure", action="append", default=[],
                      help="顺便测一下读这个文件的耗时")
    perf.add_argument("--budget", type=float, default=512.0, help="内存预算 MB")
    perf.add_argument("--json", action="store_true")
    perf.set_defaults(func=cmd_perf)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    try:
        context = Context(args.project)
    except WorkspaceError as exc:
        print(f"tcide: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    try:
        return int(args.func(args, context))
    except WorkspaceError as exc:
        print(f"tcide: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except GitError as exc:
        print(f"tcide: git 失败: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except FileNotFoundError as exc:
        print(f"tcide: 找不到文件: {exc}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
