# Tiger Cat TCIDE · PawUI Edition

> A rewrite of [TCIDE](https://github.com/Guanist/TCIDE) on top of **PawUI**
> (a declarative Python UI layer) — a personal AI coding IDE with a
> **Builder + Coder dual-agent engine**, context compression, Git intelligence,
> architecture analysis and a pixel pet.

[中文](README.md)

---

## What this is

The original TCIDE is 30,211 lines of TypeScript + Electron. This project
re-implements its capabilities on PawUI, and — more importantly — **actually
implements the features its README advertised but that were silently dead**.

The original author left the list in their own source (`src/main/ipc-handlers.ts:43`):

> These modules were only ever called from the renderer; preload/IPC was never
> wired up, so features advertised in the README — *code entropy evaluation,
> smart context, Git intelligence, repository analysis, unattended runs,
> semantic completion* — **silently did nothing**.

And `recovered-modules/p2-modules.js`:

> The original p2-modules.js was lost from the repository history; the globals
> it used to define (`acceptDiff` / `closeDiffModal`, …) … `__tcide_listSnippets`
> falls back to returning `[]`

In other words: **the advertised "50+ snippets" were an empty array upstream.**
Here it is 57 real snippets.

---

## Quick start

```bash
# 1) dependencies (PawUI is GPL-3.0, PySide6 is LGPL-3.0)
pip install PySide6 "pawui>=0.1.1"

# 2) run the IDE against the current directory
python main.py

# 3) or point it at a project
python main.py D:\my-project

# 4) headless interactive self-check (15 checks, no display needed)
python main.py --selftest

# 5) screenshot (layout verification in headless environments)
python main.py --shot shots/ide.png
```

Without an API key the AI panel runs in **offline drill mode**: it really executes
the Builder/Coder orchestration loop, but **never touches the network and never
modifies your files**, and says so in the transcript.

---

## Command line

The backend is a library that neither imports PawUI nor needs Qt, so it also
works as a standalone CLI:

```bash
python -m tcide.cli check               # static checks (11 rules); exit 1 on errors
python -m tcide.cli analyze             # cycles / layers / fan-in / smells
python -m tcide.cli index               # language mix, parse failures
python -m tcide.cli search "def main"   # project-wide search
python -m tcide.cli outline tcide/cli.py
python -m tcide.cli snippet pawwin --set title=Demo --set w=800 --set h=600
python -m tcide.cli hex assets/logo.png --info
python -m tcide.cli git suggest         # Conventional Commits suggestion
python -m tcide.cli heal err.log        # parse errors + explainable fix hints
```

Every analysis command supports `--json` for scripting.

---

## Feature parity

Legend: ✅ implemented and tested · 🚧 partial · 📋 planned

| Capability | Upstream | Here |
|---|---|---|
| Builder → Coder loop | ✅ | ✅ `agent.py` (visible fallback when planning fails) |
| Reviewer gate (`REVIEWER_GATE_ENABLED`) | ✅ | ✅ deterministic checks first, model review optional |
| Parallel coders + retries | ✅ | ✅ concurrency **proven with a `Barrier`** in tests |
| Multi-model (DeepSeek/Volcano/Ollama/Anthropic/custom) | ✅ | ✅ `models.py`, **zero third-party HTTP deps** |
| MCP tools | ✅ | ✅ 12 tools + dangerous-command denylist |
| 32K context trimming | ⚠️ never wired | ✅ `trimmer.py` (layered, with assertions) |
| Code entropy evaluation | ⚠️ never wired | ✅ `pressure.py` (**honestly renamed**: weighted heuristic) |
| Git intelligence | ⚠️ never wired | ✅ `gitintel.py` |
| Unattended runner (timeouts/checkpoints) | ⚠️ never wired | ✅ `runner.py` |
| Semantic completion | ⚠️ never wired | ✅ `completion.py` (**honestly**: prefix completion) |
| Auto-heal | ⚠️ never wired | ✅ `autoheal.py` (**explainable hints**, no "AI fixes it" claim) |
| Architecture analysis | ⚠️ never wired | ✅ `arch.py` (**real Tarjan SCC**) |
| Live diagnostics | ⚠️ never wired | ✅ `lint.py` (11 rules, stdlib only) |
| Performance monitor | ⚠️ never wired | ✅ `perf.py` (Python-side `tracemalloc`, labelled as such) |
| Debug manager | ⚠️ never wired | ✅ `debug.py` (configs/breakpoints/commands; **no DAP client**) |
| 50+ snippets | ❌ returned `[]` | ✅ `snippets.py` (57, incl. 17 PawUI-specific) |
| 9-state pixel pet | ✅ | ✅ `pet.py` (procedurally drawn, no assets; wired into the status bar) |
| Monaco / xterm / LSP | ✅ | 📋 see roadmap |
| Diff review modal (`acceptDiff`) | ❌ lost | 📋 planned |
| Tests / CI / LICENSE | ❌ none | ✅ 669 tests + CI; see license below |

Full plan and the complete bug list live in **[FEATURE_MATRIX.md](FEATURE_MATRIX.md)**.

---

## Architecture

```
app.paw          declarative UI (toolbar / file tree / Git / editor / dock / status bar + pet / palette)
main.py          entry point + wiring (AppState + 26 handlers + offline drill + --selftest/--shot)
tcide/           backend core (24 modules, imports NO PawUI, strictly testable offline)
tests/           669 tests across 19 files
tools/           screenshots, env check, encoding repair
```

**Layering rule**: `tcide/*` neither imports PawUI nor touches the UI. That is why
almost everything can be verified headlessly with pytest, and it is also what makes
the license split possible.

---

## Testing

```bash
# backend core (zero third-party deps, runs in seconds)
python -m pytest -q tests/ --ignore=tests/test_shell.py

# everything (UI parts need Qt and must run offscreen)
QT_QPA_PLATFORM=offscreen python -m pytest -q

# 15 interactive shell checks
python main.py --selftest
```

| Layer | Technique | Needs Qt |
|---|---|---|
| Pure logic (trimming/index/git/agents/pet/snippets/completion/autoheal/arch/CLI) | direct assertions | ❌ |
| Model protocols | **local fake HTTP server** asserting requests/responses | ❌ |
| Agent loop | **fake model** asserting parallelism/retries/gate | ❌ |
| Git | real `git` in a temp repository | ❌ |
| UI shell | offscreen tree build + widget/geometry assertions + screenshot | ✅ |

---

## Bugs found and fixed while building this

22 in total; the full list is in [FEATURE_MATRIX.md](FEATURE_MATRIX.md) §4. Highlights:

- **`<TextArea>` could not display text containing `{$...}`** (PawUI defect):
  any `{$x}` inside state was re-resolved as a template — measured **27,623 chars
  collapsing to 27,101**. For a framework whose own syntax is `{$x}` and which is
  being used to build a code editor, that means *it cannot display its own source*.
  Fixed narrowly (`resolve_raw(chain=False)` for widget bindings only, leaving
  `For`/`Select` chaining intact; PawUI's full 400-test suite stays green).
- **Theme tokens passed through `{$state}` were not resolved** (PawUI defect):
  `bg="{$x}"` with `x="border"` produced a **self-contradictory** warning (listing
  `border` as a valid theme color) and the color was silently dropped.
- **The reviewer gate treated "no problems found" as a problem**: the substring
  check `"问题" in output` matched the success message, so *every* change was
  rejected and retried. Replaced with a structured `ToolResult`.
- **The self-check overwrote `main.py`**: its "spy" forwarded writes to the real
  filesystem and the first `.py` it opened was `main.py` itself, which it replaced
  with a single line `value = 1`. Writes are now recorded but never persisted, plus
  a sha1 integrity check that the self-check changes nothing.

---

## License

Split licensing, because the PawUI dependency is GPL-3.0 (declared as
`GPL-3.0-or-later` in its `pyproject.toml`), and the GPL requires the **combined
work** to be GPL:

| Part | License | Notes |
|---|---|---|
| `tcide/` (28 files) | **LGPL-3.0-or-later** | reusable library that does not depend on PawUI; see [`tcide/LICENSE`](tcide/LICENSE) |
| `main.py` / `app_selftest.py` / `app.paw` / `tools/` / `tests/` | **GPL-3.0-or-later** | links GPL PawUI, so the combined work must be GPL; see [`LICENSE`](LICENSE) |

Every source file carries an **SPDX identifier** (written idempotently by
`tools/apply_licenses.py`, which also verifies the license texts are byte-identical
to authoritative local copies).

The `tcide/` boundary is enforced by tests — break it and CI fails:

- `test_package_does_not_import_pawui` — the library must not depend on GPL PawUI
- `test_package_does_not_import_qt_at_module_level` — the library must not pull the UI
  stack in at import time (the PySide6 use in `tcide/web/bridge.py` is a deliberate
  **function-local lazy import** so the library imports without Qt; PySide6 itself is LGPL)
- `test_every_library_file_is_lgpl` / `test_every_app_file_is_gpl` — SPDX header gates

> The shell self-check used to live inside the library (`tcide/shell_selftest.py`);
> to keep the LGPL boundary **without exceptions** it now lives on the app side as
> `app_selftest.py`.

---

## Roadmap

1. Embed **Monaco** and **xterm.js** via `<Web>` + QWebChannel (QtWebEngine verified available)
2. **Diff review modal** (the lost `acceptDiff`)
3. Surface already-tested backend features in the UI (outline / search / completion panels)
4. Packaging (PyInstaller) and installers
5. Execute the license split (LGPL library + GPL app + LICENSE files)
