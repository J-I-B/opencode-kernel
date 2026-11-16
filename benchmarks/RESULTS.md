# Benchmark results — cheap suite

- **Date:** 2026-11-16 · **OpenCode:** v2.3.0 · **Model:** `deepseek/deepseek-flash`
  (same model both arms; some reasoning tokens observed) · **n = 1 per arm**
- **Fixtures:** 60-file Python repo (24 files / 120 occurrences of `calc_total`,
  7 functions >30 lines), 20,000-line synthetic log. Deterministic, hermetic.
- **Arms:** `kernel` (opencode-kernel plugin enabled) vs `baseline` (plugin absent).
  Identical bench agent, identical prompts per round, `opencode run --standalone`.
- **Metrics source:** per-step `cost`/`tokens` events from `--format json`
  (OpenCode's models.dev-based estimate; DeepSeek peak/off-peak billing may differ).
  `input (miss)` = uncached input tokens; `cache read` billed separately at the hit rate.
- **Total live spend (all rounds + calibration probes): < $0.07 (≈ €0.065).**
  Cost guard: $3 cumulative, never approached. Failed boot attempts cost $0.

## Round 1 — unsteered (adoption probe)

Task prompts as originally written, no tool guidance. **The kernel arm never
called the `python` tool**: both arms solved everything with shell one-liners
(grep/sed/awk). All 6 runs PASS.

| Task | Arm | Steps | Wall s | Cost $ | Input (miss) | Cache read | Output | Verify |
|---|---|---|---|---|---|---|---|---|
| bulk-rename | kernel | 7 | 17.4 | 0.0031 | 12,904 | 74,496 | 1,018 | PASS |
| bulk-rename | baseline | 6 | 14.9 | 0.0012 | 2,911 | 64,768 | 745 | PASS |
| log-analysis | kernel | 6 | 15.8 | 0.0023 | 7,175 | 56,576 | 1,352 | PASS |
| log-analysis | baseline | 7 | 16.6 | 0.0024 | 7,692 | 69,632 | 1,465 | PASS |
| repo-audit | kernel | 7 | 15.5 | 0.0022 | 8,367 | 68,736 | 881 | PASS |
| repo-audit | baseline | 8 | 17.0 | 0.0022 | 8,710 | 81,024 | 774 | PASS |

**Finding:** with a capable model and small fixtures, shell one-liners are the
path of least resistance and avoid context pollution on their own. Kernel
adoption requires guidance (tool description, prompt rules, or explicit task
steering). This validates the "no gains without adoption" caveat in the README.
Evidence: `runs-unsteered/`.

## Round 2 — steered (mechanism A/B)

One arm-neutral sentence appended to every task prompt (identical in both
arms): *"If a python tool backed by a persistent kernel is available, use it
for this task; otherwise use the tools you have."* The kernel arm then used
`python` via Code Mode (`execute` → `tools.python`): 7 / 2 / 4 calls per task.

| Task | Arm | Steps | Wall s | Cost $ | Input (miss) | Cache read | Output | Verify |
|---|---|---|---|---|---|---|---|---|
| bulk-rename | kernel | 11 | 30.7 | 0.0035 | 6,469 | 138,496 | 2,504 | FAIL* |
| bulk-rename | baseline | 8 | 20.5 | 0.0024 | 6,857 | 112,384 | 1,276 | PASS |
| log-analysis | kernel | 5 | 13.2 | 0.0010 | 1,351 | 50,944 | 909 | PASS |
| log-analysis | baseline | 6 | 14.1 | 0.0016 | 4,659 | 71,040 | 925 | PASS |
| repo-audit | kernel | 7 | 18.4 | 0.0024 | 6,529 | 95,232 | 1,523 | PASS |
| repo-audit | baseline | 8 | 21.1 | 0.0022 | 4,535 | 98,560 | 1,468 | PASS |

- **log-analysis — the mechanism shows:** kernel arm **39% cheaper, 71% fewer
  uncached input tokens** (1,351 vs 4,659), one fewer step, ~1s faster. The log
  was loaded once into a kernel variable; queries returned only small slices.
- **repo-audit — near-parity:** kernel one fewer step and ~3s faster, slightly
  more output tokens; cost difference is noise at n=1.
- **bulk-rename — \*FAIL, disclosed verifier incident:** the kernel arm's
  rename was exact (120/120 occurrences, 24 files; the model additionally ran
  `py_compile`, an import check, and a full diff audit: 120 removed / 120 added
  lines, all clean). Those verification steps created
  `repo/pkg_alpha/__pycache__/mod_00.cpython-314.pyc`, and the verifier —
  scanning *all* files under `repo/` — counted the bytecode as a source file
  (61 files / 121 occurrences vs expected 60 / 120). **Verifier amended** to
  scan `*.py` source only (generated artifacts are not source occurrences);
  selftest 8/8 after the change, and the original workspace PASSes under the
  amended verifier. Original evidence preserved in `runs-round2-bulk-rename/`.
  The pair was then re-run under identical conditions with the amended verifier:

| Task | Arm | Steps | Wall s | Cost $ | Input (miss) | Cache read | Output | Verify |
|---|---|---|---|---|---|---|---|---|
| bulk-rename (rerun) | kernel | 7 | 19.1 | 0.0025 | 7,246 | 97,792 | 1,328 | PASS |
| bulk-rename (rerun) | baseline | 7 | 20.2 | 0.0023 | 4,938 | 89,984 | 1,246 | PASS |

Near-parity: for a mechanical rename, `sed`-class shell work is hard to beat;
the kernel adds no advantage here. Evidence: `runs-steered/`, logs in `logs/`.

## Round 3 — plugin guidance v1 (unsteered tasks)

The plugin now injects a compact guidance block into model context via the
`context` hook (ships enabled; `guidance: false` opt-out). Task prompts restored
to the original unsteered versions; the guidance is the only treatment.

Adoption: **1/3 tasks** — bulk-rename used the kernel (4 python calls), while
log-analysis and repo-audit stayed on shell one-liners. All 6 PASS. But the
adopted task was the wrong one: the kernel rename cost ~3× the baseline's sed
($0.0034 vs $0.0011). Guidance v1 said "prefer for multi-step/bulk work" —
too broad; it pulled in sed-optimal work and missed the data task.
Evidence: `runs-guidance-v1/`.

## Round 4 — plugin guidance v2, final (unsteered tasks)

Guidance sharpened: data/log analysis named explicitly as kernel territory
("load once into a variable, then query across calls"); single mechanical
transforms ("a rename sed can do") explicitly left to shell.

Adoption: **selective, as designed** —
- log-analysis → **kernel** (2 python calls via Code Mode) ✓
- bulk-rename → **shell/sed** ✓ (cheaper than round 3's kernel rename)
- repo-audit → shell (defensible at this repo size)

All 6 PASS. Costs: bulk-rename $0.0019 vs $0.0014; log-analysis $0.0030 vs
$0.0017; repo-audit $0.0032 vs $0.0009 (kernel vs baseline).

**Variance disclosure:** log-analysis was 39% *cheaper* with the kernel in
round 2 (71% fewer uncached input tokens) but ~79% *pricier* in round 4. At
n=1, small fixtures, and a cheap cached model, run-to-run model behavior
dominates; no stable cost claim is possible at this scale. The structural
mechanism (data lives in the kernel, only slices enter context) is unchanged —
whether a given run exploits it well varies. Evidence: `runs/`.

## Conclusions (what we claim, and what we don't)

1. **Works end-to-end:** plugin loads, `python` tool usable directly and via
   Code Mode, all tasks completable, no regressions vs baseline.
2. **Context-hygiene mechanism is real when used:** 71% fewer uncached input
   tokens on the data task. At this fixture scale with a cheap cached model,
   the dollar delta is fractions of a cent — the mechanism matters more as
   data size, turn count, and model price grow.
3. **No universal win:** mechanical transforms favor shell; small data favor
   one-liners. The kernel's structural advantages (state across calls and
   compaction, multi-stage stateful analysis, large data) are **not exercised**
   by this suite — the compaction-survival task remains deferred.
4. **Adoption is steerable, and now ships in the plugin:** unsteered with no
   guidance, the model ignored the kernel entirely (round 1). Broad guidance
   pulled in sed-optimal work at 3× cost (round 3). Targeted guidance (v2,
   shipped default) achieves selective adoption: kernel for data analysis,
   shell for mechanical transforms (round 4).
5. **No statistical or leaderboard claims:** n=1, one cheap model, small
   fixtures, and disclosed run-to-run variance (log-analysis cost flipped
   between rounds). Everything above is directional, with full transcripts
   published.

## Infrastructure note (OpenCode quirk found while building the runner)

`opencode run` resolves its location from the **`PWD` environment variable**,
not `getcwd()`. Scripting it via Python `subprocess(cwd=...)` without updating
`PWD` boots the wrong location ("Agent not found" for project-defined agents).
The runner sets `PWD` explicitly; see `run_bench.py`.

## Reproduce

```sh
cd benchmarks
python3 fixtures_gen.py /tmp/fixcheck && python3 verify.py --selftest /tmp/fixcheck
python3 run_bench.py --max-cost 3.0        # requires DEEPSEEK_API_KEY; ~$0.02
```

## Evidence map

- `runs-unsteered/` — round 1 transcripts + metrics + summary (no guidance)
- `runs-round2-bulk-rename/` — original round-2 bulk-rename pair (FAIL evidence)
- `runs-steered/` — round 2 steered results (bulk-rename pair re-run, amended verifier)
- `runs-guidance-v1/` — round 3 (plugin guidance v1, unsteered tasks)
- `runs/` — round 4 final (plugin guidance v2, unsteered tasks)
- `logs/` — full runner stdout per round (incl. complete summaries)
