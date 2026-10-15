# Contributing to joule:code

Thank you for your interest! Bug reports, measurements from other hardware, new language
back-ends and documentation improvements are all welcome.

## Reporting issues

Please include the output of `joulecode doctor`, the command you ran, and — for measurement
questions — the `result.json` or `report.md` of the run. For security issues see
[SECURITY.md](SECURITY.md) instead of opening a public issue.

## Development setup

```bash
git clone https://github.com/fhac-ida/joulecode.git && cd joulecode
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -q                                     # unit and integration tests
ruff check joulecode tests scripts
ruff format --check joulecode tests scripts
```

Tests that need Docker or language tools that are not installed are skipped automatically.
Python ≥ 3.11 is required.

### Language tools

The PHP, JavaScript and Java back-ends normally run their tools in Docker images. For the tests
(and for development without Docker) they can use local installations instead:

```bash
# PHP: php and nikic/php-parser
JOULECODE_PHP_TOOLS=local JOULECODE_PHP_AUTOLOAD=/path/to/vendor/autoload.php pytest -q tests/test_php.py

# JavaScript: node and the packages from joulecode/lang/js_tools/package.json
mkdir -p /tmp/js-tools && cp joulecode/lang/js_tools/package.json /tmp/js-tools/ && (cd /tmp/js-tools && npm install)
JOULECODE_JS_TOOLS=local JOULECODE_JS_NODE_MODULES=/tmp/js-tools/node_modules pytest -q tests/test_javascript.py

# Java: a JDK and Tool.java/JfrStats.java compiled against javaparser-core
javac -cp javaparser-core.jar -d /tmp/jt joulecode/lang/java_tools/{Tool,JfrStats}.java
JOULECODE_JAVA_TOOLS=local JOULECODE_JAVA_CLASSPATH=/tmp/jt:javaparser-core.jar pytest -q tests/test_java.py
```

### Test suite

Changes to profiling, prompts, agents or measurement should be checked against the
[test suite](examples/suite/README.md) on a measuring host:

```bash
python scripts/suite.py check
python scripts/suite.py run --repeat 3 --reference
```

## Pull requests

- Keep changes focused; one topic per pull request.
- Add or update tests (`tests/`), and update the documentation in `docs/` when behaviour or
  options change.
- `pytest` and `ruff` must pass (CI runs both).
- Measurement-related changes: describe how you verified them (hardware, back-end, A/A noise).
- Do not commit measurement results, METRION data or third-party archives.

## Code conventions

- Formatting and linting with Ruff (`line-length = 110`, see `pyproject.toml`).
- Principles from [docs/architecture.md](docs/architecture.md#principles): tools measure and
  decide, LLMs only propose; never weaken the test gate or the sandbox.
- Code comments, CLI messages, reports and documentation are written in English.

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
