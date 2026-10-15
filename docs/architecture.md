# Architecture

## Background

An earlier prototype rated optimisations with its own anti-pattern score — computed by the
same hand-written rules the LLM was optimising towards. Whether energy was saved was never
measured. joule:code reverses this: **a measurement decides**; static analysis and the LLM only
provide hints and proposals.

## Principles

1. **Tools measure and decide; LLMs rewrite and review.**
2. **Tests are a necessary condition.** A candidate that does not pass the original's tests is
   not measured. Tests, benchmark and `joulecode.yml` are protected paths a candidate cannot
   change.
3. **Energy is measured, not estimated** — with the most accurate method available, and the
   report always names it.
4. **Existing tools instead of own rules** (Ruff, radon, vulture, PHPMD, ESLint, PMD …).
5. **Maintainability is judged for the whole repository**, not just one file.
6. **No automatic winner.** joule:code presents measured options; a human chooses.

## Input

A Git repository with a `Dockerfile` and a `joulecode.yml`
([configuration](configuration.md)). Dependencies are installed when the image is built; at
run time containers have no network. The benchmark is supplied by the user — an LLM may at
most suggest one, so that nothing is optimised towards a self-chosen benchmark.

## Components

| Component | Modules | Command |
|---|---|---|
| Measurement chain: variants → image build → test gate → interleaved measurement → statistics → report | `variants.py`, `container.py`, `protocol.py`, `energy/`, `stats.py`, `report.py`, `html_report.py` | `joulecode measure` |
| Hotspots and static analysis | `analysis.py`, `profiling.py`, `lang/` | `joulecode analyze` |
| Agents: optimiser per hotspot, test repair, documentation, maintainability reviewer, measurement, Pareto per hotspot, combination | `optimize.py`, `agents.py`, `llm.py`, `prompts.py`, `optimize_report.py` | `joulecode optimize` |
| Language back-ends: Python, PHP, JavaScript/TypeScript, Java | `lang/` | — |
| Platform and CI: server, queue, web UI, SARIF, client | `server/`, `submit.py` | `joulecode serve`, `joulecode submit` |
| Host checks, clean-up, lock | `hostcheck.py`, `cleanup.py` | `joulecode doctor`, `joulecode cleanup` |
| Test suite | `scripts/suite.py`, `examples/suite/` | `python scripts/suite.py` |

## Measurement chain

```
joulecode measure REPO --candidate-patch opt.patch
   │
   ├─ variants.py   working copies baseline/candidate, apply patch,
   │                reset protected paths from the original
   ├─ container.py  docker build per variant; every run in a fresh container
   │                (no network, CPU/RAM/PID limits, cap_drop ALL), command via exec
   ├─ protocol.py   test gate → warm-up → blocks in random order → cool-down
   ├─ energy/       interchangeable back-ends (see below)
   ├─ stats.py      medians, bootstrap 95 % CI of the relative change, Mann–Whitney U
   └─ report.py     result.json + report.md (+ report.html)
```

### Energy back-ends

| Back-end | Measures | Accuracy | Requirement |
|---|---|---|---|
| `metrion` | dynamic CPU + DRAM energy of the container cgroup, METRION ≥ 0.2 (`metrion analyze --cgroup`) | per process; accounts for SMT, frequency, NUMA | bare-metal Linux, Intel, BTF, on-demand service + sudoers rule + calibration |
| `metrion-legacy` | active CPU + DRAM (+ GPU) energy of the container cgroup, METRION < 0.2 from GitHub | per process | bare-metal Linux, Intel, on-demand service + sudoers rule |
| `metrion-live` | dynamic energy via live attribution (Parquet) | as `metrion` | METRION with `live_attribution` |
| `rapl` | whole-socket energy minus idle | everything on the machine counts | Linux, readable RAPL |
| `cgroup` | CPU time of the container cgroup | proxy, not energy | anywhere |

`auto` picks the most accurate available back-end. The main quantity is **dynamic** energy:
the idle share depends on whatever else runs and is useless for A/B comparisons (it is still
recorded in the JSON).

**METRION ≥ 0.2 (`energy/metrion_cli.py`):** the on-demand service writes a small Parquet live
result `energy_<UTC time>` per session. After stopping, joule:code archives it and evaluates it
exclusively through METRION's official command line (`metrion analyze … --cgroup docker-
--csv`), summing the dynamic energy per container ID. No internal METRION functions are used,
which keeps the integration stable across versions. The run belonging to a measurement is found
by the timestamp in its name.

**METRION < 0.2 (`energy/metrion_db.py`):** the public version only writes to a DuckDB file,
which is locked while it writes. joule:code starts the service per campaign (fresh database),
runs everything, keeps the containers until the end (METRION resolves cgroup IDs to paths with
a delay), stops the service, copies the database and evaluates it with METRION's
`execute_queries` in METRION's own venv (`energy/metrion_db_query.py`). A narrow sudoers rule
allows start/stop; joule:code itself runs without root.

**Live attribution (`energy/metrion_live.py`):** METRION runs permanently as a systemd service
with `live_attribution.enabled: true` and writes one row per 10 s window and thread with
`cgroup_name` and energy. After the last run joule:code waits until METRION has written windows
beyond the end of the run, then sums per run all rows whose `cgroup_name` contains the container
ID. Because every run has its own container, the window boundaries do not matter.

Operational details (service, archive, troubleshooting): [installation.md](installation.md#how-joulecode-uses-metrion).

### Measurement protocol

- **Test gate first;** if a variant fails, nothing is measured.
- **Warm-up blocks** are measured but not evaluated.
- **Blocks:** each repetition is a block in which every variant runs once, in random order —
  drift (heat, background jobs) hits all variants equally.
- **Cool-down** between runs.
- **Verdict “better”** only if p < α **and** the whole 95 % CI lies below 0.
- **Warnings:** benchmark shorter than `min_duration_s`, coefficient of variation > 10 %, GPU
  busy before the measurement (LLM still running?).
- **Plausibility:** energy per CPU second hardly varies for the same code. A run with less than
  half (or more than double) the median of its variant counts as an attribution failure of the
  back-end and is discarded; the raw value stays in `value_raw`, and report and warnings show it.
  Observed with METRION's live attribution: single runs with ~1 J instead of hundreds of J at full
  CPU time. Diagnosis: `scripts/metrion_diagnose.py`; re-evaluation from raw data:
  `joulecode reanalyze DIR --raw`.
- **Adaptive repetitions** (`measurement.adaptive`, default on): at least `min_repetitions` (5),
  at most `repetitions` (10) blocks. After each block from the fifth on, a candidate whose
  comparison is settled leaves — significantly better/worse, or “equal” (whole CI within
  ±`precision`, 2 %). The original runs in every block. The early decision uses CPU time,
  because METRION's energy is only available after the session; for compute-bound benchmarks
  energy tracks CPU time closely (measured: −84.6 % energy at −84.2 % CPU time). Against
  “peeking until it fits”, early stopping uses a Bonferroni-corrected α (α / number of interim
  looks; 0.0083 for 5–10 blocks — just reachable with 5 vs. 5 runs at complete separation). The
  final evaluation uses the full α on the energy values. Undecided candidates run to the
  maximum. `--fixed` always measures all blocks.
- **Top-up** (`measurement.topup`): if the plausibility check discards so many runs that a
  variant has fewer than `min_repetitions` valid values, a second short back-end session with
  its own warm-up measures the missing blocks (original + affected variants).
- **A/A test:** `--aa` measures the original against itself to quantify the machine's noise
  before judging candidates.

## Hotspots and static analysis

`joulecode analyze` — `analysis.py`, `profiling.py`, language back-ends.

- **Profiling:** the benchmark runs once under the language's profiler in the same container
  set-up as the measurement (Python: `python script.py` / `python -m module` are rewritten to
  cProfile automatically, otherwise `profile.command`). The profile is evaluated inside the
  container; out comes JSON. Hotspots are the functions with the largest *own* time share
  (`optimize.min_share`, default 5 %), excluding tests and benchmark, mapped to qualified names
  (`Class.method`).
- **Tools:** per language (Python: Ruff `--isolated` with PERF/C4/SIM/FURB/UP/B, radon, vulture;
  others see [languages.md](languages.md)). Findings go into the prompt, not into the verdict.
- **Repository metrics:** mean/max complexity, functions with CC > 10, maintainability index
  (SLOC-weighted mean/min), linter findings, dead code, import coupling (fan-in/fan-out per
  module).

## Agents

`joulecode optimize` — `optimize.py`, `agents.py`, `llm.py`, `prompts.py`

```
build original → tests (required) → analysis + profiling → hotspots
  per hotspot:
    optimiser → n candidates (whole functions + imports, JSON schema)
      → insert via the language back-end (indentation, decorators, imports) → syntax check
      → tests in the container; failing → repair with the test output (repair_attempts)
      → documenter: docstring required
      → reviewer: rule vetoes + LLM score 1–5 (veto only with score ≤ 2)
    all vetoed → revision round with the reviewer's feedback
unload LLM (keep_alive=0), wait for an idle GPU
measure: original + all admitted candidates in one campaign
Pareto per hotspot → combination of the best across hotspots
  → tests → second measurement (original, best single candidate, combination)
```

- **Whole functions instead of diffs:** a 7B model produces complete functions far more
  reliably than correct unified diffs. The language back-end inserts them deterministically; the
  patch is derived from the result.
- **Context:** for files up to 400 lines (`MAX_CONTEXT_LINES` in `agents.py`) the optimiser sees
  the whole file; for longer files only the hotspot function itself.
- **Rule vetoes (deterministic):** changed signature (API break), new dependency outside the
  standard library and the project, complexity increase > `max_complexity_increase`.
- **LLM reviewer:** rates maintainability only (energy is measured elsewhere), score 1–5 relative
  to the original, concerns, suggestion. Without an LLM verdict the score is derived from
  Δcomplexity and Δmaintainability index.
- **Duplicates:** a candidate whose code equals an already admitted one is not reviewed or
  measured again. Comparison ignores formatting and comments (Python: syntax tree; others: token
  sequence).
- **Candidates touching several hotspots:** if the LLM rewrites hotspot B while working on A,
  the candidate belongs to the group “A + B” and is compared only with candidates changing the
  same functions.
- **Pareto per hotspot:** candidates for different hotspots are not alternatives. Within a
  hotspot, a candidate dominates only if it is clearly more efficient (confidence intervals do
  not overlap) with at least equal maintainability — or equally efficient with better
  maintainability. Noise never decides.
- **Combination:** the best candidates of different hotspot groups — greedily by saving, without
  shared functions — are merged, tested again (interactions) and measured against the original
  and the best single candidate.
- **Documentation:** every changed function needs a docstring (or PHPDoc/JSDoc/Javadoc). If the
  original had one and the candidate dropped it, it is restored; otherwise the documenter writes
  one. Each candidate gets a report `candidates/<id>.md`: rationale, measured effect with CI and
  method, maintainability metrics, reviewer concerns, history (repair/veto), patch.
- **Result:** no automatic winner — a table of energy/CI/verdict/maintainability; the human
  chooses.

### LLM and measurement never at the same time

Ollama runs on the same machine. The pipeline therefore runs strictly in phases: generate all
candidates first, unload the model, wait for an idle GPU, then measure. The measurement warns if
the GPU is busy beforehand.

## Language back-ends

Measurement, test gate, containers and statistics are language-neutral — they only run the
commands from `joulecode.yml`. Each back-end supplies analysis, profiling, finding/replacing
functions and prompts. Details: [languages.md](languages.md).

## Platform

`joulecode serve` / `joulecode submit` with the modes review, gate and optimize, a single-worker
queue and SARIF output. Details: [platform.md](platform.md).

## Test suite

`scripts/suite.py` with `examples/suite/suite.yml` checks joule:code's *verdict* on examples with
known expectations (details in [examples/suite/README.md](../examples/suite/README.md)):

- **Expectations:** `improvement` (significantly better and ≥ `min_gain`), `no_improvement`
  (negative control: no “better” beyond `tolerance`), `no_hotspots` (pipeline stops after
  profiling).
- **Holdout tests** live outside the example repositories; joule:code never sees them. After each
  run every candidate that passed the test gate is checked against them in the same container
  set-up. A gate gap in a significantly better candidate fails the example.
- **Reference patches:** hand optimisations; `check` validates them against all tests,
  `run --reference` measures them to put the achieved saving into perspective.
- **Long examples:** one per language with 1,150–1,450 lines of source code, hotspots among a lot
  of unremarkable code, one in a file with > 400 lines (so the optimiser sees only the function).
- **Repetition:** `--repeat N`, because the LLM is not deterministic. Each run is its own
  `joulecode optimize` process; the summary is rewritten after each run, and an interrupted suite
  can be resumed.

## Security

joule:code deliberately executes the code it measures — that is the core of the approach. The
sandbox: a fresh container per run, no network, resource limits, no capabilities,
`no-new-privileges`. joule:code itself needs access to the Docker daemon, which is equivalent to
root on the measuring host — hence a dedicated machine. See [SECURITY.md](../SECURITY.md) and,
for the platform, [platform.md](platform.md#security).
