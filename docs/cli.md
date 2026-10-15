# Command-line reference

```
joulecode [--version] <command> [options]
```

> **Tip:** `joulecode --help` lists all commands, `joulecode <command> --help` shows every
> option of a command with its default. This page collects the same information in one place.

**Defaults come from `.joulecode.env`.** Options marked *(env)* below take their default from an
environment variable in [`.joulecode.env`](configuration.md#1-machine-settings-joulecodeenv)
(back-end, model, server, project, token). Set them there once; pass the option only to deviate
for a single run.

`measure`, `analyze` and `optimize` take a lock: only one of them runs at a time on a machine
(two measurements would disturb each other). Results go to `joulecode-runs/` in the current
directory unless `--out` is given.

## `joulecode doctor`

Checks the machine: operating system, cgroup version, Docker, GPU, RAPL and every energy
back-end, and prints the most accurate one that is ready. Exit code 1 if a required check
fails.

| Option | |
|---|---|
| `--metrion-results GLOB` | where to look for `metrion-live` output |

## `joulecode measure REPO`

Builds the original and a candidate, runs the tests in both (both must pass) and measures the
benchmark in interleaved, randomised blocks. Writes `report.html`, `report.md` and
`result.json`.

| Option | |
|---|---|
| `--candidate-patch FILE` | candidate as a patch (`git diff`) against the original |
| `--candidate-dir DIR` | candidate as a directory |
| `--aa` | original against itself — measures the noise of the machine |
| `--baseline-ref REF` | take the original from this Git ref instead of the working tree |

Measurement options (also for `optimize`):

| Option | |
|---|---|
| `--backend NAME` | *(env)* `auto`, `metrion`, `metrion-legacy`, `metrion-live`, `rapl`, `cgroup`; default `$JOULECODE_BACKEND` or `auto` |
| `--repetitions N` | maximum blocks (adaptive) or fixed number (`--fixed`) |
| `--min-repetitions N` | earliest stop with adaptive measurement |
| `--fixed` | always measure all blocks |
| `--warmup N`, `--cooldown S`, `--seed N` | override `measurement.*` |
| `--env KEY=VALUE` | extra benchmark variable (repeatable), e.g. `--env BENCH_ROUNDS=80` |
| `--rapl-idle S` | seconds of idle measurement for `rapl` (default 10) |
| `--metrion-results GLOB` | output location for `metrion-live` |
| `--out DIR` | output directory |
| `--force` | start despite failed pre-checks of the back-end |

## `joulecode analyze REPO`

Static analysis of the whole repository and hotspots by profiling the benchmark in the
container. Needs no language model. Writes `analysis.md` and `analysis.json` to `joulecode-runs/analysis-<timestamp>/` and prints the hotspots.

| Option | |
|---|---|
| `--no-profile` | static analysis only, without Docker |
| `--hotspots N` | number of hotspots (overrides `optimize.hotspots`) |
| `--out DIR` | output directory |

## `joulecode optimize REPO`

The full pipeline: build and test the original → analysis and profiling → per hotspot, LLM
candidates → insert, test, repair → documentation → maintainability review (veto, revision) →
unload the LLM → measure all admitted candidates → Pareto front per hotspot → test and measure
the combination of the best → report. Nothing is applied to your repository.

| Option | |
|---|---|
| `--model NAME` | *(env)* Ollama model (default `$JOULECODE_OLLAMA_MODEL` or `qwen2.5-coder:14b`) |
| `--ollama-host URL` | *(env)* default `$JOULECODE_OLLAMA_HOST` or `http://localhost:11434` |
| `--hotspots N`, `--candidates N` | override `optimize.hotspots` / `candidates_per_hotspot` |
| `--no-measure` | only generate and check candidates (fast trial run) |
| measurement options | as for `measure` |

Output (`joulecode-runs/optimize-<timestamp>/`): `report.html`, `report.md`, `result.json`,
`candidates/<id>.md` and `.patch` per candidate.

## `joulecode report DIR`

Re-creates `report.md` and `report.html` from the `result.json` of a result folder, without
measuring — e.g. for folders created by an older version.

## `joulecode reanalyze DIR`

Repeats the METRION evaluation of a result folder without measuring again (after a failed
evaluation, or with a newer METRION).

| Option | |
|---|---|
| `--db PATH` | METRION result to use (live directory, run or `.db`); default: automatic |
| `--raw` | evaluate the raw data (`monitor_*`) instead of the live result — slower, but without gaps of the live attribution |

## `joulecode cleanup`

Removes leftovers of aborted runs: measurement containers, temporary folders, a running
on-demand METRION service. Only things joule:code created (label `joulecode=1`).

| Option | |
|---|---|
| `--dry-run` | only show what would be removed |
| `--images` | also delete the built variant images |
| `--runs-older-than DAYS` | also delete result folders older than DAYS |
| `--keep-metrion` | do not stop a running METRION service |
| `--force` | even if a run seems to be active |

```bash
joulecode cleanup --dry-run                 # show what would be removed
joulecode cleanup                           # measurement containers, temp folders, running METRION service
joulecode cleanup --images                  # also the built variant images
joulecode cleanup --runs-older-than 30      # also result folders older than 30 days
```

Only things joule:code created itself are removed: containers and images labelled
`joulecode=1` (the Ollama container stays), its own `/tmp/joulecode-*` folders and — only on
request — old folders under `joulecode-runs/`. While a measurement is running, `cleanup`
refuses. METRION results in `/var/lib/metrion` stay so that `reanalyze` can still use them.

## `joulecode serve`

Starts the platform: web UI, REST API (`/api/docs`) and a job queue with exactly one worker.
Requires `pip install -e '.[server]'`. See [platform.md](platform.md).

| Option | |
|---|---|
| `--host ADDR` | default `127.0.0.1` (local only; a warning is printed otherwise) |
| `--port N` | default `8765` |
| `--data-dir DIR` | *(env)* database and uploads (default `$JOULECODE_DATA` or `~/.joulecode/server`) |
| `--processor full\|review\|dummy` | `full`: all modes real (measuring host); `review`: review real, gate/optimize simulated; `dummy`: everything simulated |
| `--backend`, `--rapl-idle`, `--metrion-results` | energy back-end for gate/optimize (*(env)* `$JOULECODE_BACKEND`) |
| `--model`, `--ollama-host` | *(env)* LLM for optimize |
| `--lock-wait S` | how long a job waits while a manual `measure`/`optimize` is running (default 3600) |
| `--max-upload-mb N` | upload limit (default 200) |
| `--dummy-delay S` | seconds per phase in dummy mode |
| `--log-level LEVEL` | `critical` … `debug` |

## `joulecode submit [REPO]`

Packs the working tree (`git ls-files -co --exclude-standard`: tracked and untracked, not
ignored files), uploads it to a server, waits for the result and prints a summary (in GitHub
Actions also to the step summary).

| Option | |
|---|---|
| `--server URL` | *(env)* default `$JOULECODE_SERVER` or `http://127.0.0.1:8765` |
| `--project KEY`, `--token TOKEN` | *(env)* default `$JOULECODE_PROJECT`, `$JOULECODE_TOKEN` |
| `--mode review\|gate\|optimize` | default `review` |
| `--base REF` | target branch, e.g. `origin/main`; required for `gate`, limits `review` to changed functions; detected from GitHub Actions / GitLab CI variables |
| `--max-regression PCT` | gate: fail if the whole CI is more than PCT % worse (default 5) |
| `--fail-on none\|info\|minor\|major` | fail on findings of this severity or higher (default `none`) |
| `--all` | review with `--base`: also report findings outside the changed code |
| `--no-wait` | submit only, do not wait |
| `--timeout S` | how long to wait (default: review 600, gate 7200, optimize 21600); the job keeps running |
| `--output FILE` | write the job including the result as JSON |
| `--sarif FILE` | write findings as SARIF 2.1.0 |

Exit codes: `0` passed or no condition set, `1` gate failed, `2` error.

## Scripts

| Script | |
|---|---|
| `scripts/install.sh` | set up a measuring host, see [installation.md](installation.md) |
| `scripts/suite.py` | test suite: `check`, `run`, `evaluate`, see [examples/suite](../examples/suite/README.md) |
| `scripts/metrion_diagnose.py` | where did METRION attribute the energy of a run? Run with METRION's Python: `sudo /opt/metrion/.venv/bin/python scripts/metrion_diagnose.py joulecode-runs/…` |
