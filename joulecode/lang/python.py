"""Python back-end: Ruff/radon/vulture, cProfile, AST operations from `pycode`."""

from __future__ import annotations

from pathlib import Path

from joulecode import pycode
from joulecode.lang.base import FunctionInfo, Language, LanguagePrompts

PROMPTS = LanguagePrompts(
    language="Python",
    fence="python",
    stdlib="the Python standard library",
    idioms="- Readable, idiomatic code instead of tricks; keep the docstring and adapt it if necessary.",
    qualname_example="`Class.method`",
    import_example="`from collections import Counter`",
    doc_term="docstring",
    doc_format="without quotes, without indentation",
    function_hint="Return every changed function COMPLETELY, without indentation of the surrounding class.",
    lint_tool="Ruff",
)


def _info(span: pycode.FunctionSpan) -> FunctionInfo:
    return FunctionInfo(
        qualname=span.qualname,
        start=span.start,
        end=span.end,
        def_line=span.def_line,
        indent=span.indent,
        signature=pycode.signature(span.node),
        docstring=pycode.docstring_of(span.node),
    )


class PythonLanguage(Language):
    name = "python"
    display = "Python"
    prompts = PROMPTS
    profiler = "cProfile"

    def analyze_repo(self, repo: Path, excluded: list[str]):
        from joulecode.analysis import analyze_repo

        return analyze_repo(repo, excluded)

    def profile(self, cfg, runner, image, repo, *, log=print):
        from joulecode.profiling import profile

        return profile(cfg, runner, image, repo, log=log)

    def internal_modules(self, static) -> frozenset[str]:
        return frozenset(m.split(".")[0] for m in static.coupling)

    def functions(self, source: str) -> list[FunctionInfo]:
        import ast

        lines = source.splitlines(keepends=True)
        out = []
        for span in pycode.iter_functions(ast.parse(source)):
            span.indent = pycode._real_indent(lines, span)
            out.append(_info(span))
        return out

    def find_function(self, source: str, qualname: str) -> FunctionInfo | None:
        span = pycode.find_function(source, qualname)
        return _info(span) if span else None

    def replace_function(self, source: str, qualname: str, new_code: str) -> str:
        return pycode.replace_function(source, qualname, new_code)

    def normalize_imports(self, imports: list[str], code: str) -> list[str]:
        return pycode.normalize_imports(imports, code)

    def add_imports(self, source: str, imports: list[str]) -> str:
        return pycode.add_imports(source, imports)

    def insert_docstring(self, source: str, qualname: str, text: str) -> str:
        return pycode.insert_docstring(source, qualname, text)

    def complexity(self, source: str, qualname: str) -> int | None:
        from joulecode.analysis import complexity_of

        span = pycode.find_function(source, qualname)
        if span is None:
            return None
        return complexity_of(span.source(source.splitlines(keepends=True)))

    def maintainability_index(self, source: str) -> float:
        from joulecode.analysis import maintainability_index

        return maintainability_index(source)

    def canonical(self, source: str) -> str:
        import ast

        # The syntax tree knows no formatting and no comments, but it does know indentation as structure.
        return ast.dump(ast.parse(source))

    def new_dependencies(self, before: str, after: str, internal: frozenset[str]):
        new = pycode.imported_top_modules(after) - pycode.imported_top_modules(before)
        third_party = sorted(m for m in new if not pycode.is_stdlib(m) and m not in internal)
        return sorted(new), third_party
