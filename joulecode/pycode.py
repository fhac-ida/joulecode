"""AST helpers for Python source code: find and replace functions, insert imports and docstrings.

The optimiser returns whole functions instead of diffs. A 7B model generates
functions much more reliably than correct unified diffs; applying them
at the right place is done deterministically by this module.
"""

from __future__ import annotations

import ast
import re
import sys
import textwrap
from dataclasses import dataclass

FuncNode = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass
class FunctionSpan:
    qualname: str  # e.g. "frequent_words" or "Parser.parse"
    start: int  # 1-based, incl. decorators
    end: int  # 1-based, inclusive
    def_line: int
    indent: str
    node: FuncNode

    def source(self, lines: list[str]) -> str:
        return "".join(lines[self.start - 1 : self.end])


def iter_functions(tree: ast.AST) -> list[FunctionSpan]:
    """All functions and methods with qualified names (not nested functions)."""
    spans: list[FunctionSpan] = []

    def visit(body: list[ast.stmt], prefix: str, indent: str) -> None:
        for node in body:
            if isinstance(node, FuncNode):
                start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                spans.append(
                    FunctionSpan(f"{prefix}{node.name}", start, node.end_lineno, node.lineno, indent, node)
                )
            elif isinstance(node, ast.ClassDef):
                visit(node.body, f"{prefix}{node.name}.", indent + " " * 4)

    visit(tree.body, "", "")
    return spans


def _real_indent(lines: list[str], span: FunctionSpan) -> str:
    line = lines[span.start - 1]
    return line[: len(line) - len(line.lstrip())]


def find_function(source: str, qualname: str) -> FunctionSpan | None:
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for span in iter_functions(tree):
        if span.qualname == qualname:
            span.indent = _real_indent(lines, span)
            return span
    return None


def function_at_line(source: str, line: int) -> FunctionSpan | None:
    """Function whose def line or first decorator line is `line` (this is how cProfile reports functions)."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for span in iter_functions(tree):
        if line in (span.start, span.def_line):
            span.indent = _real_indent(lines, span)
            return span
    return None


def _parse_single_function(code: str, name: str) -> tuple[str, FuncNode]:
    code = textwrap.dedent(code).strip("\n") + "\n"
    tree = ast.parse(code)
    funcs = [n for n in tree.body if isinstance(n, FuncNode)]
    if len(funcs) != 1 or len(tree.body) != 1:
        raise ValueError("Expected exactly one function definition without further code.")
    if funcs[0].name != name:
        raise ValueError(f"Function is called {funcs[0].name!r}, expected {name!r}.")
    return code, funcs[0]


def replace_function(source: str, qualname: str, new_code: str) -> str:
    span = find_function(source, qualname)
    if span is None:
        raise KeyError(f"Function {qualname!r} not found.")
    code, _ = _parse_single_function(new_code, qualname.rsplit(".", 1)[-1])
    indented = textwrap.indent(code, span.indent, lambda line: line.strip() != "")
    lines = source.splitlines(keepends=True)
    result = "".join(lines[: span.start - 1]) + indented + "".join(lines[span.end :])
    ast.parse(result)
    return result


def _import_key(node: ast.stmt) -> str:
    return ast.dump(node)


_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def normalize_imports(imports: list[str], code: str) -> list[str]:
    """Turn LLM answers into valid import statements.

    Small models often return only the module name (`"collections"`) instead of
    `from collections import Counter`. For modules of the standard library, what is needed
    is derived from the code: names of the module that are used are imported
    via `from … import …`, `module.xyz` accesses via `import module`.
    """
    import importlib

    try:
        tree = ast.parse(textwrap.dedent(code))
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    except SyntaxError:
        used = set()
    out: list[str] = []
    for raw in imports:
        stmt = raw.strip().rstrip(";")
        if not stmt:
            continue
        try:
            parsed = ast.parse(stmt)
            if len(parsed.body) == 1 and isinstance(parsed.body[0], ast.Import | ast.ImportFrom):
                out.append(stmt)
                continue
        except SyntaxError:
            pass
        if not _DOTTED.match(stmt) or not is_stdlib(stmt.split(".")[0]):
            out.append(stmt)  # leave unchanged – add_imports reports the error
            continue
        try:
            public = {n for n in dir(importlib.import_module(stmt)) if not n.startswith("_")}
        except ImportError:
            public = set()
        names = sorted(used & public)
        if names:
            out.append(f"from {stmt} import {', '.join(names)}")
        if re.search(rf"\b{re.escape(stmt)}\.", code) or not names:
            out.append(f"import {stmt}")
    return out


def add_imports(source: str, imports: list[str]) -> str:
    """Insert import statements after the existing imports (duplicates are skipped)."""
    if not imports:
        return source
    tree = ast.parse(source)
    existing = {_import_key(n) for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)}
    new: list[str] = []
    for stmt in imports:
        parsed = ast.parse(stmt.strip())
        if len(parsed.body) != 1 or not isinstance(parsed.body[0], ast.Import | ast.ImportFrom):
            raise ValueError(f"Not a single import statement: {stmt!r}")
        if _import_key(parsed.body[0]) not in existing:
            new.append(stmt.strip() + "\n")
            existing.add(_import_key(parsed.body[0]))
    if not new:
        return source

    lines = source.splitlines(keepends=True)
    insert_at = 0
    for node in tree.body:
        is_docstring = (
            node is tree.body[0] and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        )
        if isinstance(node, ast.Import | ast.ImportFrom) or is_docstring:
            insert_at = node.end_lineno
        else:
            break
    prefix = "".join(lines[:insert_at])
    block = "".join(new)
    if insert_at and not any(isinstance(n, ast.Import | ast.ImportFrom) for n in tree.body[:2]):
        block = "\n" + block  # blank line after the module docstring
    result = prefix + block + "".join(lines[insert_at:])
    ast.parse(result)
    return result


def docstring_of(node: FuncNode) -> str | None:
    return ast.get_docstring(node)


def insert_docstring(source: str, qualname: str, docstring: str) -> str:
    """Set (or replace) the docstring of a function."""
    span = find_function(source, qualname)
    if span is None:
        raise KeyError(qualname)
    lines = source.splitlines(keepends=True)
    body = span.node.body
    first = body[0]
    body_line = lines[first.lineno - 1]
    body_indent = body_line[: len(body_line) - len(body_line.lstrip())]
    text = docstring.strip().replace('"""', '\\"\\"\\"')
    if "\n" in text:
        doc = f'{body_indent}"""{textwrap.indent(text, body_indent).lstrip()}\n{body_indent}"""\n'
    else:
        doc = f'{body_indent}"""{text}"""\n'
    has_doc = (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )
    if has_doc:
        result = "".join(lines[: first.lineno - 1]) + doc + "".join(lines[first.end_lineno :])
    else:
        result = "".join(lines[: first.lineno - 1]) + doc + "".join(lines[first.lineno - 1 :])
    ast.parse(result)
    return result


def signature(node: FuncNode) -> str:
    """Comparable representation of the signature (names, kinds, number of defaults)."""
    a = node.args
    parts = [
        "pos:" + ",".join(x.arg for x in a.posonlyargs),
        "args:" + ",".join(x.arg for x in a.args),
        "var:" + (a.vararg.arg if a.vararg else ""),
        "kw:" + ",".join(x.arg for x in a.kwonlyargs),
        "kwvar:" + (a.kwarg.arg if a.kwarg else ""),
        f"defaults:{len(a.defaults)}",
        "kwdefaults:" + ",".join("1" if d is not None else "0" for d in a.kw_defaults),
        "async" if isinstance(node, ast.AsyncFunctionDef) else "sync",
    ]
    return ";".join(parts)


def imported_top_modules(source: str) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def is_stdlib(module: str) -> bool:
    return module in sys.stdlib_module_names or module == "__future__"
