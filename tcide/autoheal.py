# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""编译/测试错误解析与修复建议（AutoHeal）。

对应 TCIDE 原版的 `core/autoheal/autoheal-manager.ts` —— 该模块在原版
**从未接线，功能静默失效**（作者注释原话：「preload/IPC 从未接线，导致……自动修复
等 README 宣称功能静默失效」）。

诚实定位：这里是**确定性的错误解析 + 可解释的建议**，不是"AI 自动修好"。
每条建议都说明依据（相似符号、同名文件、缩进统计……），并给出置信度。
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field

# --- 各工具/语言的错误行模式（顺序即匹配优先级）--------------------------

#: Python 回溯帧：File "path", line N, in func
PY_FRAME_RE = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>\S+))?')

#: Python 末行异常：NameError: name 'x' is not defined
PY_EXC_RE = re.compile(r"^(?P<kind>[A-Za-z_]*(?:Error|Exception|Interrupt|Exit))"
                       r":\s*(?P<msg>.*)$", re.MULTILINE)

#: Python 语法错误的插入符行（给出列号）
PY_CARET_RE = re.compile(r"^(?P<spaces>\s*)\^", re.MULTILINE)

#: pytest 失败行：FAILED tests/test_a.py::test_b - AssertionError: ...
PYTEST_FAIL_RE = re.compile(r"^(?P<status>FAILED|ERROR)\s+(?P<file>[^\s:]+)"
                            r"(?:::?(?P<test>[\w\[\]\-\.]+))?(?:\s+-\s+(?P<msg>.*))?$",
                            re.MULTILINE)

#: TypeScript：path(line,col): error TS1234: msg
TSC_RE = re.compile(r"^(?P<file>[^\s(]+)\((?P<line>\d+),(?P<col>\d+)\):\s*"
                    r"error\s+(?P<code>TS\d+):\s*(?P<msg>.*)$", re.MULTILINE)

#: gcc / rustc / 通用：path:line:col: error: msg  或 path:line: error: msg
GENERIC_RE = re.compile(r"^(?P<file>[^\s:]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
                        r"(?P<level>error|warning|note):\s*(?P<msg>.*)$", re.MULTILINE)

#: 从消息里抽出被引用的标识符：name 'x' / 'x' is not defined / undefined name 'x'
SYMBOL_RE = re.compile(r"['\"](?P<name>[A-Za-z_][\w\.]*)['\"]")
#: 模块名：No module named 'x'
MODULE_RE = re.compile(r"No module named\s+['\"](?P<name>[\w\.]+)['\"]")

#: 错误类型 -> 归一化的类别
KIND_MAP: tuple[tuple[str, str], ...] = (
    ("SyntaxError", "syntax"), ("IndentationError", "syntax"), ("TabError", "syntax"),
    ("ModuleNotFoundError", "import"), ("ImportError", "import"),
    ("NameError", "name"), ("AttributeError", "attribute"), ("TypeError", "type"),
    ("AssertionError", "test"), ("FileNotFoundError", "file"),
    ("KeyError", "key"), ("IndexError", "key"), ("ValueError", "value"),
)

#: 常见错误的通用建议（确定性、可解释）
GENERIC_ADVICE: dict[str, str] = {
    "syntax": "先运行 get_diagnostics 定位语法错误；注意报错行往往在真正出错位置之后",
    "import": "确认模块已安装（requirements.txt / package.json），或检查导入路径与文件名",
    "name": "检查拼写；若是项目内符号，确认它已定义且导入",
    "attribute": "确认对象类型；属性可能改名或属于另一个对象",
    "type": "检查实参类型；注意 None 与可选值",
    "test": "先看断言行与期望值；必要时最小化复现",
    "file": "检查路径与工作目录；确认文件已创建",
    "key": "检查容器是否包含该键/下标",
    "value": "检查实参取值范围与格式",
    "compile": "查看编译器的完整输出，通常第一条错误才是根因",
    "unknown": "先复现并定位到具体文件与行号",
}


@dataclass
class ParsedError:
    """一条结构化错误。"""

    kind: str = "unknown"
    file: str = ""
    line: int = 0
    column: int = 0
    message: str = ""
    symbol: str = ""
    code: str = ""            # 如 TS2345 / pytest 用例名
    raw: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "file": self.file, "line": self.line,
                "column": self.column, "message": self.message,
                "symbol": self.symbol, "code": self.code}

    def location(self) -> str:
        if not self.file:
            return ""
        base = f"{self.file}:{self.line}" if self.line else self.file
        return f"{base}:{self.column}" if self.column else base


@dataclass
class HealSuggestion:
    """一条修复建议。"""

    action: str
    detail: str = ""
    confidence: float = 0.5
    source: str = "rule"      # rule | index | workspace

    def to_dict(self) -> dict:
        return {"action": self.action, "detail": self.detail,
                "confidence": round(self.confidence, 2), "source": self.source}


@dataclass
class Diagnosis:
    """一条错误 + 它的建议。"""

    error: ParsedError
    suggestions: list[HealSuggestion] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"error": self.error.to_dict(),
                "suggestions": [s.to_dict() for s in self.suggestions]}

    def to_markdown(self) -> str:
        head = f"**{self.error.kind}**"
        if self.error.location():
            head += f" @ `{self.error.location()}`"
        lines = [head, "", self.error.message or "(无消息)"]
        if self.suggestions:
            lines.append("")
            for item in self.suggestions:
                lines.append(f"- {item.action}"
                             + (f"（{item.detail}）" if item.detail else "")
                             + f" · 置信度 {item.confidence:.2f}")
        return "\n".join(lines)


class ErrorParser:
    """把工具输出解析成结构化错误列表。"""

    def parse(self, output: str) -> list[ParsedError]:
        text = output or ""
        errors: list[ParsedError] = []

        # 1) TypeScript 风格（带列号，最具体）
        for match in TSC_RE.finditer(text):
            errors.append(ParsedError(
                kind="compile", file=match.group("file"),
                line=int(match.group("line")), column=int(match.group("col")),
                message=match.group("msg").strip(), code=match.group("code"),
                raw=match.group(0)))

        # 2) pytest 失败行
        for match in PYTEST_FAIL_RE.finditer(text):
            errors.append(ParsedError(
                kind="test", file=match.group("file"),
                message=(match.group("msg") or match.group("status")).strip(),
                code=match.group("test") or "", raw=match.group(0)))

        # 3) Python 回溯：帧 + 末行异常
        frames = list(PY_FRAME_RE.finditer(text))
        exceptions = list(PY_EXC_RE.finditer(text))
        if exceptions:
            last = exceptions[-1]
            kind_name = last.group("kind")
            frames_for_file = [f for f in frames]
            frame = frames_for_file[-1] if frames_for_file else None
            message = last.group("msg").strip()
            symbol = ""
            module = MODULE_RE.search(message)
            if module:
                symbol = module.group("name")
            else:
                found = SYMBOL_RE.search(message)
                if found:
                    symbol = found.group("name")
            errors.append(ParsedError(
                kind=self._kind_of(kind_name), file=frame.group("file") if frame else "",
                line=int(frame.group("line")) if frame else 0,
                message=f"{kind_name}: {message}", symbol=symbol,
                code=kind_name, raw=last.group(0)))
            # 语法错误：从插入符位置推列号
            if kind_name in ("SyntaxError", "IndentationError", "TabError"):
                caret = PY_CARET_RE.search(text)
                if caret and errors:
                    errors[-1].column = len(caret.group("spaces")) + 1

        # 4) gcc / rustc / 通用 "file:line: error: msg"
        for match in GENERIC_RE.finditer(text):
            if match.group("level") != "error":
                continue
            errors.append(ParsedError(
                kind="compile", file=match.group("file"),
                line=int(match.group("line")),
                column=int(match.group("col") or 0),
                message=match.group("msg").strip(), raw=match.group(0)))

        # 去重（同一 file:line:message 只留一条）
        unique: list[ParsedError] = []
        seen: set[tuple] = set()
        for error in errors:
            key = (error.file, error.line, error.column, error.message[:80])
            if key in seen:
                continue
            seen.add(key)
            unique.append(error)
        return unique

    @staticmethod
    def _kind_of(exception_name: str) -> str:
        for name, kind in KIND_MAP:
            if exception_name == name:
                return kind
        if exception_name.endswith(("Error", "Exception")):
            return "unknown"
        return "unknown"


class AutoHeal:
    """根据结构化错误给出可解释的修复建议。"""

    def __init__(self, index=None, workspace=None) -> None:
        self.index = index
        self.workspace = workspace
        self.parser = ErrorParser()

    # -- 建议 -------------------------------------------------------------

    def suggest(self, error: ParsedError) -> list[HealSuggestion]:
        suggestions: list[HealSuggestion] = []

        if error.symbol:
            suggestions.extend(self._symbol_advice(error))
        if error.kind == "import" and error.symbol:
            suggestions.extend(self._import_advice(error))
        if error.kind == "syntax":
            suggestions.extend(self._syntax_advice(error))
        if error.kind == "test":
            suggestions.extend(self._test_advice(error))

        advice = GENERIC_ADVICE.get(error.kind)
        if advice:
            suggestions.append(HealSuggestion(action=advice, confidence=0.4,
                                              source="rule"))
        return suggestions

    def _symbol_advice(self, error: ParsedError) -> list[HealSuggestion]:
        """按索引里的真实符号给出"是不是想写 X"。"""
        out: list[HealSuggestion] = []
        if self.index is None or not error.symbol:
            return out
        names = [s.name for s in self.index.symbols()]
        if not names:
            return out
        base = error.symbol.split(".")[0]
        if base in names:
            out.append(HealSuggestion(
                action=f"符号 {base} 在项目里有定义，检查是否忘了导入或写错了作用域",
                confidence=0.6, source="index"))
            return out
        close = difflib.get_close_matches(base, sorted(set(names)), n=3, cutoff=0.6)
        if close:
            out.append(HealSuggestion(
                action=f"未定义的 {base}，是否想写 {' / '.join(close)}？",
                detail="按项目符号表做近似匹配", confidence=0.7, source="index"))
        else:
            out.append(HealSuggestion(
                action=f"项目里没有近似于 {base} 的符号，可能确实需要新建或导入外部库",
                confidence=0.5, source="index"))
        return out

    def _import_advice(self, error: ParsedError) -> list[HealSuggestion]:
        out: list[HealSuggestion] = []
        module = (error.symbol or "").split(".")[0]
        if not module or self.workspace is None:
            return out
        candidates = [f"{module}.py", f"{module}/__init__.py", f"{module}.ts",
                      f"{module}.js", f"{module}/index.ts", f"{module}/index.js"]
        found = [path for path in candidates if self.workspace.exists(path)]
        if found:
            out.append(HealSuggestion(
                action=f"模块 {module} 在项目内存在（{found[0]}），检查导入路径/包结构",
                confidence=0.75, source="workspace"))
        else:
            out.append(HealSuggestion(
                action=f"项目内没有 {module}，需要安装依赖或补上该文件",
                detail="已按 6 种常见模块布局查找", confidence=0.6, source="workspace"))
        return out

    def _syntax_advice(self, error: ParsedError) -> list[HealSuggestion]:
        out = [HealSuggestion(
            action="语法错误：删掉报错行的上一行内容试试（解析器常在下一行才报错）",
            confidence=0.5)]
        if error.column:
            out.append(HealSuggestion(
                action=f"从第 {error.column} 列开始检查括号/引号是否配对",
                confidence=0.45))
        return out

    def _test_advice(self, error: ParsedError) -> list[HealSuggestion]:
        out: list[HealSuggestion] = []
        if error.code:
            out.append(HealSuggestion(
                action=f"单独跑这个用例：pytest -q {error.code}",
                confidence=0.6))
        if error.file and self.workspace is not None and self.workspace.exists(error.file):
            out.append(HealSuggestion(
                action=f"打开 {error.file} 查看断言上下文", confidence=0.5))
        return out

    # -- 主流程 -----------------------------------------------------------

    def analyze(self, output: str) -> list[Diagnosis]:
        return [Diagnosis(error=error, suggestions=self.suggest(error))
                for error in self.parser.parse(output)]

    def report(self, output: str) -> str:
        diagnoses = self.analyze(output)
        if not diagnoses:
            return "没有从输出里解析出可识别的错误"
        lines = [f"解析出 {len(diagnoses)} 个错误：", ""]
        for diagnosis in diagnoses:
            lines.append(diagnosis.to_markdown())
            lines.append("")
        return "\n".join(lines).rstrip()

    def dominant_indent(self, path: str) -> str:
        """统计文件里最常见的缩进（用于「是不是缩进混了」的判断）。"""
        if self.workspace is None:
            return ""
        try:
            text = self.workspace.read(path)
        except Exception:  # noqa: BLE001
            return ""
        counter: Counter[str] = Counter()
        for line in text.splitlines():
            stripped = line.lstrip(" \t")
            if stripped and stripped != line:
                counter[line[:len(line) - len(stripped)]] += 1
        return counter.most_common(1)[0][0] if counter else ""
