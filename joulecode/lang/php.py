"""PHP back-end: PHP-Parser, PHPMD, Rector and Xdebug.

The PHP tools run in the image `joulecode/php-tools` (Dockerfile in
`php_tools/`), which joule:code builds itself on first use – the
measurement machine does not need PHP. For development and tests a local PHP can
be used instead of the image: `JOULECODE_PHP_TOOLS=local` and
`JOULECODE_PHP_AUTOLOAD=<path/to/vendor/autoload.php>`.

* **Code operations:** `tool.php` returns functions and methods with spans
  (incl. PHPDoc and attributes), signature, PHPDoc text, complexity as well as
  use statements and referenced names. Code is applied in Python by
  lines; afterwards PHP-Parser checks the syntax.
* **Analysis:** complexity (CCN as in PHPMD), maintainability index (formula as in
  radon), PHPMD `unusedcode` as dead code, Rector (codeQuality, deadCode,
  dry run) as findings for the optimiser, coupling via referenced classes.
* **Profiling:** Xdebug in profile mode in an image derived from the original
  (measurement images stay without Xdebug). The analysis happens in the container.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from pathlib import Path

from joulecode.analysis import Finding
from joulecode.lang.base import LanguagePrompts
from joulecode.lang.tooling import (
    ToolBox,
    ToolError,
    ToolLanguage,
    diff_hunks,
    format_doc_block,
    relative,
    require_json,
    source_files,
)
from joulecode.lang.tooling import tools_image as _tools_image
from joulecode.profiling import Hotspot, ProfileResult, _parse_stats

TOOLS_DIR = Path(__file__).with_name("php_tools")
PROFILE_DIR = "/tmp/joulecode-prof"

# Installs Xdebug in the derived profiling image. Works for the official
# php images (Debian and Alpine); other images: profile.setup in joulecode.yml.
DEFAULT_XDEBUG_SETUP = (
    "set -e; "
    "if php -m | grep -qi '^xdebug$'; then exit 0; fi; "
    "if command -v apk >/dev/null 2>&1; then apk add --no-cache $PHPIZE_DEPS linux-headers; fi; "
    "pecl install xdebug; docker-php-ext-enable xdebug"
)

PROMPTS = LanguagePrompts(
    language="PHP",
    fence="php",
    stdlib="the PHP core and the extensions already used in the project (no new Composer package)",
    idioms=(
        "- Readable, idiomatic PHP code (PSR-12) instead of tricks; keep the PHPDoc block and adapt it if "
        "necessary.\n"
        "- Use built-in array and string functions (implemented in C), e.g. isset/array_key_exists "
        "on an array with keys instead of in_array in loops, implode instead of string concatenation "
        "in loops.\n"
        "- Mind PHP semantics: array keys like '1' become int (array_flip, array_count_values, "
        "$a[$word]), comparisons stay strict (=== / in_array(..., true)), sorting and tie-breaking rule "
        "stay the same.\n"
        "- A list and an associative array are different return values: if the original returns a list of pairs "
        "[[word, count], …], the candidate must return exactly that – not ['word' => count]. "
        "usort/sort discard keys, arsort/asort keep them."
    ),
    qualname_example="`Class::method` or `functionName`",
    import_example="`use App\\Util\\Helper;`",
    doc_term="PHPDoc block",
    doc_format="only the text, without /** */ and without leading asterisks",
    function_hint=(
        "Return every changed function or method COMPLETELY (incl. PHPDoc, attributes and "
        "visibility), without `<?php`, without the surrounding class and without its indentation."
    ),
    lint_tool="Rector",
)


class PhpToolError(ToolError):
    pass


def tools_image() -> str:
    return _tools_image(TOOLS_DIR, "php")


class PhpTools(ToolBox):
    """tool.php, PHPMD and Rector – in the image joulecode/php-tools or locally."""

    tools_dir = TOOLS_DIR
    name = "php"
    interpreter = "php"
    script = "tool.php"
    error_cls = PhpToolError

    def local_argv(self, argv: list[str]) -> list[str]:
        # Locally: PHPMD/Rector from the vendor/bin next to JOULECODE_PHP_AUTOLOAD, otherwise from PATH.
        autoload = os.environ.get("JOULECODE_PHP_AUTOLOAD")
        local_bin = Path(autoload).parent / "bin" / argv[0] if autoload else None
        if local_bin and local_bin.is_file():
            return [str(local_bin), *argv[1:]]
        return argv


_TOOLS: PhpTools | None = None


def default_tools() -> PhpTools:
    global _TOOLS
    if _TOOLS is None:
        _TOOLS = PhpTools()
    return _TOOLS


# -- Helpers ---------------------------------------------------------------------
def php_files(repo: Path, excluded: list[str]) -> list[str]:
    return source_files(repo, excluded, (".php",))


format_phpdoc = format_doc_block  # former name


def parse_rector(output: str, root_prefix: str, _res=None) -> list[Finding]:
    """Rector JSON (dry run) → one finding per diff hunk with the proposed changes."""
    data = json.loads(require_json(output))
    findings = []
    for fd in data.get("file_diffs", []):
        file = relative(fd["file"], root_prefix)
        rules = ", ".join(r.rsplit("\\", 1)[-1].removesuffix("Rector") for r in fd.get("applied_rectors", []))
        findings.extend(
            Finding("rector", rules or "rector", " | ".join(changes[:6]), file, first, "\n".join(changes))
            for first, changes in diff_hunks(fd.get("diff", ""))
        )
    return findings


def parse_phpmd(output: str, root_prefix: str, _res=None) -> list[Finding]:
    data = json.loads(require_json(output))
    return [
        Finding(
            "phpmd",
            v.get("rule", ""),
            v.get("description", "").strip(),
            relative(f["file"], root_prefix),
            v["beginLine"],
        )
        for f in data.get("files", [])
        for v in f.get("violations", [])
    ]


def map_xdebug_name(name: str) -> str | None:
    """`App\\Foo->bar` → `Foo::bar`, `App\\top` → `top`; closures, include, internal functions → None."""
    if name.startswith(("php::", "require", "include")) or "{" in name:
        return None
    for sep in ("->", "::"):
        if sep in name:
            cls, method = name.split(sep, 1)
            return f"{cls.rsplit(chr(92), 1)[-1]}::{method}"
    return name.rsplit("\\", 1)[-1]


# -- Backend ------------------------------------------------------------------------
class PhpLanguage(ToolLanguage):
    name = "php"
    display = "PHP"
    prompts = PROMPTS
    profiler = "Xdebug"
    method_sep = "::"
    suffixes = (".php",)
    lint_name, dead_name = "rector", "phpmd"
    metrics_labels = ("PHP", "Rector", "PHPMD")

    def default_tools(self) -> PhpTools:
        return default_tools()

    def clean_code(self, code: str) -> str:
        return re.sub(r"^\s*<\?php\s*", "", code)

    # Imports -------------------------------------------------------------------------
    def normalize_imports(self, imports: list[str], code: str) -> list[str]:
        out = []
        for raw in imports:
            stmt = raw.strip().rstrip(";").strip()
            if not stmt:
                continue
            if not stmt.startswith("use "):
                stmt = "use " + stmt.lstrip("\\")
            out.append(stmt + ";")
        return out

    def add_imports(self, source: str, imports: list[str]) -> str:
        if not imports:
            return source
        info = self._call("file_info", source=source)
        existing = {u.lower() for u in info["uses"]}
        new = []
        for stmt in imports:
            if not re.fullmatch(
                r"use\s+(function\s+|const\s+)?\\?[A-Za-z_][\w\\]*(\s+as\s+\w+)?\s*;", stmt.strip()
            ):
                raise ValueError(f"Not a single use statement: {stmt!r}")
            norm = re.sub(r"\s+", " ", stmt.strip()).replace("use \\", "use ")
            if norm.lower() not in existing:
                new.append(norm + "\n")
                existing.add(norm.lower())
        if not new:
            return source
        block = "".join(new)
        if not info["uses"]:
            block = "\n" + block  # blank line after namespace/declare/<?php
        return self.insert_lines(source, info["use_insert_after"], block)

    def new_dependencies(self, before: str, after: str, internal: frozenset[str]):
        def names(src: str) -> set[str]:
            # referenced classes/functions (incl. use statements), without the ones built into PHP.
            return set(self._call("file_info", source=src)["referenced"])

        new = sorted(names(after) - names(before))
        third_party = sorted(n for n in new if n.split("\\")[0] not in internal and n not in internal)
        return new, third_party

    # Analysis -----------------------------------------------------------------------
    def internal_from(self, answer: dict) -> list[str]:
        # names declared in the repository and their top-level namespace segments.
        names = [n for data in answer["files"].values() for n in data.get("info", {}).get("declared", [])]
        return sorted({n.split("\\")[0] for n in names} | set(names))

    def linters(self, repo: Path, files: list[str]):
        base = self.tools.repo_path(repo)
        rector = [
            "rector", "process", *[f"{base}/{f}" for f in files], "--dry-run", "--output-format=json",
            "--no-progress-bar", "--clear-cache", "--config", self.tools.tool_path("rector.php"),
        ]  # fmt: skip
        phpmd = ["phpmd", ",".join(f"{base}/{f}" for f in files), "json", "unusedcode"]
        return [("rector", rector, parse_rector), ("phpmd", phpmd, parse_phpmd)]

    # Profiling ----------------------------------------------------------------------
    def profile(self, cfg, runner, image, repo, *, log=print) -> ProfileResult:
        setup = cfg.profile.setup or DEFAULT_XDEBUG_SETUP
        try:
            with tempfile.TemporaryDirectory(prefix="joulecode-xdebug-") as tmp:
                ctx = Path(tmp)
                (ctx / "cachegrind.php").write_text(
                    (TOOLS_DIR / "cachegrind.php").read_text(encoding="utf-8")
                )
                short = image.split(":")[-1][:12]
                # FROM needs a name; not every builder understands the ID alone.
                base = (
                    runner.tag(image, f"joulecode/profile-base:{short}") if hasattr(runner, "tag") else image
                )
                (ctx / "Dockerfile").write_text(
                    f"FROM {base}\nUSER root\nRUN {setup}\nCOPY cachegrind.php /opt/joulecode/cachegrind.php\n",
                    encoding="utf-8",
                )
                log("Building profiling image with Xdebug …")
                prof_image = runner.build(ctx, "Dockerfile", f"joulecode/profile:{short}")
        except Exception as exc:  # noqa: BLE001 – Docker errors are manifold
            return ProfileResult(
                ok=False, profiler=self.profiler, error=f"Xdebug image could not be built: {exc}"
            )

        env = {
            **cfg.benchmark.env,
            **cfg.profile.env,
            "XDEBUG_MODE": "profile",
            "XDEBUG_CONFIG": f"start_with_request=yes output_dir={PROFILE_DIR} profiler_output_name=cachegrind.out.%p",
        }
        command = cfg.profile.command or cfg.benchmark.command
        box = runner.start(prof_image, cfg.limits)
        try:
            log("Profiling run (Xdebug) …")
            res = box.exec(f"mkdir -p {PROFILE_DIR} && {command}", cfg.profile.timeout_s, env)
            if not res.ok:
                return ProfileResult(
                    ok=False,
                    profiler=self.profiler,
                    error=f"Profiling run exit {res.exit_code}:\n{res.output[-2000:]}",
                )
            script = (
                f"php -d xdebug.mode=off /opt/joulecode/cachegrind.php {PROFILE_DIR} "
                f"{shlex.quote(json.dumps(cfg.protected_paths))}"
            )
            stats_res = box.exec(script, 300)
        finally:
            box.remove()
        try:
            stats = _parse_stats(stats_res.output)
        except (ValueError, json.JSONDecodeError) as exc:
            return ProfileResult(ok=False, profiler=self.profiler, error=str(exc))
        hotspots = self.select_hotspots(
            stats, repo, limit=cfg.optimize.hotspots, min_share=cfg.optimize.min_share
        )
        return ProfileResult(ok=True, profiler=self.profiler, total_s=stats["total_s"], hotspots=hotspots)

    def select_hotspots(self, stats: dict, repo: Path, *, limit: int, min_share: float) -> list[Hotspot]:
        total = stats["total_s"] or 1e-9
        hotspots: list[Hotspot] = []
        for row in sorted(stats["functions"], key=lambda r: r["tottime"], reverse=True):
            share = row["tottime"] / total
            if share < min_share or len(hotspots) >= limit:
                break
            path = repo / row["file"]
            qualname = map_xdebug_name(row["function"])
            if path.suffix != ".php" or not path.is_file() or qualname is None:
                continue
            source = path.read_text(encoding="utf-8")
            funcs = self.functions(source)
            span = next((f for f in funcs if f.qualname.lower() == qualname.lower()), None) or next(
                (f for f in funcs if f.start <= row["line"] <= f.end), None
            )
            if span is None or any(h.file == row["file"] and h.qualname == span.qualname for h in hotspots):
                continue
            hotspots.append(
                Hotspot(
                    file=row["file"], qualname=span.qualname, line=span.start, end_line=span.end,
                    ncalls=row["ncalls"], tottime=row["tottime"], cumtime=row["cumtime"], share=share,
                    code=span.source(source.splitlines(keepends=True)),
                )
            )  # fmt: skip
        return hotspots
