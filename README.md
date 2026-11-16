# opencode-kernel

**Persistent Python for OpenCode — state that survives compaction.**

One new tool, `python`: a long-lived Python session per OpenCode session. Variables,
imports, and functions persist across calls — and across context compaction. Bulk data
stays in the kernel, not in your context window. Zero dependencies: a pure-stdlib
driver, no pip install, no Jupyter server.

Inspired by [Unharnessed](https://unharnessed.dev)'s one-tool thesis, built for the
harness you already use: permission gates, sub-agents, TUI/desktop/web, any provider.

## Why

- **Context is scarce.** Every tool result poured into context stays there and is
  re-sent every turn. A 5,000-line log (~50k tokens) carried across 30 turns is
  ~1.5M cumulative input tokens. In the kernel, the log is a variable; each query
  returns only the slice you asked for.
- **Round trips are latency.** A 200-file sweep with a tool list is 200 model
  inferences. In the kernel it's one loop in one call.
- **Compaction erases working state.** After a long task compacts, the agent
  re-reads and re-derives everything it had — pure waste. Kernel state survives
  summarization untouched. No other option in this space addresses that.

## Install

Requires Python 3.8+ on PATH (or point the `python` option at any interpreter/venv).

```jsonc
// opencode.json
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["opencode-kernel"]
}
```

Local checkout: `"plugins": ["/path/to/opencode-kernel"]`.

## Usage

The model writes plain Python — no JSON wrapper needed (freeform input). State
persists between calls:

```python
# call 1
from pathlib import Path
files = list(Path("src").rglob("*.ts"))
len(files)
```

```python
# call 2 — `files` is still there
big = [f for f in files if f.stat().st_size > 50_000]
[f.name for f in big]
```

Bulk refactor in one call:

```python
import re
changed = 0
for f in Path("src").rglob("*.py"):
    t = f.read_text()
    n = re.sub(r"\bold_name\b", "new_name", t)
    if n != t:
        f.write_text(n)
        changed += 1
changed
```

Parse once, query many times (with packages installed in your interpreter):

```python
import pandas as pd
df = pd.read_csv("huge.csv")
df.shape
```

```python
df[df.status == "error"].head(10).to_string()
```

Also available inside Code Mode: `await tools.python("...")`.

**Guidance baked into the tool:** use the kernel for multi-step, bulk, or
programmatic work of any kind — file finding, parsing, refactors, checks, data.
Keep native `read`/`grep`/`edit` for quick one-off lookups and surgical single-file
edits: they preserve OpenCode's diff display and per-path edit permissions, which
kernel-side writes bypass.

## What it improves — and what it doesn't

**Structural guarantees (arithmetic, not hope):**
- Data tokens stop accumulating in context; only filtered output enters the window.
- State survives compaction by construction — the kernel is a server-side process.
- Bulk work is one call instead of N inferences.

**Likely, task-dependent (measured before we quote numbers — see `benchmarks/`):**
- Fewer compactions on data-heavy tasks, hence better long-task coherence.
- Lower cost on long tasks: fewer turns, less carried context.
- The full Python ecosystem (pandas, polars, duckdb, sqlite, matplotlib) in the loop.

**Not promised:**
- Benchmark score gains. Harness changes move scores a few points at best and can
  hurt; no claims until measured.
- Universal speedup. Small edits, UI work, docs: ~zero gain, zero harm. The kernel
  is additive — existing tools stay.

## How it compares

| Alternative | Gap | opencode-kernel |
|---|---|---|
| mittalsuraj18/opencode-ipython-plugin (V1 prior art) | V1 plugin API — incompatible with current OpenCode; Jupyter gateway + auto-venv; 850-line prelude | V2-native, zero-dependency, empty namespace |
| MCP kernels (ipython-mcp, jupyter-mcp-server) | Extra server/config; outside OpenCode's permission and agent model | Native tool: permission-gated, per-session kernels, Code Mode integration |
| Unharnessed | No permission gates, VS Code-only, no sub-agents | Keeps your harness: gates, sub-agents, TUI/desktop/web, any provider |
| Code Mode alone | Ephemeral runtime, fixed tool list, no data state | Persistent kernel complements it: execute orchestrates tools, kernel holds state |

## Design

- **Zero-dependency driver** — pure stdlib, ~10ms startup (measured on the reference
  machine), one long-lived process per session, lazily spawned.
- **OS-process isolation**, not WASM (lesson from the retired pydantic/mcp-run-python).
- **Permission-gated, ask by default** — OpenCode's base policy would let custom
  actions run unprompted, so the plugin enforces `ask` for the `python` action
  itself. Explicit allow rules (agent config or saved "always allow" approvals)
  are respected; explicit deny is final. Opt out with `"permission": "allow"`.
- **SIGINT interrupts** — timed-out cells are interrupted with state preserved;
  unresponsive kernels restart with an explicit notice.
- **Idle eviction + LRU cap** — kernels don't pile up.
- **Output truncation** with guidance to keep data in variables and print slices.
- **Empty namespace** — no prelude. The model writes and reuses its own helper
  functions, like any other variable.
- **Language-agnostic JSON-lines protocol** — alternative backends (IPython with
  rich output, others) can be added without redesign.

## Configuration

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": [
    {
      "package": "opencode-kernel",
      "options": {
        "python": "python3",      // interpreter path — point at a venv for pandas/polars
        "timeoutSeconds": 120,    // per-call default (max 600)
        "idleTTLSeconds": 1800,   // idle kernel eviction
        "maxKernels": 8,          // LRU cap across sessions
        "maxOutputChars": 20000,  // per-stream output cap
        "permission": "ask"       // "ask" (default, plugin-enforced) or "allow" (no prompts)
      }
    }
  ]
}
```

## Performance path

Rust/C++ speed where it matters: `pip install polars duckdb pyarrow orjson` into
your interpreter of choice and point `python` at it — Rust/C++ cores, Python surface.
The kernel's real speedup is persistence: parse a 100MB file once, query it twenty
times, instead of re-parsing per call.

## Safety & tradeoffs

- Arbitrary Python carries bash-level trust. Executions prompt by default
  (plugin-enforced); run in a container for untrusted repositories, or set
  `"permission": "allow"` deliberately for autonomous runs.
- Kernel-side file writes bypass OpenCode's edit diff display and per-path edit
  permissions — use native `edit` for surgical changes.
- A kernel crash loses in-memory state (automatic restart; state journal on the roadmap).
- Long cells block the kernel until interrupted (SIGINT on timeout preserves state).

## Roadmap

IPython backend (rich output, inline plots) · state journal + reconnect across
restarts · pre-warmed kernel pool · output spill-to-file.

## Benchmarks

Evidence suite and protocol: [`benchmarks/PROTOCOL.md`](benchmarks/PROTOCOL.md).
Numbers are published with full transcripts; nothing is claimed until measured.

## Prior art & credits

- [Unharnessed](https://unharnessed.dev) — the one-tool / persistent-session thesis
  that inspired this plugin.
- [mittalsuraj18/opencode-ipython-plugin](https://github.com/mittalsuraj18/opencode-ipython-plugin)
  — the prior OpenCode-native attempt (V1 API). This project is a V2-native,
  zero-dependency reimagining, not a continuation.
- [ConorForgie/ipython-mcp](https://github.com/ConorForgie/ipython-mcp) — the
  timeout-interrupt-with-state-preservation pattern.
- open-primeagent — truncate-and-spill output discipline.
- [pydantic/mcp-run-python](https://github.com/pydantic/mcp-run-python) (retired) —
  the lesson that OS-process isolation beats WASM sandboxing for this use.

## License

MIT
