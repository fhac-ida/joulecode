# Test suite

Several examples with fixed expectations. They check whether joule:code **judges correctly** —
not just whether the chain runs through: Does it find real savings? Does it stay quiet on code
that is already efficient? Does the test gate stop behaviour-changing optimisations?

## Examples

| Example | Category | What it checks | Expectation |
|---|---|---|---|
| [python-wordstats](../python-wordstats) | positive | list membership, counting loop, string concatenation | ≥ 30 % saved |
| [python-graphroute](../python-graphroute) | positive | class methods (`Network.neighbors`), the data structure must change; trap: a cache becomes stale after `add_edge` | ≥ 30 % |
| [python-logstats](../python-logstats) | positive | several modules; the hotspot by own time (`Request.endpoint`) is not the cause, which lies in the caller | ≥ 20 % |
| [python-sigproc](../python-sigproc) | positive | numpy in Python loops → vectorisation; floating-point tolerance; third-party library already present | ≥ 50 % |
| [python-editdist](../python-editdist) | positive | recursion without memoisation | ≥ 50 % |
| [python-textclean](../python-textclean) | traps | obvious optimisations change order, ties, `lower`/`casefold`, empty input; plus a hidden hotspot (`word_counts` spends its time in `list.count`) | ≥ 10 % |
| [python-checksum](../python-checksum) | negative control | already efficient, the work happens in hashlib/zlib/sorted | no saving > 5 % |
| [python-iowait](../python-iowait) | special case | run time is almost only `sleep` | stop without hotspots |
| [python-fleetlog](../python-fleetlog) | long (Python) | fleet telematics, ~1,400 lines in 8 modules; the cause of the fuel-level lookup lies in the caller | ≥ 30 % |
| [php-shopcore](../php-shopcore) | long (PHP) | web shop back end, ~1,400 lines in 13 classes; numeric keys as a trap | ≥ 30 % |
| [js-helpdesk](../js-helpdesk) | long (JavaScript) | ticket system, ~1,200 lines in 10 modules; SLA in business hours | ≥ 30 % |
| [java-ledger](../java-ledger) | long (Java) | payment reconciliation, ~1,150 lines in 13 classes, BigDecimal | ≥ 30 % |

Expectations and thresholds are defined in [suite.yml](suite.yml).

### Long examples

The other examples are small (54–233 lines), so the file always fits into the prompt as a
whole. The four long examples check that joule:code also works on realistically sized code: one
domain per language, 1,150–1,450 lines of source code (1,500–1,850 with tests, benchmark and
holdout), three to five built-in inefficiencies among a lot of unremarkable code. One hotspot
each lies in a file with **more than 400 lines** — above that limit (`MAX_CONTEXT_LINES` in
`joulecode/agents.py`) the optimiser only gets the function instead of the whole file.

| Example | Long file | Hotspots (share in the profile, development machine) | Reference |
|---|---|---|---|
| python-fleetlog | `trips.py` (485) | `dedupe_points` 41–48 %, fuel-level lookup `_level_before`/`_level_after` ~5 % each, `FleetIndex.vehicle` ~5 % | −86 % |
| php-shopcore | `Catalog.php` (421) | `Export::topSellers`+`revenueOf` 27 %, `Pricing::rulesFor` 10 %, `Catalog::facetCounts` 9 %, `findBySku` 7 % (Xdebug) | −92 % |
| js-helpdesk | `tickets.js` (433) | `isoDate` ~40 %, `isBusinessMinute` ~20 %, `agentLoad` ~28 % (including callback) | −96 % |
| java-ledger | `Reconciler.java` (403) | `findInvoice` 44 %, `openInvoicesOf` 16 %, `Report.openByCustomer` 7 % (JFR) | −67 % |

Each example contains traps that an obvious optimisation violates and that only the holdout
tests check — e.g. “with equal timestamps the first reading counts” (a `bisect` would take the
last one), numeric article numbers as PHP array keys, an SLA deadline ending exactly at closing
time, or an index that must be updated when invoices are added later. The descriptions in
`suite.yml` name them.

Run times come from a development machine; `BENCH_ROUNDS` is set for ~20 s and must be
recalibrated on the measuring host with `suite.py check`.

Each example comes with:

- **Holdout tests** (`holdout/<name>/`): joule:code never sees them. If a candidate passes the
  visible tests but not these, the test gate let a behaviour change through (**gate gap**). The
  visible tests are deliberately incomplete — just like in real projects.
- **Reference patch** (`reference/<name>.patch`, for python-wordstats `../python-wordstats-optimized.patch`):
  a hand optimisation showing how much is possible at all.

## Running on the measuring host

```bash
source .venv/bin/activate

# 1. Validate the examples: original and reference pass visible + holdout tests;
#    benchmark duration per example (with a suggested BENCH_ROUNDS if < 20 s)
python scripts/suite.py check

# 2. Know the noise of the machine (once)
joulecode measure examples/python-wordstats --aa

# 3. Quick trial: LLM part only, no measurement (≈ 5–10 min)
python scripts/suite.py run --out suite-runs/probe -- --no-measure

# 4. Complete, e.g. overnight: 3 runs per example, measure the references too
python scripts/suite.py run --repeat 3 --reference
```

Arguments after `--` go to `joulecode optimize` (e.g. `--model`, `--backend`, `--repetitions`);
the measurement-related ones also to the reference measurement. `--only python-graphroute,python-logstats`
restricts the run to single examples, e.g. only the long ones:

```bash
python scripts/suite.py run --only python-fleetlog,php-shopcore,js-helpdesk,java-ledger
```

**Duration:** with 10 repetitions of ≥ 20 s, 2 hotspots × 2 candidates and the combination
measurement, one example takes roughly 20–40 minutes (plus LLM time); `python-iowait` stops after
profiling. A pass over all examples therefore takes several hours, `--repeat 3` accordingly
longer.

**Interrupt and resume:** Ctrl+C ends the current step cleanly. With `--out <same folder>` the
suite continues; finished runs are skipped.

## Results

In the suite folder (`suite-runs/<timestamp>/`):

- `summary.md` — per example: expectation met in n of m runs, best saving, reference, hotspots
  hit, gate gaps; details per run; the **funnel** across all runs (generated → insertable → tests
  pass → no veto → measured → significantly better); holdout output for gate gaps
- `summary.json` — the same, machine-readable
- `<example>/run-<n>/` — full output of `joulecode optimize` (`report.md`, candidate patches,
  measurement data) plus `optimize.log` and `holdout.json`
- `<example>/reference/` — measurement of the reference patch

`python scripts/suite.py evaluate <folder>` regenerates the summary (e.g. after
`joulecode reanalyze` of a run); with `--holdout` the holdout tests are run again too.

### Reading the results

- **Expectation not met for positive examples:** look into the run's `report.md` to see where
  the candidates get stuck — funnel and candidate reports show whether it was profiling (wrong
  hotspot), the optimiser (invalid, tests failing), the reviewer (veto) or the measurement (no
  significant difference).
- **False alarm for `python-checksum`:** look at the patch. If it really is faster, the negative control
  was less efficient than thought; otherwise check the measurement noise (A/A test, coefficient of
  variation in the warnings).
- **Gate gap:** the visible tests of the example were not enough to detect the behaviour change.
  For your own projects this means: the test gate is only as good as the tests — the gap is a
  finding, not a bug of the suite.
- **Hotspots hit:** how many of the expected functions profiling found. In `python-textclean`,
  `word_counts` is deliberately hard to find (its time is in `list.count`, which cProfile counts
  as a separate function).

## Adding an example

1. A folder `examples/<name>/` like the others: `Dockerfile`, `joulecode.yml`, `src/<package>/`,
   `tests/`, `bench/run.py` with `BENCH_ROUNDS` and a fixed random seed.
2. Holdout tests in `examples/suite/holdout/<name>/test_holdout_<name>.py` (unique file name so
   pytest does not confuse them). For other languages also set `holdout_dir` (where they go in the
   copy) and `holdout_command` (how they run) in `suite.yml`, e.g. PHP
   `vendor/bin/phpunit _holdout`, JavaScript `node --test _holdout/*.test.js`, Java
   `src/test/java/holdout` with `--select-package holdout` (there, so that the Dockerfile compiles
   them at build time).
3. Optionally a hand optimisation as a patch in `examples/suite/reference/<name>.patch` (paths
   `a/src/...`, `b/src/...` relative to the example).
4. An entry in `suite.yml` with the expectation, then `python scripts/suite.py check --only <name>`.

Without Docker (e.g. under WSL) `python scripts/suite.py check --local` checks the examples
locally — without isolation and with inaccurate run times. For non-Python examples the tools must
then be available locally (`JOULECODE_JS_TOOLS=local` etc.), and the test/holdout commands from
`joulecode.yml`/`suite.yml` must work locally.
