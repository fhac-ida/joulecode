# Languages

Measurement is the same for every language: joule:code only runs the commands from
`joulecode.yml` inside the container. Language-specific are four things — analysis,
profiling, finding and replacing functions, and the prompts — which each back-end provides
behind one interface (`joulecode/lang/base.py`). `language:` in `joulecode.yml` selects it.

| Language | `language:` | Analysis | Profiler | Example | Status |
|---|---|---|---|---|---|
| Python | `python` (default) | radon, Ruff, vulture | cProfile | [python-wordstats](../examples/python-wordstats) | first back-end, reference for the others |
| PHP | `php` | nikic/PHP-Parser, Rector, PHPMD | Xdebug (derived image) | [php-wordstats](../examples/php-wordstats) | tested on the measuring host |
| JavaScript / TypeScript (Node.js) | `javascript` / `typescript` | TypeScript compiler API, ESLint (+ unicorn) | V8 `--cpu-prof` | [js-wordstats](../examples/js-wordstats) | tested on the measuring host |
| Java ≥ 17 | `java` | JavaParser, PMD | Java Flight Recorder | [java-wordstats](../examples/java-wordstats) | tested on the measuring host |

The tools for PHP, JavaScript and Java run in their own Docker images (`joulecode/php-tools`,
`joulecode/js-tools`, `joulecode/java-tools`), which joule:code builds on first use. The
measuring host needs neither PHP nor Node nor a JDK. Use at least `qwen2.5-coder:14b` (the
default) for PHP, JavaScript and Java — with `qwen2.5-coder:7b` hardly any PHP candidates passed
the tests ([which model for which GPU](installation.md#2-choose-a-language-model)).

## Python

```yaml
language: python
test:      { command: pytest -q }
benchmark: { command: python bench/run.py }
```

Install dependencies in the `Dockerfile`. Profiling wraps `python script.py` /
`python -m module` with cProfile automatically. Example: [examples/python-wordstats](../examples/python-wordstats).

## PHP

```yaml
language: php
test:      { command: vendor/bin/phpunit tests }
benchmark: { command: php bench/run.php }
protected_paths: [tests, bench, joulecode.yml, composer.json, composer.lock]
```

```dockerfile
FROM php:8.3-cli
RUN apt-get update && apt-get install -y --no-install-recommends git unzip && rm -rf /var/lib/apt/lists/*
COPY --from=composer:2 /usr/bin/composer /usr/bin/composer
WORKDIR /app
COPY composer.json ./
RUN composer install --no-interaction --no-progress --no-autoloader
COPY . /app
RUN composer dump-autoload --optimize
```

Profiling uses Xdebug in an image derived from yours — the measured images stay without
Xdebug, because merely loading it costs energy. For images not based on the official `php`
images, `profile.setup` installs Xdebug:

```yaml
profile:
  setup: "apk add --no-cache php83-pecl-xdebug"   # e.g. Alpine without pecl
```

Example: [examples/php-wordstats](../examples/php-wordstats).

## JavaScript / TypeScript

```yaml
language: javascript            # or typescript
test:      { command: node --test tests/*.test.js }
benchmark: { command: node bench/run.js }
protected_paths: [tests, bench, joulecode.yml, package.json, package-lock.json]
```

Install dependencies in the `Dockerfile` (`npm ci`). Profiling uses `node --cpu-prof`, which
joule:code enables via `NODE_OPTIONS` — this also works behind `npm run bench` and needs no
extra image. TypeScript runs directly on Node ≥ 22.18 / 24 (types are stripped, line numbers
stay the same) or via `tsx`. A candidate that imports a new package is vetoed unless the
package is already listed in a `package.json` of the repository.

Example: [examples/js-wordstats](../examples/js-wordstats) (no dependencies, `node:test`).

## Java

```yaml
language: java
test:      { command: mvn -o -q test }                       # or the JUnit console, see example
benchmark: { command: java -cp target/classes:target/bench Bench }
protected_paths: [src/test, bench, joulecode.yml, Dockerfile, pom.xml]
```

Compile in the `Dockerfile` (Maven: `mvn -B dependency:go-offline` and
`mvn -B -o package -DskipTests`, Gradle accordingly, or plain `javac`) — there is no network
at run time. A compiler error in a candidate shows up when building its image, counts like a
failing test, and the compiler output goes to the repair attempt. Profiling uses Java Flight
Recorder, enabled via `JAVA_TOOL_OPTIONS` — this works behind Maven or Gradle too and needs no
extra image, but Java ≥ 17 in the image. Methods are named `Class.method`, overloads
`Class.method(String,int)`. Importing a library the repository does not use anywhere yet leads
to a veto.

Example: [examples/java-wordstats](../examples/java-wordstats) (`javac` + JUnit console, no Maven).

## How the back-ends work

| | Python | PHP | JavaScript/TypeScript | Java |
|---|---|---|---|---|
| Find/replace functions | `ast` (`pycode.py`) | nikic/PHP-Parser (`php_tools/tool.php`), replace by line span, then syntax check | TypeScript compiler API (`js_tools/tool.js`, understands JS and TS), otherwise as PHP | JavaParser (`java_tools/Tool.java`), otherwise as PHP |
| Recognised functions | `def`, methods | functions, methods | `function`, class methods, `const f = (…) => …`, class properties with arrow functions | methods and constructors of all (also nested) classes |
| Qualified name | `Class.method` | `Class::method` (without namespace) | `Class.method` | `Class.method`, `Outer.Inner.m`, `Class.<init>`; overloaded: `Class.m(String,int)` |
| Required documentation | docstring | PHPDoc block (part of the function span, like attributes) | JSDoc block (likewise) | Javadoc (likewise, annotations belong to the span) |
| Imports | `import` / `from … import` | `use …;` after the existing ones, else after `namespace` | `import …` / `const x = require(…)` after the existing ones, else at the top | `import …;` after the existing ones, else after `package` |
| Complexity | radon | own count like PHPMD/pdepend (CCN2) | own count like ESLint `complexity` | own count like PMD |
| Maintainability index | radon | same formula as radon (Halstead from tokens, comment share) — comparable in construction, not 1:1 | likewise | likewise |
| Linter hints | Ruff (PERF, C4, SIM, FURB, UP, B) | Rector (codeQuality, deadCode) as dry run, one hint per diff hunk | ESLint: a few efficiency rules from core and unicorn (`prefer-set-has`, `prefer-includes`, `no-await-in-loop` …) | PMD, category performance |
| Dead code | vulture | PHPMD `unusedcode` | ESLint `no-unused-vars` | PMD `Unused*` |
| New dependency (veto) | module outside stdlib/project | referenced class/function neither built into PHP nor declared in the repo | package that is neither `node:…` nor in a `package.json` of the repo | import outside JDK and repo packages from a library the repo does not import yet |
| Profiler | cProfile | Xdebug (profile mode, Cachegrind) | V8 `--cpu-prof` via `NODE_OPTIONS` (sampling, 200 µs) | JFR via `JAVA_TOOL_OPTIONS` (sampling, 10 ms) |

### PHP details

PHP-Parser, PHPMD and Rector run in the image `joulecode/php-tools:<hash>` (Dockerfile in
`joulecode/lang/php_tools/`), built on first use; the hash over the folder triggers a rebuild
after changes. Calls run without network, the repository is mounted read-only.
`joulecode cleanup` keeps the image (label `joulecode.tools`).

The profile is evaluated inside the container with `php_tools/cachegrind.php` (no
dependencies): own and total time per function of the project files, where time spent in
built-in functions (`in_array`, `usort` …) counts towards the caller — like an `x in list` in
cProfile. Xdebug names (`Ns\Class->method`) are mapped to `Class::method`. Xdebug slows code
with many small calls down disproportionately, so the shares are a ranking, not a measurement.

The prompts warn about typical PHP traps that tests often catch late: numeric strings as array
keys become `int` (`array_flip`, `array_count_values`), strict comparisons, stable sorting.

### JavaScript/TypeScript details

Tool image `joulecode/js-tools` (Node 24, typescript, eslint, typescript-eslint,
eslint-plugin-unicorn). The profile is evaluated in the benchmark container with
`js_tools/cpuprofile.js` (via `node -e`, no dependencies). The V8 profiler samples and knows no
call counts — the prompt tells the optimiser. Time in built-ins (`Array.prototype.includes` …)
counts towards the caller; functions from `node_modules` count towards nobody in the project.
V8 names methods only by name; mapping uses name and line. JSX only in `.jsx`/`.tsx`. The
prompts distinguish JavaScript and TypeScript so that the model does not add types to plain
JavaScript, and warn about JS traps: integer-like object keys are sorted numerically first,
`sort()` without comparator sorts as strings.

### Java details

Tool image `joulecode/java-tools` (Temurin 21, JavaParser, PMD). A repair that only adds a
missing import counts as a change. Profiling runs with `-XX:StartFlightRecording` (one file per
JVM) together with `-XX:+DebugNonSafepoints`: without it, JFR attributes samples from JIT code
to the nearest safepoint — on the measuring host (1 CPU) an inlined list search disappeared
from the profile that way. The profile is evaluated in the benchmark container with `JfrStats`
(compiled for Java 17, shipped base64-encoded so that no derived image is needed). JDK and
library methods count towards the calling project method; lambdas and anonymous classes are
mapped via the line of their method. The prompts warn about Java traps: HashMap/HashSet
without insertion order, immutable instead of mutable return lists, `==` instead of `equals`,
locale in `toLowerCase`.

## Adding a language

Implement `Language` from `joulecode/lang/base.py` (analysis, profiling, function spans and
replacement, prompts), register it in `get_language` (`joulecode/lang/__init__.py`) and in the `language` literal of `joulecode/config.py`, add an example under
`examples/` and tests modelled on `tests/test_php.py`. `joulecode/lang/tooling.py` contains the
shared part for back-ends whose tools run in their own Docker image.
