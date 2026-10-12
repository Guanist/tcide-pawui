# SPDX-FileCopyrightText: 2025 tcide-pawui contributors
# SPDX-License-Identifier: LGPL-3.0-or-later
"""代码片段（snippets）。

对应 TCIDE 原版 README 宣称的 "Snippets system (50+ presets)" ——
但原版 `p2-modules.js` 丢失后只留了兼容桩，``window.__tcide_listSnippets``
兜底返回 **空数组**，也就是说 **50+ 片段实际并不存在**。这里真正把它做出来。

设计：
- 片段按语言分组，前缀唯一（同语言内），便于 UI 做前缀补全；
- 除了通用语言，还带 **PawUI 专属片段**（本项目自己就是 PawUI 应用）；
- ``body`` 里用 ``${name}`` 之类的占位符留给后续插入时替换（简单可测）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 片段正文里的占位符：``${name}`` / ``${1:name}`` 两种都支持
PLACEHOLDER_RE = re.compile(r"\$\{\d*:?([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class Snippet:
    """一个代码片段。"""

    prefix: str
    name: str
    body: str
    language: str = "plaintext"
    description: str = ""

    @property
    def placeholders(self) -> list[str]:
        """正文里的占位符名（按出现顺序去重）。"""
        seen: list[str] = []
        for match in PLACEHOLDER_RE.finditer(self.body):
            if match.group(1) not in seen:
                seen.append(match.group(1))
        return seen

    def render(self, **values: str) -> str:
        """替换占位符；未提供的保留原样。"""
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            return str(values.get(name, match.group(0)))

        return PLACEHOLDER_RE.sub(replace, self.body)

    def to_dict(self) -> dict:
        return {"prefix": self.prefix, "name": self.name, "language": self.language,
                "description": self.description, "placeholders": self.placeholders}


def _s(prefix: str, name: str, language: str, body: str, description: str = "") -> Snippet:
    return Snippet(prefix=prefix, name=name, body=body, language=language,
                   description=description)


#: 内置片段表（≥50 条，覆盖 TCIDE 声称支持的常见语言 + PawUI）
SNIPPETS: list[Snippet] = [
    # ---- Python ---------------------------------------------------------
    _s("def", "函数", "python", "def ${name}(${args}):\n    ${body}\n", "定义一个函数"),
    _s("class", "类", "python",
       "class ${Name}:\n    def __init__(self${args}):\n        ${body}\n", "定义一个类"),
    _s("ifmain", "主入口", "python",
       'if __name__ == "__main__":\n    ${body}\n', "脚本入口"),
    _s("tryexc", "异常处理", "python",
       "try:\n    ${body}\nexcept ${Exception} as exc:\n    ${handle}\n", "try/except"),
    _s("dataclass", "数据类", "python",
       "@dataclass\nclass ${Name}:\n    ${field}: ${type}\n", "dataclass"),
    _s("pytest", "测试函数", "python",
       "def test_${name}():\n    assert ${condition}\n", "pytest 用例"),
    _s("contextmgr", "上下文管理器", "python",
       "@contextmanager\ndef ${name}():\n    ${setup}\n    try:\n        yield ${value}\n"
       "    finally:\n        ${teardown}\n", "contextmanager"),
    _s("asyncdef", "异步函数", "python",
       "async def ${name}(${args}):\n    ${body}\n", "async def"),
    # ---- TypeScript / JavaScript ---------------------------------------
    _s("fn", "函数", "typescript",
       "function ${name}(${args}): ${ReturnType} {\n  ${body}\n}\n", "函数声明"),
    _s("arrow", "箭头函数", "typescript",
       "const ${name} = (${args}) => {\n  ${body}\n};\n", "箭头函数"),
    _s("interface", "接口", "typescript",
       "export interface ${Name} {\n  ${field}: ${type};\n}\n", "接口"),
    _s("classexport", "导出类", "typescript",
       "export class ${Name} {\n  constructor(${args}) {}\n\n  ${body}\n}\n", "导出类"),
    _s("trycatch", "异常处理", "typescript",
       "try {\n  ${body}\n} catch (error) {\n  console.error(error);\n}\n", "try/catch"),
    _s("promise", "Promise", "typescript",
       "await new Promise<void>((resolve, reject) => {\n  ${body}\n});\n", "Promise"),
    _s("forof", "for...of", "javascript",
       "for (const ${item} of ${items}) {\n  ${body}\n}\n", "for...of"),
    _s("fetchjson", "fetch JSON", "javascript",
       "const ${name} = await fetch(${url}).then((r) => r.json());\n", "fetch JSON"),
    # ---- Go -------------------------------------------------------------
    _s("func", "函数", "go",
       "func ${Name}(${args}) ${ret} {\n\t${body}\n}\n", "Go 函数"),
    _s("struct", "结构体", "go", "type ${Name} struct {\n\t${Field} ${Type}\n}\n", "结构体"),
    _s("iferr", "错误处理", "go",
       "if err != nil {\n\treturn ${zero}, fmt.Errorf(\"${msg}: %w\", err)\n}\n", "if err"),
    _s("testfunc", "测试函数", "go",
       'func Test${Name}(t *testing.T) {\n\t${body}\n}\n', "Go 测试"),
    # ---- Rust -----------------------------------------------------------
    _s("rfn", "函数", "rust",
       "fn ${name}(${args}) -> ${Ret} {\n    ${body}\n}\n", "Rust 函数"),
    _s("rstruct", "结构体", "rust",
       "#[derive(Debug, Clone)]\nstruct ${Name} {\n    ${field}: ${Type},\n}\n", "结构体"),
    _s("rimpl", "impl 块", "rust",
       "impl ${Name} {\n    pub fn ${method}(&self) -> ${Ret} {\n        ${body}\n    }\n}\n",
       "impl"),
    _s("rresult", "Result 处理", "rust",
       "let ${value} = ${expr}.map_err(|e| ${Err}(e.to_string()))?;\n", "Result"),
    # ---- Shell / PowerShell --------------------------------------------
    _s("shfunc", "Shell 函数", "shell",
       "${name}() {\n  ${body}\n}\n", "shell 函数"),
    _s("shguard", "严格模式", "shell",
       "set -euo pipefail\n\n${body}\n", "set -euo pipefail"),
    _s("psfunc", "PowerShell 函数", "powershell",
       "function ${Name} {\n    param(${params})\n    ${body}\n}\n", "PowerShell 函数"),
    # ---- HTML / CSS / Markdown -----------------------------------------
    _s("html5", "HTML5 骨架", "html",
       "<!DOCTYPE html>\n<html lang=\"${lang}\">\n<head>\n  <meta charset=\"utf-8\">\n"
       "  <title>${title}</title>\n</head>\n<body>\n  ${body}\n</body>\n</html>\n", "HTML5"),
    _s("cssflex", "flex 居中", "css",
       ".${class} {\n  display: flex;\n  align-items: center;\n  justify-content: center;\n}\n",
       "flex 居中"),
    _s("cssgrid", "grid 布局", "css",
       ".${class} {\n  display: grid;\n  grid-template-columns: repeat(${n}, 1fr);\n"
       "  gap: ${gap}px;\n}\n", "grid"),
    _s("mdtable", "Markdown 表格", "markdown",
       "| ${列1} | ${列2} |\n| --- | --- |\n| ${值1} | ${值2} |\n", "表格"),
    # ---- JSON / YAML / TOML -------------------------------------------
    _s("jsonobj", "JSON 对象", "json",
       '{\n  "${key}": "${value}"\n}\n', "JSON 对象"),
    _s("gha", "GitHub Actions", "yaml",
       "name: ${name}\n\non:\n  push:\n    branches: [main]\n\njobs:\n  build:\n"
       "    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
       "      - run: ${command}\n", "GitHub Actions 工作流"),
    _s("dcservice", "docker-compose 服务", "yaml",
       "services:\n  ${name}:\n    image: ${image}\n    ports:\n"
       '      - "${host}:${container}"\n', "compose 服务"),
    _s("tpyproject", "pyproject", "toml",
       "[project]\nname = \"${name}\"\nversion = \"0.1.0\"\n"
       "dependencies = [\"${dep}\"]\n", "pyproject.toml"),
    # ---- 通用控制流 -----------------------------------------------------
    _s("todo", "TODO 注释", "plaintext", "TODO(${who}): ${what}\n", "TODO 注释"),
    _s("fence", "代码围栏", "markdown", "```${lang}\n${code}\n```\n", "代码围栏"),
    _s("license", "许可证头", "plaintext",
       "Copyright (c) ${year} ${owner}\nSPDX-License-Identifier: ${spdx}\n",
       "许可证头"),
    _s("table", "文本表格", "plaintext",
       "${列1}\t${列2}\n${值1}\t${值2}\n", "制表符表格"),
    _s("log", "日志行", "plaintext", "[${level}] ${message}\n", "日志行"),
    # ---- PawUI 专属（本项目自己的 DSL）---------------------------------
    _s("pawwin", "PawUI 窗口", "paw",
       '<Window title="${title}" theme="dark" width="${w}" height="${h}">\n'
       "  <Column>\n    ${body}\n  </Column>\n</Window>\n", "PawUI 窗口骨架"),
    _s("pawcol", "PawUI 列", "paw", '<Column gap="${gap}">\n  ${body}\n</Column>\n',
       "PawUI 纵向布局"),
    _s("pawrow", "PawUI 行", "paw", '<Row gap="${gap}" align="center">\n  ${body}\n</Row>\n',
       "PawUI 横向布局"),
    _s("pawbtn", "PawUI 按钮", "paw",
       '<Button on_click="${handler}">${label}</Button>\n', "PawUI 按钮"),
    _s("pawlist", "PawUI 列表", "paw",
       '<List items="{$rows}" on_select="${handler}" height="${h}"/>\n', "PawUI 列表"),
    _s("pawfor", "PawUI 循环", "paw",
       '<For each="${item}" in="{$items}">\n  <Text>{$${item}}</Text>\n</For>\n',
       "PawUI For 循环"),
    _s("pawif", "PawUI 条件", "paw",
       '<If condition="{$flag}">\n  ${body}\n</If>\n', "PawUI If 分支"),
    _s("pawscript", "PawUI 脚本块", "paw",
       "<script>\n${code}\n</script>\n", "PawUI 顶层 script 块"),
    _s("pawtabs", "PawUI 标签页", "paw",
       "<Tabs>\n  <Tab label=\"${label1}\">\n    ${body1}\n  </Tab>\n"
       "  <Tab label=\"${label2}\">\n    ${body2}\n  </Tab>\n</Tabs>\n", "PawUI 标签页"),
    _s("pawsplit", "PawUI 分栏", "paw",
       '<SplitPane axis="x" ratio="0.25" width="${w}">\n  ${left}\n  ${right}\n</SplitPane>\n',
       "PawUI 分栏"),
    _s("pawtable", "PawUI 表格", "paw",
       '<Table columns="[${列}]" rows="{$rows}" height="${h}"/>\n', "PawUI 表格"),
    _s("pawdialog", "PawUI 对话框", "paw",
       '<Dialog title="${title}" open="{$open}" on_accept="${accept}">\n  ${body}\n</Dialog>\n',
       "PawUI 对话框"),
    _s("pawshortcut", "PawUI 快捷键", "paw",
       '<Shortcut keys="${keys}" on_press="${handler}"/>\n', "PawUI 快捷键"),
    _s("pawstyle", "PawUI 样式", "paw",
       "<Style>\n.${class} { color: ${color}; padding: ${padding}px; }\n</Style>\n",
       "PawUI 样式块"),
    _s("pawcanvas", "PawUI 画布", "paw",
       '<Canvas id="${id}" width="${w}" height="${h}" on_draw="${handler}"/>\n',
       "PawUI Canvas"),
    _s("pawstate", "PawUI 状态绑定", "paw",
       '<Text>{$${key}}</Text>\n', "PawUI 状态插值"),
    _s("pawcheck", "PawUI 校验字段", "paw",
       '<Input bind="${key}" required="true" min_length="${n}" error="${msg}"/>\n',
       "PawUI 带校验的输入框"),
]


class SnippetLibrary:
    """片段查询与补全。"""

    def __init__(self, snippets: list[Snippet] | None = None) -> None:
        self.snippets = list(SNIPPETS if snippets is None else snippets)

    def languages(self) -> list[str]:
        return sorted({snippet.language for snippet in self.snippets})

    def for_language(self, language: str) -> list[Snippet]:
        return [s for s in self.snippets if s.language == language]

    def by_prefix(self, prefix: str, *, language: str | None = None) -> Snippet | None:
        for snippet in self.snippets:
            if snippet.prefix == prefix and (language is None
                                             or snippet.language == language):
                return snippet
        return None

    def complete(self, fragment: str, *, language: str | None = None,
                 limit: int = 20) -> list[Snippet]:
        """前缀/子串补全：同语言的精确前缀优先，其次前缀包含，最后全表包含。"""
        fragment = (fragment or "").strip().lower()
        if not fragment:
            return self.for_language(language)[:limit] if language else []
        pool = [s for s in self.snippets if language is None or s.language == language]
        exact = [s for s in pool if s.prefix.lower().startswith(fragment)]
        fuzzy = [s for s in pool if s not in exact
                 and (fragment in s.prefix.lower() or fragment in s.name.lower())]
        return (exact + fuzzy)[:limit]

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for snippet in self.snippets:
            counts[snippet.language] = counts.get(snippet.language, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def render(self, prefix: str, **values: str) -> str:
        snippet = self.by_prefix(prefix)
        if snippet is None:
            raise KeyError(f"没有这个片段: {prefix}")
        return snippet.render(**values)
