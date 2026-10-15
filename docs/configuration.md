# Setup and configuration

joule:code is configured in two places:

| Where | What | Set up |
|---|---|---|
| **`.joulecode.env`** in the joule:code folder | settings of *this machine*: energy back-end, language model, METRION paths, server and client | once, after the [installation](installation.md) |
| **`Dockerfile` + `joulecode.yml`** in *your* repository | how to build, test and benchmark your code | once per repository |

Because everything machine-specific lives in `.joulecode.env`, the commands themselves stay short:
`joulecode measure REPO`, `joulecode optimize REPO`, `joulecode submit`. All command-line options
are optional and only needed to deviate from these settings for a single run — see
`joulecode <command> --help` or the [command reference](cli.md).

**Contents:** [1. Machine settings: `.joulecode.env`](#1-machine-settings-joulecodeenv) ·
[2. Prepare your repository](#2-prepare-your-repository) ·
[3. Check the set-up](#3-check-the-set-up) ·
[`joulecode.yml` reference](#joulecodeyml-reference) ·
[Environment variables](#environment-variables-joulecodeenv)

## 1. Machine settings: `.joulecode.env`

On a measuring host, `scripts/install.sh` has already created `.joulecode.env` from
[`.joulecode.env.example`](../.joulecode.env.example) and filled in the METRION paths and the
model. `source .venv/bin/activate` loads it, so every `joulecode` command sees these values.
Open it in an editor and check or add the following:

```bash
cd ~/joulecode
nano .joulecode.env                 # or any editor
source .venv/bin/activate           # re-load after changes (or open a new terminal)
joulecode doctor                    # shows which back-end and model are in use
```

### Energy back-end

```bash
# export JOULECODE_BACKEND="auto"
```

Leave it at `auto` (the default): joule:code uses the most accurate back-end that is ready —
`metrion` on a measuring host. Set it explicitly only to force a back-end, e.g.
`export JOULECODE_BACKEND="cgroup"` on a machine without energy counters (then CPU time is
compared instead of energy) or `"rapl"` without METRION. The
[installation guide](installation.md#alternatives) explains the alternatives.

### Change the language model

```bash
export JOULECODE_OLLAMA_HOST="http://localhost:11434"
export JOULECODE_OLLAMA_MODEL="qwen2.5-coder:14b"
```

A different model has to be pulled once before you switch to it:

```bash
docker exec joulecode-ollama ollama pull qwen2.5-coder:32b     # then set it in .joulecode.env
```

Which model fits your GPU: [installation guide, step 2](installation.md#2-choose-a-language-model).
`JOULECODE_OLLAMA_HOST` only needs to change if Ollama runs on another machine.

### Server and client (only for the platform)

If you use [`joulecode serve` / `joulecode submit`](platform.md), put the connection here, so
`joulecode submit` needs no further options on this machine:

```bash
export JOULECODE_SERVER="http://measuring-host:8765"
export JOULECODE_PROJECT="myapp"
export JOULECODE_TOKEN="gcp_…"          # project token – keep the file private (chmod 600)
```

In CI the same variables come from the CI settings (secrets) instead.

### METRION paths

`JOULECODE_METRION_*` are written by the install script and normally need no change. All
variables are listed under [environment variables](#environment-variables-joulecodeenv).

## 2. Prepare your repository

joule:code needs two files in the **root of your repository**: a `Dockerfile` and a
`joulecode.yml`. The fastest start is to copy them from the example for your language and adapt
them:

| Language | Copy from |
|---|---|
| Python | [examples/python-wordstats](../examples/python-wordstats) |
| PHP | [examples/php-wordstats](../examples/php-wordstats) |
| JavaScript / TypeScript | [examples/js-wordstats](../examples/js-wordstats) |
| Java | [examples/java-wordstats](../examples/java-wordstats) |

Language-specific details (Composer, npm, Maven/Gradle, profilers) are in
[languages.md](languages.md). Step by step:

1. **Dockerfile** — installs all dependencies at build time ([details](#the-dockerfile)).
2. **Benchmark** — a deterministic workload of at least ~20 s ([details](#the-benchmark)).
3. **`joulecode.yml`** — at minimum the test and the benchmark command:

   ```yaml
   version: 1
   language: python                     # python | php | javascript | typescript | java
   test:
     command: pytest -q
   benchmark:
     command: python bench/run.py
   protected_paths: [tests, bench, joulecode.yml]   # candidates cannot change these
   ```

   Everything else has sensible defaults ([reference](#joulecodeyml-reference)).

## 3. Check the set-up

```bash
joulecode analyze path/to/repo        # builds the image, runs static analysis, profiles the benchmark
joulecode measure path/to/repo --aa   # original against itself: should show no significant difference
```

If `analyze` lists hotspots and the A/A measurement shows no significant difference with a narrow
confidence interval, the repository is ready for `joulecode measure --candidate-patch …` and
`joulecode optimize` — see [usage](../README.md#usage).

Unknown keys in `joulecode.yml` are rejected, so a typo never silently leaves a default in place.

## The Dockerfile

joule:code builds one image per variant (original, each candidate) and runs every test and
benchmark run in a **fresh container without network**, with CPU/memory/PID limits, all
capabilities dropped and `no-new-privileges`. Hence:

- install **all** dependencies at build time (`pip install`, `composer install`, `npm ci`,
  `mvn dependency:go-offline` …);
- copy the source into the image (`COPY . /app`) — the commands from `joulecode.yml` run in
  the image's working directory;
- for compiled languages, compile at build time: a compiler error in a candidate then shows up
  when its image is built and counts like a failing test.

Minimal Python example:

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest==8.3.3
WORKDIR /app
COPY . /app
ENV PYTHONPATH=/app/src PYTHONDONTWRITEBYTECODE=1
```

Templates for other languages: [languages.md](languages.md) and [examples/](../examples).

## The benchmark

The benchmark is the workload joule:code optimises for and measures, so it decides what
“better” means. Write it yourself (do not let an LLM choose it):

- **representative** of real use, **deterministic** (fixed input, fixed random seed);
- **long enough**: at least `min_duration_s` (default 20 s) per run — energy counters sample
  at ~1 s, METRION attributes in 10 s windows; shorter runs produce a warning;
- **scalable** via an environment variable (convention: `BENCH_ROUNDS`), so you can calibrate
  it on the measuring host and use a smaller load for profiling (`profile.env`);
- it should print or check a result, so that a candidate cannot skip work unnoticed.

## `joulecode.yml` reference

```yaml
version: 1                       # required, currently always 1
language: python                 # python | php | javascript | typescript | java

build:
  dockerfile: Dockerfile         # relative to context
  context: .

test:                            # required – the test gate
  command: pytest -q
  timeout_s: 300
  env: {}

benchmark:                       # required – the measured workload
  command: python bench/run.py
  timeout_s: 300
  env: {BENCH_ROUNDS: "40"}
  min_duration_s: 20             # warn if a run is shorter

limits:                          # applied to every container
  cpus: 1.0
  memory: 1g
  cpuset: null                   # e.g. "2" or "2-3": pin to fixed cores (recommended, not core 0)
  pids: 512

measurement:
  repetitions: 10                # maximum number of blocks
  min_repetitions: 5             # earliest stop with adaptive measurement
  adaptive: true                 # stop a candidate early once its verdict is settled
  precision: 0.02                # CI fully inside ±2 % counts as "equal" for early stopping
  topup: true                    # re-measure if implausible runs had to be discarded
  warmup: 1                      # warm-up blocks (measured, not evaluated)
  cooldown_s: 5                  # pause between runs
  alpha: 0.05                    # significance level
  seed: null                     # seed for the random order within blocks

profile:                         # profiling run for hotspot detection
  command: null                  # own profiling command (rarely needed, see below)
  env: {BENCH_ROUNDS: "5"}       # extra variables only for profiling (e.g. smaller load)
  timeout_s: 900
  setup: null                    # PHP only: shell command that installs Xdebug

optimize:
  hotspots: 2                    # how many hotspot functions to work on
  min_share: 0.05                # ignore functions with less than 5 % of the run time
  candidates_per_hotspot: 2      # 1–5
  repair_attempts: 1             # 0–3: retries when a candidate fails the tests
  review_rounds: 1               # 0–2: revisions after a maintainability veto
  max_complexity_increase: 3     # veto if cyclomatic complexity rises by more
  combine: true                  # build and measure the combination of the best candidates

protected_paths: [tests, bench, joulecode.yml]
```

### `language`

Selects the back-end for analysis, profiling, function replacement and prompts. `typescript`
uses the same back-end as `javascript`; only the prompts differ. See
[languages.md](languages.md).

### `test`

The test gate: a candidate whose tests fail is never measured. The command runs inside the
container; exit code 0 means pass. Its output goes to the repair attempt if it fails.

### `benchmark` and `measurement`

Every repetition is one *block* in which each variant runs once, in random order, each in a
fresh container. With `adaptive: true` a candidate leaves the measurement early as soon as
its comparison with the original is settled (significantly better/worse, or equal within
`precision`), but never before `min_repetitions`. The command-line options `--repetitions`,
`--min-repetitions`, `--fixed`, `--warmup`, `--cooldown` and `--seed` override these values.
How the verdict is reached: [architecture.md](architecture.md#measurement-protocol).

### `profile`

- **Python:** `python script.py` and `python -m module` are wrapped with cProfile
  automatically. For any other command, give `profile.command` that writes a cProfile file to
  `$JOULECODE_PROFILE_OUT`.
- **PHP:** the benchmark runs with Xdebug in profile mode in an image derived from yours.
  `profile.setup` installs Xdebug if your base image is not an official `php` image, e.g.
  `apk add --no-cache php83-pecl-xdebug`.
- **JavaScript/TypeScript, Java:** profiling is switched on through `NODE_OPTIONS` /
  `JAVA_TOOL_OPTIONS`; nothing to configure.

Profilers slow programs down (Xdebug a lot), so use `profile.env` to reduce the load.

### `optimize`

Hotspots are the functions with the largest *own* time share above `min_share`, excluding
tests and benchmark. Lower `min_share` if a relevant function sits just below 5 %.

### `protected_paths`

Paths a candidate cannot change: before building, they are copied back from the original —
otherwise a candidate could “optimise” the tests or the benchmark instead of the code. Paths
must be relative and inside the repository. Add dependency manifests and build files:

| Language | Typical `protected_paths` |
|---|---|
| Python | `[tests, bench, joulecode.yml]` |
| PHP | `[tests, bench, joulecode.yml, composer.json, composer.lock]` |
| JavaScript | `[tests, bench, joulecode.yml, package.json, package-lock.json]` |
| Java | `[src/test, bench, joulecode.yml, Dockerfile, pom.xml]` |

## Environment variables (`.joulecode.env`)

All variables are optional. A command-line option always wins over a variable; a variable wins
over the built-in default.

### Measurement and language model

| Variable | Used by | Default | Command-line option |
|---|---|---|---|
| `JOULECODE_BACKEND` | `measure`, `optimize`, `serve` | `auto` | `--backend` |
| `JOULECODE_OLLAMA_HOST` | `optimize`, `serve` | `http://localhost:11434` | `--ollama-host` |
| `JOULECODE_OLLAMA_MODEL` | `optimize`, `serve` | `qwen2.5-coder:14b` | `--model` |

### METRION (written by `scripts/install.sh`)

| Variable | Meaning | Default |
|---|---|---|
| `JOULECODE_METRION_BIN` | `metrion` executable (≥ 0.2) | `/opt/metrion/.venv/bin/metrion` |
| `JOULECODE_METRION_DIR` | METRION working directory (≥ 0.2) | `/var/lib/metrion` |
| `JOULECODE_METRION_SERVICE` | on-demand systemd service | `joulecode-metrion.service` |
| `JOULECODE_METRION_DB` | DuckDB file (< 0.2) | `/var/lib/metrion/monitor.db` |
| `JOULECODE_METRION_PYTHON` | METRION's Python (< 0.2) | `/opt/metrion/.venv/bin/python` |
| `JOULECODE_METRION_RESULTS` | glob of live-attribution output (`metrion-live`) | `/var/lib/metrion/energy*` |

### Platform

| Variable | Used by | Meaning | Command-line option |
|---|---|---|---|
| `JOULECODE_DATA` | `serve` | data directory (default `~/.joulecode/server`) | `--data-dir` |
| `JOULECODE_ADMIN_TOKEN` | `serve` | fixed admin token instead of the generated one | — |
| `JOULECODE_SERVER` | `submit` | server URL (default `http://127.0.0.1:8765`) | `--server` |
| `JOULECODE_PROJECT` | `submit` | project key | `--project` |
| `JOULECODE_TOKEN` | `submit` | project token | `--token` |

### Development (language tools without Docker)

| Variable | Meaning |
|---|---|
| `JOULECODE_PHP_TOOLS=local`, `JOULECODE_PHP_AUTOLOAD` | use a local PHP with nikic/php-parser (`vendor/autoload.php`) |
| `JOULECODE_JS_TOOLS=local`, `JOULECODE_JS_NODE_MODULES` | use a local Node with the packages from `joulecode/lang/js_tools/package.json` |
| `JOULECODE_JAVA_TOOLS=local`, `JOULECODE_JAVA_CLASSPATH`, `JOULECODE_PMD` | use a local JDK with compiled `Tool`/`JfrStats` and JavaParser; optional PMD |

See [CONTRIBUTING.md](../CONTRIBUTING.md#language-tools).
