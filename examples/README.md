# Examples

Every example is a complete, self-contained repository with a `Dockerfile`, a
`joulecode.yml`, tests and a benchmark — copy one as a starting point for your own project.

## Getting started

| Example | Language | Run it |
|---|---|---|
| [python-wordstats](python-wordstats) | Python | `joulecode optimize examples/python-wordstats` |
| [php-wordstats](php-wordstats) | PHP (Composer, PHPUnit) | `joulecode optimize examples/php-wordstats` |
| [js-wordstats](js-wordstats) | JavaScript (Node, `node:test`) | `joulecode optimize examples/js-wordstats` |
| [java-wordstats](java-wordstats) | Java (`javac`, JUnit console) | `joulecode optimize examples/java-wordstats` |

All four contain the same deliberately inefficient word statistics (list membership instead of
a set, counting loops, string concatenation). [python-wordstats-optimized.patch](python-wordstats-optimized.patch)
is a hand optimisation of the Python version — measure it against the original with:

```bash
joulecode measure examples/python-wordstats --candidate-patch examples/python-wordstats-optimized.patch
```

On a machine without energy counters set `JOULECODE_BACKEND=cgroup` in `.joulecode.env`
(see [installation](../docs/installation.md#alternatives)); joule:code then compares CPU time.
The examples for PHP, JavaScript and Java need at least `qwen2.5-coder:14b` (the default).

## Test suite examples

The remaining folders belong to the [test suite](suite/README.md): positive cases
(`python-graphroute`, `python-logstats`, `python-sigproc`, `python-editdist`), traps (`python-textclean`), a negative control
(`python-checksum`), a case without hotspots (`python-iowait`) and one long example per language
(`python-fleetlog`, `php-shopcore`, `js-helpdesk`, `java-ledger`). Their hidden holdout tests and
reference patches are in [suite/](suite).

## Structure of an example

```
examples/python-wordstats/
├── Dockerfile          # installs dependencies at build time (no network at run time)
├── joulecode.yml       # test and benchmark commands, limits, measurement settings
├── src/wordstats/      # the code to optimise
├── tests/              # the test gate (protected)
└── bench/run.py        # deterministic workload, scaled with BENCH_ROUNDS (protected)
```

Some test and benchmark data in the examples (city names, product names, money formats)
are deliberately German – the identifiers and the behaviour are what matters.
