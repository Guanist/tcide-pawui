# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""架构分析：循环依赖、分层、扇入扇出、代码异味。

对应 TCIDE 原版的 `core/arch/arch-analyzer.ts` 与 `indexer/warehouse-analyzer.ts`。
这里只做**可验证的图论与度量**，不做"AI 判断架构好坏"：

- ``cycles()`` 用 **Tarjan 迭代版**求强连通分量（真算法，不是启发式）；
- ``layers()`` 给出拓扑分层，环上节点单独成层；
- ``fan_in`` / ``fan_out`` 是真实的依赖计数；
- ``smells()`` 每条都有明确阈值与依据（超长函数、上帝文件、深层目录、无主文件）。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

#: 默认阈值（可覆盖，便于测试与按项目调整）
DEFAULT_LONG_FUNCTION = 80        # 函数超过多少行算长
DEFAULT_GOD_SYMBOLS = 40          # 单文件符号数超过多少算"上帝文件"
DEFAULT_GOD_LINES = 800           # 单文件行数超过多少算"上帝文件"
DEFAULT_DEEP_DEPTH = 5            # 目录深度超过多少算深


@dataclass
class Smell:
    """一条代码异味（带明确依据）。"""

    kind: str
    path: str
    detail: str
    metric: float = 0.0
    severity: str = "low"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "path": self.path, "detail": self.detail,
                "metric": self.metric, "severity": self.severity}


@dataclass
class ArchReport:
    """一次架构分析的结果。"""

    files: int = 0
    edges: int = 0
    cycles: list[list[str]] = field(default_factory=list)
    layers: list[list[str]] = field(default_factory=list)
    fan_in: dict[str, int] = field(default_factory=dict)
    fan_out: dict[str, int] = field(default_factory=dict)
    orphans: list[str] = field(default_factory=list)
    smells: list[Smell] = field(default_factory=list)

    @property
    def acyclic(self) -> bool:
        return not self.cycles

    def to_dict(self) -> dict:
        return {"files": self.files, "edges": self.edges, "acyclic": self.acyclic,
                "cycles": self.cycles,
                "layers": self.layers,
                "layer_count": len(self.layers),
                "fan_in": dict(sorted(self.fan_in.items(), key=lambda kv: -kv[1])[:20]),
                "fan_out": dict(sorted(self.fan_out.items(), key=lambda kv: -kv[1])[:20]),
                "orphans": self.orphans,
                "smells": [s.to_dict() for s in self.smells]}

    def summary(self) -> str:
        return (f"{self.files} 个文件 / {self.edges} 条依赖边；"
                f"循环依赖 {len(self.cycles)} 组；拓扑 {len(self.layers)} 层；"
                f"无主文件 {len(self.orphans)} 个；异味 {len(self.smells)} 条")


class ArchAnalyzer:
    """在 :class:`~tcide.indexer.ProjectIndex` 的依赖图上做分析。"""

    def __init__(self, index, *, long_function: int = DEFAULT_LONG_FUNCTION,
                 god_symbols: int = DEFAULT_GOD_SYMBOLS,
                 god_lines: int = DEFAULT_GOD_LINES,
                 deep_depth: int = DEFAULT_DEEP_DEPTH) -> None:
        self.index = index
        self.long_function = long_function
        self.god_symbols = god_symbols
        self.god_lines = god_lines
        self.deep_depth = deep_depth

    # -- 图 ---------------------------------------------------------------

    def graph(self) -> dict[str, list[str]]:
        return self.index.dependency_graph()

    def cycles(self) -> list[list[str]]:
        """Tarjan 强连通分量（迭代实现，避免深图递归爆栈）。

        只返回规模 > 1 的分量，以及自环文件。
        """
        graph = self.graph()
        index_of: dict[str, int] = {}
        low: dict[str, int] = {}
        on_stack: set[str] = set()
        stack: list[str] = []
        result: list[list[str]] = []
        counter = 0

        for root in graph:
            if root in index_of:
                continue
            work: list[tuple[str, int]] = [(root, 0)]
            while work:
                node, child_index = work.pop()
                if child_index == 0:
                    index_of[node] = counter
                    low[node] = counter
                    counter += 1
                    stack.append(node)
                    on_stack.add(node)
                recursed = False
                children = graph.get(node, [])
                for position in range(child_index, len(children)):
                    child = children[position]
                    if child not in index_of:
                        work.append((node, position + 1))
                        work.append((child, 0))
                        recursed = True
                        break
                    if child in on_stack:
                        low[node] = min(low[node], index_of[child])
                if recursed:
                    continue
                if low[node] == index_of[node]:
                    component: list[str] = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    component.sort()
                    if len(component) > 1:
                        result.append(component)
                    elif component and component[0] in graph.get(component[0], []):
                        result.append(component)      # 自环
                if work:
                    parent, _ = work[-1]
                    low[parent] = min(low[parent], low[node])
        return sorted(result, key=lambda group: (len(group), group), reverse=True)

    def layers(self) -> list[list[str]]:
        """拓扑分层：第 0 层是不依赖任何项目内文件的文件。

        环会让拓扑排序无解，所以**先按强连通分量缩点**再分层。
        """
        graph = self.graph()
        component_of: dict[str, int] = {}
        for number, group in enumerate(self.cycles()):
            for node in group:
                component_of[node] = number
        next_id = len(self._cycle_groups())
        for node in graph:
            if node not in component_of:
                component_of[node] = next_id
                next_id += 1

        # 缩点后的边（去自环）
        dag: dict[int, set[int]] = defaultdict(set)
        for node, children in graph.items():
            source = component_of[node]
            for child in children:
                target = component_of.get(child)
                if target is not None and target != source:
                    dag[source].add(target)

        # 计算每个分量的层级 = 最长依赖链（被依赖者层号更小）
        memo: dict[int, int] = {}

        def level(component: int, guard: set[int] | None = None) -> int:
            if component in memo:
                return memo[component]
            guard = guard or set()
            if component in guard:
                return 0
            guard.add(component)
            value = 0
            for child in dag.get(component, ()):  # 依赖别人 -> 我在别人上面
                value = max(value, level(child, guard) + 1)
            guard.discard(component)
            memo[component] = value
            return value

        grouped: dict[int, list[str]] = defaultdict(list)
        for node, component in component_of.items():
            grouped[level(component)].append(node)
        return [sorted(grouped[key]) for key in sorted(grouped)]

    def _cycle_groups(self) -> list[list[str]]:
        return self.cycles()

    def fan_in(self) -> dict[str, int]:
        """被依赖数（越高越核心）。"""
        counts: dict[str, int] = {path: 0 for path in self.graph()}
        for _node, children in self.graph().items():
            for child in children:
                counts[child] = counts.get(child, 0) + 1
        return counts

    def fan_out(self) -> dict[str, int]:
        """对外依赖数。"""
        return {node: len(children) for node, children in self.graph().items()}

    def orphans(self) -> list[str]:
        """既不被依赖、也不依赖别人的文件（可能是死代码或入口）。"""
        fan_in, fan_out = self.fan_in(), self.fan_out()
        return sorted(path for path in self.graph()
                      if fan_in.get(path, 0) == 0 and fan_out.get(path, 0) == 0)

    # -- 异味 -------------------------------------------------------------

    def smells(self) -> list[Smell]:
        smells: list[Smell] = []
        fan_in = self.fan_in()

        for path, entry in self.index.files.items():
            lines = entry.lines
            symbol_count = len(entry.symbols)
            depth = path.count("/")
            if lines >= self.god_lines or symbol_count >= self.god_symbols:
                smells.append(Smell(
                    kind="god-file", path=path,
                    detail=f"{lines} 行 / {symbol_count} 个符号，建议按职责拆分",
                    metric=float(lines), severity="high" if lines >= self.god_lines * 2
                    else "medium"))
            if depth >= self.deep_depth:
                smells.append(Smell(
                    kind="deep-path", path=path,
                    detail=f"目录深度 {depth}，考虑扁平化",
                    metric=float(depth), severity="low"))
            for symbol in entry.symbols:
                length = max(0, symbol.end_line - symbol.line)
                if symbol.kind in ("function", "method") and length >= self.long_function:
                    smells.append(Smell(
                        kind="long-function", path=path,
                        detail=f"{symbol.name} 约 {length} 行，建议提取子函数",
                        metric=float(length),
                        severity="high" if length >= self.long_function * 2 else "medium"))
            if entry.error:
                smells.append(Smell(
                    kind="parse-error", path=path, detail=entry.error,
                    severity="high"))

        for path, count in fan_in.items():
            if count >= 6:
                smells.append(Smell(
                    kind="high-fan-in", path=path,
                    detail=f"被 {count} 个文件依赖，改动影响面大，建议加测试保护",
                    metric=float(count), severity="medium"))

        for group in self.cycles():
            smells.append(Smell(
                kind="dependency-cycle",
                path=" ↔ ".join(group),
                detail=f"{len(group)} 个文件互相依赖，无法单独理解/测试",
                metric=float(len(group)), severity="high"))

        severity_rank = {"high": 0, "medium": 1, "low": 2}
        return sorted(smells, key=lambda s: (severity_rank.get(s.severity, 3),
                                            -s.metric, s.path))

    # -- 汇总 -------------------------------------------------------------

    def analyze(self) -> ArchReport:
        graph = self.graph()
        return ArchReport(
            files=len(graph),
            edges=sum(len(children) for children in graph.values()),
            cycles=self.cycles(),
            layers=self.layers(),
            fan_in=self.fan_in(),
            fan_out=self.fan_out(),
            orphans=self.orphans(),
            smells=self.smells(),
        )

    def report(self) -> str:
        data = self.analyze()
        lines = ["# 架构分析", "", data.summary(), ""]
        if data.cycles:
            lines.append("## 循环依赖")
            for group in data.cycles:
                lines.append(f"- {' ↔ '.join(group)}")
            lines.append("")
        lines.append("## 分层（0 = 最底层，不依赖任何项目内文件）")
        for number, files in enumerate(data.layers):
            preview = "、".join(files[:6]) + ("…" if len(files) > 6 else "")
            lines.append(f"- 第 {number} 层（{len(files)} 个）：{preview}")
        lines.append("")
        if data.fan_in:
            top = sorted(data.fan_in.items(), key=lambda kv: -kv[1])[:5]
            lines.append("## 被依赖最多")
            lines += [f"- `{path}`：{count} 个文件依赖" for path, count in top if count]
            lines.append("")
        if data.smells:
            lines.append(f"## 代码异味（{len(data.smells)} 条，按严重度排序）")
            for smell in data.smells[:15]:
                lines.append(f"- [{smell.severity}] {smell.kind} `{smell.path}`："
                             f"{smell.detail}")
        return "\n".join(lines)
