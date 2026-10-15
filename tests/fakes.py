"""Fakes for LLM and Docker so that the pipeline can be tested without GPU/Ollama."""

from __future__ import annotations

import json
from pathlib import Path

from joulecode import prompts
from joulecode.container import ExecResult

OPT_UNIQUE = '''def unique_words(text: str) -> list[str]:
    """All words in order of their first occurrence, lower-cased."""
    return list(dict.fromkeys(text.lower().split()))
'''

OPT_FREQUENT = """def frequent_words(text: str, top: int = 10) -> list[tuple[str, int]]:
    counts = Counter(text.lower().split())
    return sorted(counts.items(), key=lambda wc: (-wc[1], wc[0]))[:top]
"""

# Deliberately wrong: ties are not broken alphabetically.
BROKEN_FREQUENT = '''def frequent_words(text: str, top: int = 10) -> list[tuple[str, int]]:
    """The `top` most frequent words with their count."""
    return Counter(text.lower().split()).most_common(top)
'''

# Changes the signature → rule veto.
API_BREAK_UNIQUE = '''def unique_words(text: str, lower: bool = True) -> list[str]:
    """All words."""
    return list(dict.fromkeys((text.lower() if lower else text).split()))
'''


class ScriptedLLM:
    """Answers with prepared candidates depending on schema/hotspot."""

    name = "scripted"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.unloaded = False

    def generate_json(self, system: str, prompt: str, schema: dict, *, max_tokens=None) -> dict:
        if schema is prompts.OPTIMIZER_SCHEMA:
            self.calls.append(("optimizer", prompt))
            if "frequent_words" in prompt.split("\n", 1)[0]:
                if "did NOT pass the tests" in prompt:
                    return {
                        "candidates": [
                            _cand("Counter (repaired)", OPT_FREQUENT, ["from collections import Counter"])
                        ]
                    }
                return {
                    "candidates": [
                        _cand("most_common", BROKEN_FREQUENT, ["from collections import Counter"]),
                        _cand("Counter + sorted", OPT_FREQUENT, ["from collections import Counter"]),
                    ]
                }
            if "unique_words" in prompt.split("\n", 1)[0]:
                if "rejected" in prompt:
                    return {"candidates": [_cand("dict.fromkeys", OPT_UNIQUE, [])]}
                return {"candidates": [_cand("Parameter lower", API_BREAK_UNIQUE, [])]}
            return {"candidates": []}
        if schema is prompts.REVIEWER_SCHEMA:
            self.calls.append(("reviewer", prompt))
            return {"maintainability": 4, "concerns": [], "suggestion": "", "veto": False}
        if schema is prompts.DOC_SCHEMA:
            self.calls.append(("doc", prompt))
            return {
                "docstring": "The `top` most frequent words with their count, ties broken alphabetically."
            }
        raise AssertionError("unknown schema")

    def unload(self) -> None:
        self.unloaded = True


def _cand(title: str, code: str, imports: list[str]) -> dict:
    name = code.split("(", 1)[0].removeprefix("def ").strip()
    return {
        "title": title,
        "rationale": f"{title} instead of quadratic search.",
        "imports": imports,
        "functions": [{"name": name, "code": code}],
    }


class FakeBox:
    """Runs tests and benchmark locally 'simulated' – depending on the content of the variant."""

    def __init__(self, path: Path, runner: PathRunner) -> None:
        self.path, self.runner = path, runner
        self.id = f"box{len(runner.started)}"
        self.cpu = 0.0

    def exec(self, command, timeout_s, env=None):
        core = (self.path / "src/wordstats/core.py").read_text()
        if "pytest" in command:
            ok = "most_common" not in core and "BROKEN" not in core
            return ExecResult(
                0 if ok else 1, "1 passed" if ok else "FAILED test_frequent_words_counts_and_tie_break", 0.1
            )
        if "pstats" in command:
            stats = {
                "total_s": 10.0,
                "functions": [
                    {
                        "file": "src/wordstats/core.py",
                        "line": 13,
                        "function": "frequent_words",
                        "ncalls": 40,
                        "tottime": 6.0,
                        "cumtime": 9.0,
                    },
                    {
                        "file": "src/wordstats/core.py",
                        "line": 4,
                        "function": "unique_words",
                        "ncalls": 120,
                        "tottime": 3.0,
                        "cumtime": 3.0,
                    },
                    {
                        "file": "src/wordstats/core.py",
                        "line": 26,
                        "function": "report",
                        "ncalls": 40,
                        "tottime": 0.01,
                        "cumtime": 5.0,
                    },
                ],
            }
            return ExecResult(0, "JOULECODE_JSON" + json.dumps(stats), 0.1)
        cost = 1.0 + ("Counter" not in core) * 2.0 + ("seen = []" in core) * 1.0
        self.cpu += cost
        return ExecResult(0, "ok", cost)

    def cpu_seconds(self):
        return self.cpu

    def remove(self):
        pass


class PathRunner:
    def __init__(self) -> None:
        self.images: dict[str, Path] = {}
        self.started: list[FakeBox] = []

    def build(self, context, dockerfile, tag):
        self.images[tag] = Path(context)
        return tag

    def start(self, image, limits):
        box = FakeBox(self.images[image], self)
        self.started.append(box)
        return box
